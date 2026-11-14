import type { RoomDO } from "./room";

export interface Env {
  ROOMS: DurableObjectNamespace<RoomDO>;
  PEER_TTL_SECONDS?: string;
  MAX_PEERS_PER_ROOM?: string;
  /** Optional shared secret. When set, /api/* requires X-Room-Secret. */
  ROOM_SECRET?: string;
}
