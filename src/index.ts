// p2punch rendezvous Worker.
//
// Responsibilities:
//   - Observe each peer's public IP (CF-Connecting-IP)
//   - Keep an online/offline presence table per room (Durable Object)
//   - Return the STUN-derived UDP endpoint + NAT type of every online peer
//
// The data plane never touches Cloudflare: peers use this metadata to perform
// UDP hole punching directly.

import type { Env } from "./env";
import type { AnnounceRequest, Endpoint } from "./types";

export { RoomDO } from "./room";

const MAX_BODY_BYTES = 16 * 1024;
const ID_RE = /^[A-Za-z0-9._-]{1,64}$/;
const SECRET_RE = /^[A-Za-z0-9_-]{32,128}$/;
const PUNCH_KEY_RE = /^[A-Za-z0-9_-]{16,128}$/;
const NAT_TYPE_RE = /^[A-Za-z0-9._-]{1,32}$/;

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;

    if (request.method === "OPTIONS") {
      return withCors(new Response(null, { status: 204 }));
    }

    try {
      if (path === "/health" && request.method === "GET") {
        return withCors(json({ status: "ok", time: Date.now() }));
      }

      if (path === "/" && request.method === "GET") {
        return withCors(
          json({
            name: "p2punch",
            description: "rendezvous for UDP hole punching",
            endpoints: {
              "GET /health": "health check",
              "GET /api/whoami": "your observed public IP (no registration)",
              "POST /api/announce": "register / heartbeat, returns online peers",
              "GET /api/peers?room=...": "list online peers in a room",
              "POST /api/leave": "remove yourself from a room",
            },
          }),
        );
      }

      if (path === "/api/whoami" && request.method === "GET") {
        return withCors(handleWhoami(request));
      }

      if (path.startsWith("/api/") && env.ROOM_SECRET) {
        const provided = request.headers.get("X-Room-Secret") || "";
        if (!timingSafeEqual(provided, env.ROOM_SECRET)) {
          return withCors(json({ ok: false, error: "invalid room secret" }, 401));
        }
      }

      if (path === "/api/announce" && request.method === "POST") {
        return withCors(await handleAnnounce(request, env));
      }
      if (path === "/api/peers" && request.method === "GET") {
        return withCors(await handlePeers(request, env, url));
      }
      if (path === "/api/leave" && request.method === "POST") {
        return withCors(await handleLeave(request, env));
      }

      return withCors(json({ ok: false, error: "not found" }, 404));
    } catch (err) {
      console.error("unhandled error", err);
      return withCors(json({ ok: false, error: "internal server error" }, 500));
    }
  },
};

// ---- handlers ----

function handleWhoami(request: Request): Response {
  const ray = request.headers.get("CF-Ray") || "";
  return json({
    ok: true,
    ip: observedIp(request),
    country: request.headers.get("CF-IPCountry") || null,
    colo: ray.includes("-") ? ray.split("-").pop() : null,
    ray: ray || null,
  });
}

async function handleAnnounce(request: Request, env: Env): Promise<Response> {
  const body = await readJson(request);
  if (body instanceof Response) return body;

  const error = validateAnnounce(body);
  if (error) return json({ ok: false, error }, 400);

  const req = body as unknown as AnnounceRequest;
  const room = env.ROOMS.getByName(req.room);
  const result = await room.announce(req, observedIp(request));
  if (!result.ok) return json({ ok: false, error: result.error }, result.status);
  return json(result);
}

async function handlePeers(request: Request, env: Env, url: URL): Promise<Response> {
  const roomName = url.searchParams.get("room") || "";
  const peerId = request.headers.get("X-Peer-Id") || "";
  const secret = request.headers.get("X-Peer-Secret") || "";

  if (!ID_RE.test(roomName)) return json({ ok: false, error: "invalid room" }, 400);
  if (!ID_RE.test(peerId)) return json({ ok: false, error: "invalid X-Peer-Id header" }, 400);
  if (!SECRET_RE.test(secret)) return json({ ok: false, error: "invalid X-Peer-Secret header" }, 400);

  const room = env.ROOMS.getByName(roomName);
  const result = await room.list(roomName, peerId, secret);
  if (!result.ok) return json({ ok: false, error: result.error }, result.status);
  return json(result);
}

async function handleLeave(request: Request, env: Env): Promise<Response> {
  const body = await readJson(request);
  if (body instanceof Response) return body;

  const obj = body as Record<string, unknown>;
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) {
    return json({ ok: false, error: "body must be a JSON object" }, 400);
  }
  if (typeof obj.room !== "string" || !ID_RE.test(obj.room)) {
    return json({ ok: false, error: "invalid room" }, 400);
  }
  if (typeof obj.peerId !== "string" || !ID_RE.test(obj.peerId)) {
    return json({ ok: false, error: "invalid peerId" }, 400);
  }
  if (typeof obj.secret !== "string" || !SECRET_RE.test(obj.secret)) {
    return json({ ok: false, error: "invalid secret" }, 400);
  }

  const room = env.ROOMS.getByName(obj.room);
  const result = await room.leave(obj.peerId, obj.secret);
  if (!result.ok) return json({ ok: false, error: result.error }, result.status);
  return json(result);
}

// ---- validation ----

function validateAnnounce(body: unknown): string | null {
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return "body must be a JSON object";
  }
  const obj = body as Record<string, unknown>;

  if (typeof obj.room !== "string" || !ID_RE.test(obj.room)) {
    return "room must match [A-Za-z0-9._-]{1,64}";
  }
  if (typeof obj.peerId !== "string" || !ID_RE.test(obj.peerId)) {
    return "peerId must match [A-Za-z0-9._-]{1,64}";
  }
  if (typeof obj.secret !== "string" || !SECRET_RE.test(obj.secret)) {
    return "secret must be 32-128 chars of [A-Za-z0-9_-]";
  }
  if (typeof obj.punchKey !== "string" || !PUNCH_KEY_RE.test(obj.punchKey)) {
    return "punchKey must be 16-128 chars of [A-Za-z0-9_-]";
  }
  if (typeof obj.natType !== "string" || !NAT_TYPE_RE.test(obj.natType)) {
    return "natType must match [A-Za-z0-9._-]{1,32}";
  }

  for (const field of ["publicEndpoint", "localEndpoint"] as const) {
    const value = obj[field];
    if (value === null || value === undefined) continue;
    const endpointError = validateEndpoint(value);
    if (endpointError) return `${field}: ${endpointError}`;
  }

  if (obj.metadata !== undefined && obj.metadata !== null) {
    if (typeof obj.metadata !== "object" || Array.isArray(obj.metadata)) {
      return "metadata must be an object";
    }
    if (JSON.stringify(obj.metadata).length > 1024) {
      return "metadata too large (max 1024 bytes)";
    }
  }

  return null;
}

function validateEndpoint(value: unknown): string | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return "must be an object";
  }
  const ep = value as Record<string, unknown>;
  if (typeof ep.host !== "string" || ep.host.length === 0 || ep.host.length > 64) {
    return "host must be a 1-64 char IP literal";
  }
  if (ep.family !== 4 && ep.family !== 6) return "family must be 4 or 6";
  const version = ipVersion(ep.host);
  if (version === 0) return "host must be an IPv4 or IPv6 literal";
  if (version !== ep.family) return `family ${ep.family} does not match host ${ep.host}`;
  if (
    typeof ep.port !== "number" ||
    !Number.isInteger(ep.port) ||
    ep.port < 1 ||
    ep.port > 65535
  ) {
    return "port must be an integer 1-65535";
  }
  return null;
}

function ipVersion(host: string): 4 | 6 | 0 {
  if (isIPv4(host)) return 4;
  if (isIPv6(host)) return 6;
  return 0;
}

function isIPv4(host: string): boolean {
  const parts = host.split(".");
  if (parts.length !== 4) return false;
  return parts.every((part) => /^\d{1,3}$/.test(part) && Number(part) <= 255);
}

function isIPv6(host: string): boolean {
  if (!host.includes(":") || host.length > 45) return false;
  try {
    // URL parses and validates bracketed IPv6 literals.
    const url = new URL(`http://[${host}]/`);
    return url.hostname.length > 2;
  } catch {
    return false;
  }
}

// ---- helpers ----

async function readJson(request: Request): Promise<unknown | Response> {
  const length = Number(request.headers.get("content-length") || "0");
  if (Number.isFinite(length) && length > MAX_BODY_BYTES) {
    return json({ ok: false, error: "body too large" }, 413);
  }
  const text = await request.text();
  if (text.length > MAX_BODY_BYTES) {
    return json({ ok: false, error: "body too large" }, 413);
  }
  try {
    return JSON.parse(text);
  } catch {
    return json({ ok: false, error: "invalid JSON body" }, 400);
  }
}

function observedIp(request: Request): string {
  const direct = request.headers.get("CF-Connecting-IP");
  if (direct) return direct.trim();
  const forwarded = request.headers.get("X-Forwarded-For");
  if (forwarded) return forwarded.split(",")[0].trim();
  return "127.0.0.1";
}

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}

function withCors(response: Response): Response {
  response.headers.set("Access-Control-Allow-Origin", "*");
  response.headers.set("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  response.headers.set(
    "Access-Control-Allow-Headers",
    "Content-Type, X-Peer-Id, X-Peer-Secret, X-Room-Secret",
  );
  response.headers.set("X-Content-Type-Options", "nosniff");
  response.headers.set("Referrer-Policy", "no-referrer");
  return response;
}

function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}
