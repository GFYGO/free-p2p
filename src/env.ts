// Environment bindings. In production, regenerate with `wrangler types`.
import type { PeerSignalingDO } from "./PeerSignalingDO";

export interface Env {
  PEER_REGISTRY: KVNamespace;
  PEER_SIGNALING: DurableObjectNamespace<PeerSignalingDO>;
  PEER_TTL_SECONDS: string;
}
