// Test: two peers exchange signaling messages via the P2P addressing Worker.
// Run with: node test-signaling.mjs

const BASE = "ws://localhost:8787";

function connectPeer(peerId) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(`${BASE}/ws/${peerId}`);
    ws.addEventListener("open", () => resolve(ws));
    ws.addEventListener("error", reject);
    setTimeout(() => reject(new Error(`${peerId} connect timeout`)), 5000);
  });
}

function waitForMessage(ws, timeout = 5000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("message timeout")), timeout);
    const handler = (event) => {
      clearTimeout(timer);
      ws.removeEventListener("message", handler);
      resolve(JSON.parse(event.data.toString()));
    };
    ws.addEventListener("message", handler);
  });
}

async function main() {
  console.log("Connecting peer-1 and peer-2...");
  const peer1 = await connectPeer("peer-1");
  const peer2 = await connectPeer("peer-2");
  console.log("Both peers connected.\n");

  // peer-1 sends an SDP offer to peer-2
  console.log("peer-1 -> peer-2: offer");
  peer1.send(JSON.stringify({ type: "offer", to: "peer-2", data: { sdp: "v=0..." } }));

  const offer = await waitForMessage(peer2);
  console.log("peer-2 received:", JSON.stringify(offer));
  console.assert(offer.type === "offer", "should be offer");
  console.assert(offer.from === "peer-1", "from should be peer-1");
  console.assert(offer.data.sdp === "v=0...", "sdp should match");

  // peer-2 sends an answer back
  console.log("\npeer-2 -> peer-1: answer");
  peer2.send(JSON.stringify({ type: "answer", to: "peer-1", data: { sdp: "v=0 answer..." } }));

  const answer = await waitForMessage(peer1);
  console.log("peer-1 received:", JSON.stringify(answer));
  console.assert(answer.type === "answer", "should be answer");
  console.assert(answer.from === "peer-2", "from should be peer-2");

  // peer-2 sends an ICE candidate
  console.log("\npeer-2 -> peer-1: ice-candidate");
  peer2.send(JSON.stringify({ type: "ice-candidate", to: "peer-1", data: { candidate: "candidate:..." } }));
  const ice = await waitForMessage(peer1);
  console.log("peer-1 received:", JSON.stringify(ice));
  console.assert(ice.type === "ice-candidate", "should be ice-candidate");

  peer1.close();
  peer2.close();
  console.log("\n✅ All WebSocket signaling tests passed!");
}

main().catch((err) => {
  console.error("❌ Test failed:", err.message);
  process.exit(1);
});
