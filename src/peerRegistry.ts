// PeerRegistry: KV-backed global address registry for P2P peer discovery.
//
// Each peer announces its (peerId -> address/port) mapping with a TTL.
// Lookups read directly from KV for low-latency, globally-distributed reads.

import type { Env } from "./env";
import type { PeerAnnouncement, PeerRecord } from "./types";

const KEY_PREFIX = "peer:";

function peerKey(peerId: string): string {
  return `${KEY_PREFIX}${peerId}`;
}

/** Validate an announcement payload. Returns an error string or null. */
export function validateAnnouncement(body: unknown): string | null {
  if (typeof body !== "object" || body === null) return "body must be a JSON object";
  const a = body as Record<string, unknown>;
  if (typeof a.peerId !== "string" || a.peerId.length === 0)
    return "peerId must be a non-empty string";
  if (a.peerId.length > 128) return "peerId too long (max 128)";
  if (typeof a.address !== "string" || a.address.length === 0)
    return "address must be a non-empty string";
  if (typeof a.port !== "number" || !Number.isInteger(a.port) || a.port < 1 || a.port > 65535)
    return "port must be an integer 1-65535";
  if (a.metadata !== undefined && typeof a.metadata !== "object")
    return "metadata must be an object";
  return null;
}

/** Announce (register or refresh) a peer's address with TTL. */
export async function announcePeer(
  env: Env,
  announcement: PeerAnnouncement,
): Promise<{ peerRecord: PeerRecord; ttlSeconds: number }> {
  const ttlSeconds = Number.parseInt(env.PEER_TTL_SECONDS || "300", 10) || 300;
  const now = Date.now();
  const record: PeerRecord = {
    ...announcement,
    lastSeen: now,
    expiresAt: now + ttlSeconds * 1000,
  };
  await env.PEER_REGISTRY.put(peerKey(announcement.peerId), JSON.stringify(record), {
    expirationTtl: ttlSeconds,
  });
  return { peerRecord: record, ttlSeconds };
}

/** Look up a peer by id. Returns null if not found / expired. */
export async function lookupPeer(env: Env, peerId: string): Promise<PeerRecord | null> {
  const raw = await env.PEER_REGISTRY.get(peerKey(peerId));
  if (!raw) return null;
  try {
    return JSON.parse(raw) as PeerRecord;
  } catch {
    return null;
  }
}

/** Remove a peer from the registry. */
export async function unregisterPeer(env: Env, peerId: string): Promise<boolean> {
  const exists = await env.PEER_REGISTRY.get(peerKey(peerId));
  if (!exists) return false;
  await env.PEER_REGISTRY.delete(peerKey(peerId));
  return true;
}

/** List all currently registered peers (best-effort, paginated). */
export async function listPeers(env: Env): Promise<PeerRecord[]> {
  const records: PeerRecord[] = [];
  let cursor: string | undefined;
  do {
    const result = await env.PEER_REGISTRY.list({ prefix: KEY_PREFIX, cursor, limit: 1000 });
    const values = await Promise.all(result.keys.map((k) => env.PEER_REGISTRY.get(k.name)));
    for (const v of values) {
      if (!v) continue;
      try {
        records.push(JSON.parse(v) as PeerRecord);
      } catch {
        // ignore malformed entries
      }
    }
    cursor = result.list_complete ? undefined : result.cursor;
  } while (cursor);
  return records;
}
