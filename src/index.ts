// P2P Addressing Worker — entry point.
//
// REST API:
//   POST   /announce            Register/refresh a peer's address
//   GET    /lookup/:peerId      Look up a peer's address
//   GET    /peers               List all known peers
//   DELETE /peers/:peerId       Unregister a peer
//   GET    /health              Health check
//
// WebSocket signaling:
//   GET    /ws/:peerId          Open a signaling channel for a peer

import type { Env } from "./env";
import type { PeerAnnouncement } from "./types";
import {
  announcePeer,
  listPeers,
  lookupPeer,
  unregisterPeer,
  validateAnnouncement,
} from "./peerRegistry";

export { PeerSignalingDO } from "./PeerSignalingDO";

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;

    // CORS preflight
    if (request.method === "OPTIONS") {
      return corsResponse(new Response(null, { status: 204 }));
    }

    // WebSocket signaling channel
    if (path.startsWith("/ws/")) {
      return handleWebSocket(request, env, path.slice(4));
    }

    try {
      let response: Response;

      if (path === "/announce" && request.method === "POST") {
        response = await handleAnnounce(request, env);
      } else if (path.startsWith("/lookup/") && request.method === "GET") {
        response = await handleLookup(env, decodeURIComponent(path.slice(8)));
      } else if (path === "/peers" && request.method === "GET") {
        response = await handleListPeers(env);
      } else if (path.startsWith("/peers/") && request.method === "DELETE") {
        response = await handleUnregister(env, decodeURIComponent(path.slice(7)));
      } else if (path === "/health" && request.method === "GET") {
        response = Response.json({ status: "ok", time: Date.now() });
      } else {
        response = Response.json({ error: "not found" }, { status: 404 });
      }

      return corsResponse(response);
    } catch (err) {
      console.error("unhandled error", err);
      return corsResponse(
        Response.json(
          { error: "internal server error", detail: err instanceof Error ? err.message : String(err) },
          { status: 500 },
        ),
      );
    }
  },
};

// ---- handlers ----

async function handleAnnounce(request: Request, env: Env): Promise<Response> {
  const body = await parseJsonSafe(request);
  if (body instanceof Response) return body;

  const err = validateAnnouncement(body);
  if (err) return Response.json({ error: err }, { status: 400 });

  const announcement = body as PeerAnnouncement;
  const { peerRecord, ttlSeconds } = await announcePeer(env, announcement);

  return Response.json({
    success: true,
    ttlSeconds,
    peer: peerRecord,
  });
}

async function handleLookup(env: Env, peerId: string): Promise<Response> {
  if (!peerId) return Response.json({ error: "peerId required" }, { status: 400 });
  const record = await lookupPeer(env, peerId);
  if (!record) return Response.json({ error: "peer not found" }, { status: 404 });

  // Check live signaling connection.
  const stub = env.PEER_SIGNALING.getByName(peerId);
  const online = await stub.isOnline();
  return Response.json({ ...record, online });
}

async function handleListPeers(env: Env): Promise<Response> {
  const peers = await listPeers(env);
  return Response.json({ count: peers.length, peers });
}

async function handleUnregister(env: Env, peerId: string): Promise<Response> {
  if (!peerId) return Response.json({ error: "peerId required" }, { status: 400 });
  const removed = await unregisterPeer(env, peerId);
  return Response.json({ success: removed, peerId });
}

async function handleWebSocket(request: Request, env: Env, peerId: string): Promise<Response> {
  if (!peerId) return new Response("peerId required", { status: 400 });
  const stub = env.PEER_SIGNALING.getByName(peerId);
  // Forward the upgrade request to the peer's DO, passing peerId via header.
  const doRequest = new Request(request.url, {
    method: request.method,
    headers: new Headers(request.headers),
  });
  doRequest.headers.set("X-Peer-Id", peerId);
  return stub.fetch(doRequest);
}

// ---- helpers ----

async function parseJsonSafe(request: Request): Promise<unknown | Response> {
  const ct = request.headers.get("content-type") || "";
  if (!ct.includes("application/json")) {
    return Response.json({ error: "content-type must be application/json" }, { status: 400 });
  }
  try {
    return await request.json();
  } catch {
    return Response.json({ error: "invalid JSON body" }, { status: 400 });
  }
}

function corsResponse(response: Response): Response {
  response.headers.set("Access-Control-Allow-Origin", "*");
  response.headers.set("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS");
  response.headers.set("Access-Control-Allow-Headers", "Content-Type");
  return response;
}
