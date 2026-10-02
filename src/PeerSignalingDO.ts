// PeerSignalingDO: Durable Object that owns one peer's signaling state.
//
// Routing is deterministic: env.PEER_SIGNALING.getByName(peerId) always
// returns the same DO instance for a given peerId. This lets any edge node
// deliver a signaling message to a peer's live WebSocket connection with
// strong consistency.

import { DurableObject } from "cloudflare:workers";
import type { Env } from "./env";
import type { RelayedMessage, SignalingMessage } from "./types";

const MAX_QUEUED_MESSAGES = 50;
const QUEUE_TTL_MS = 5 * 60 * 1000; // 5 minutes

export class PeerSignalingDO extends DurableObject<Env> {
  private socket: WebSocket | null = null;
  private peerId: string;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    // The DO id is derived from the peer id (getByName). We recover it lazily
    // from the first WebSocket connect so we don't need to store a mapping.
    this.peerId = "";
  }

  /**
   * HTTP entry point — handles WebSocket upgrade for this peer.
   * The Worker passes the peerId via the `X-Peer-Id` header.
   * Only one live socket per peer; a new connection replaces the old one.
   */
  async fetch(request: Request): Promise<Response> {
    const peerId = request.headers.get("X-Peer-Id") || "";
    if (!peerId) {
      return new Response("X-Peer-Id header required", { status: 400 });
    }
    this.peerId = peerId;

    const upgradeHeader = request.headers.get("Upgrade");
    if (upgradeHeader !== "websocket") {
      return new Response("expected websocket upgrade", { status: 426 });
    }

    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair) as [WebSocket, WebSocket];

    // Replace any existing socket.
    if (this.socket) {
      try {
        this.socket.close(1000, "replaced by new connection");
      } catch {
        /* ignore */
      }
    }

    server.accept();
    this.socket = server;

    server.addEventListener("message", (event) => {
      this.handleClientMessage(server, event.data);
    });
    server.addEventListener("close", () => {
      if (this.socket === server) this.socket = null;
    });
    server.addEventListener("error", () => {
      if (this.socket === server) this.socket = null;
    });

    // Deliver any queued messages for this peer.
    this.ctx.waitUntil(this.drainQueue(server));

    return new Response(null, { status: 101, webSocket: client });
  }

  /**
   * Deliver a signaling message to this peer. If the peer is connected it is
   * sent immediately; otherwise it is queued in DO storage for up to QUEUE_TTL_MS.
   */
  async deliver(message: SignalingMessage): Promise<{ delivered: boolean; queued: boolean }> {
    const envelope: RelayedMessage = {
      ...message,
      from: message.from || "unknown",
      id: crypto.randomUUID(),
      timestamp: Date.now(),
    };

    if (this.socket) {
      try {
        this.socket.send(JSON.stringify(envelope));
        return { delivered: true, queued: false };
      } catch {
        // socket broken; fall through to queue
        this.socket = null;
      }
    }

    await this.queueMessage(envelope);
    return { delivered: false, queued: true };
  }

  /** Return whether this peer currently has a live connection. */
  isOnline(): boolean {
    return this.socket !== null;
  }

  // ---- internals ----

  private handleClientMessage(server: WebSocket, data: string | ArrayBuffer) {
    // Outbound signaling: the connected peer sends a message to another peer.
    // We parse it, stamp `from`, and route it to the recipient's DO.
    let parsed: SignalingMessage;
    try {
      parsed = JSON.parse(typeof data === "string" ? data : new TextDecoder().decode(data));
    } catch {
      server.send(JSON.stringify({ type: "error", error: "invalid JSON" }));
      return;
    }
    if (!parsed.to) {
      server.send(JSON.stringify({ type: "error", error: "missing 'to' field" }));
      return;
    }
    const outgoing: SignalingMessage = { ...parsed, from: this.peerId };
    const recipientStub = this.env.PEER_SIGNALING.getByName(parsed.to);
    this.ctx.waitUntil(recipientStub.deliver(outgoing));
  }

  private async queueMessage(message: RelayedMessage): Promise<void> {
    await this.ctx.blockConcurrencyWhile(async () => {
      const stored = (await this.ctx.storage.get<RelayedMessage[]>("queue")) || [];
      const filtered = stored.filter((m) => Date.now() - m.timestamp < QUEUE_TTL_MS);
      filtered.push(message);
      while (filtered.length > MAX_QUEUED_MESSAGES) filtered.shift();
      await this.ctx.storage.put("queue", filtered);
    });
  }

  private async drainQueue(server: WebSocket): Promise<void> {
    const stored = (await this.ctx.storage.get<RelayedMessage[]>("queue")) || [];
    const now = Date.now();
    const fresh = stored.filter((m) => now - m.timestamp < QUEUE_TTL_MS);
    if (fresh.length === 0) {
      if (stored.length > 0) await this.ctx.storage.delete("queue");
      return;
    }
    for (const msg of fresh) {
      try {
        server.send(JSON.stringify(msg));
      } catch {
        break;
      }
    }
    await this.ctx.storage.put("queue", []);
  }
}
