// RoomDO: one Durable Object per room. It holds the live presence table in
// memory and prunes peers that stop sending heartbeats.
//
// Presence is intentionally ephemeral: if a Durable Object is evicted, peers
// simply re-register on their next heartbeat (every TTL/3 seconds).

import { DurableObject } from "cloudflare:workers";
import type { Env } from "./env";
import type {
  AnnounceRequest,
  AnnounceResponse,
  Endpoint,
  LeaveResponse,
  PeerView,
  PeersResponse,
  RpcResult,
} from "./types";

interface StoredPeer {
  peerId: string;
  secretHash: string;
  punchKey: string;
  natType: string;
  publicEndpoint: Endpoint | null;
  localEndpoint: Endpoint | null;
  observedIp: string;
  metadata: Record<string, unknown> | null;
  firstSeen: number;
  lastSeen: number;
}

const DEFAULT_TTL_SECONDS = 45;
const DEFAULT_MAX_PEERS = 256;

export class RoomDO extends DurableObject<Env> {
  private peers = new Map<string, StoredPeer>();

  private ttlMs(): number {
    const parsed = Number.parseInt(this.env.PEER_TTL_SECONDS || "", 10);
    const seconds = Number.isFinite(parsed) && parsed > 0 ? parsed : DEFAULT_TTL_SECONDS;
    return seconds * 1000;
  }

  private maxPeers(): number {
    const parsed = Number.parseInt(this.env.MAX_PEERS_PER_ROOM || "", 10);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : DEFAULT_MAX_PEERS;
  }

  /**
   * Register or refresh a peer. This doubles as the heartbeat: the response
   * contains the current peer list so the client discovers newcomers without
   * an extra round trip.
   */
  async announce(req: AnnounceRequest, observedIp: string): Promise<RpcResult<AnnounceResponse>> {
    const now = Date.now();
    this.prune(now);

    const secretHash = await sha256Hex(req.secret);
    const existing = this.peers.get(req.peerId);
    if (existing && !timingSafeEqual(existing.secretHash, secretHash)) {
      return {
        ok: false,
        status: 403,
        error: "peerId is already registered with a different secret",
      };
    }
    if (!existing && this.peers.size >= this.maxPeers()) {
      return { ok: false, status: 429, error: "room is full" };
    }

    const stored: StoredPeer = {
      peerId: req.peerId,
      secretHash,
      punchKey: req.punchKey,
      natType: req.natType,
      publicEndpoint: req.publicEndpoint,
      localEndpoint: req.localEndpoint,
      observedIp,
      metadata: req.metadata ?? null,
      firstSeen: existing ? existing.firstSeen : now,
      lastSeen: now,
    };
    this.peers.set(req.peerId, stored);
    await this.ctx.storage.setAlarm(now + this.ttlMs());

    return {
      ok: true,
      room: req.room,
      self: toView(stored),
      peers: this.viewsExcept(req.peerId),
      ttlSeconds: this.ttlMs() / 1000,
      heartbeatSeconds: Math.max(5, Math.floor(this.ttlMs() / 3000)),
      serverTime: now,
    };
  }

  /** List live peers. The caller must already be registered in this room. */
  async list(room: string, peerId: string, secret: string): Promise<RpcResult<PeersResponse>> {
    const now = Date.now();
    this.prune(now);

    const self = this.peers.get(peerId);
    if (!self || !timingSafeEqual(self.secretHash, await sha256Hex(secret))) {
      return {
        ok: false,
        status: 403,
        error: "unknown peer or bad secret; call /api/announce first",
      };
    }
    self.lastSeen = now;

    return {
      ok: true,
      room,
      peers: this.viewsExcept(peerId),
      ttlSeconds: this.ttlMs() / 1000,
      serverTime: now,
    };
  }

  /** Remove a peer from the room. */
  async leave(peerId: string, secret: string): Promise<RpcResult<LeaveResponse>> {
    const self = this.peers.get(peerId);
    if (!self) return { ok: true, removed: false };
    if (!timingSafeEqual(self.secretHash, await sha256Hex(secret))) {
      return { ok: false, status: 403, error: "bad secret" };
    }
    this.peers.delete(peerId);
    return { ok: true, removed: true };
  }

  /** Called by the Durable Object alarm to evict peers that stopped heartbeating. */
  async alarm(): Promise<void> {
    const now = Date.now();
    this.prune(now);
    if (this.peers.size > 0) {
      await this.ctx.storage.setAlarm(now + this.ttlMs());
    }
  }

  private prune(now: number): void {
    const cutoff = now - this.ttlMs();
    for (const [peerId, peer] of this.peers) {
      if (peer.lastSeen < cutoff) this.peers.delete(peerId);
    }
  }

  private viewsExcept(peerId: string): PeerView[] {
    const views: PeerView[] = [];
    for (const peer of this.peers.values()) {
      if (peer.peerId !== peerId) views.push(toView(peer));
    }
    return views;
  }
}

function toView(peer: StoredPeer): PeerView {
  return {
    peerId: peer.peerId,
    punchKey: peer.punchKey,
    natType: peer.natType,
    publicEndpoint: peer.publicEndpoint,
    localEndpoint: peer.localEndpoint,
    observedIp: peer.observedIp,
    lastSeen: peer.lastSeen,
    online: true,
    metadata: peer.metadata,
  };
}

async function sha256Hex(input: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(input));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}
