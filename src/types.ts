// Shared type definitions for the P2P addressing service.

/** Information a peer announces about itself. */
export interface PeerAnnouncement {
  /** Stable identifier for the peer. */
  peerId: string;
  /** Reachable address (IP or hostname). */
  address: string;
  /** Listening port. */
  port: number;
  /** Optional free-form metadata (e.g. capabilities, version). */
  metadata?: Record<string, unknown>;
}

/** Stored peer record returned by lookups. */
export interface PeerRecord extends PeerAnnouncement {
  /** Epoch ms when the record was last announced. */
  lastSeen: number;
  /** Epoch ms when the record expires. */
  expiresAt: number;
}

/** Signaling message relayed between peers over WebSocket. */
export interface SignalingMessage {
  type: "offer" | "answer" | "ice-candidate" | "bye" | string;
  /** Sender peer id (set by the server). */
  from?: string;
  /** Recipient peer id. */
  to: string;
  /** Arbitrary payload (SDP, ICE candidate, etc.). */
  data?: unknown;
}

/** A relayed message envelope stored for offline peers. */
export interface RelayedMessage extends SignalingMessage {
  id: string;
  timestamp: number;
}
