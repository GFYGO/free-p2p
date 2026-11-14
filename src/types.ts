// Shared wire types for the rendezvous API.

export interface Endpoint {
  host: string;
  port: number;
  family: 4 | 6;
}

/** Body of POST /api/announce. Also used as the heartbeat. */
export interface AnnounceRequest {
  room: string;
  peerId: string;
  secret: string;
  punchKey: string;
  natType: string;
  publicEndpoint: Endpoint | null;
  localEndpoint: Endpoint | null;
  metadata?: Record<string, unknown> | null;
}

/** What other peers in the room are allowed to see. */
export interface PeerView {
  peerId: string;
  punchKey: string;
  natType: string;
  publicEndpoint: Endpoint | null;
  localEndpoint: Endpoint | null;
  observedIp: string;
  lastSeen: number;
  online: true;
  metadata: Record<string, unknown> | null;
}

export interface AnnounceResponse {
  ok: true;
  room: string;
  self: PeerView;
  peers: PeerView[];
  ttlSeconds: number;
  heartbeatSeconds: number;
  serverTime: number;
}

export interface PeersResponse {
  ok: true;
  room: string;
  peers: PeerView[];
  ttlSeconds: number;
  serverTime: number;
}

export interface LeaveResponse {
  ok: true;
  removed: boolean;
}

export type RpcResult<T> =
  | T
  | { ok: false; status: number; error: string };
