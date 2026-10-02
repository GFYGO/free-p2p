# free-p2p

> 基于 Cloudflare Workers 的 P2P 节点寻址与信令服务

`free-p2p` 是一个轻量级的 P2P（Peer-to-Peer）寻址服务，运行在 Cloudflare Workers 边缘网络上。它为 P2P 网络中的节点提供**地址注册/发现**与**实时信令中继**能力，可用于 WebRTC、NAT 穿透、分布式应用等场景。

- **全球边缘部署**：利用 Cloudflare Workers 的 300+ 边缘节点，低延迟就近响应
- **KV 地址注册表**：节点宣告地址带 TTL 自动过期，支持分布式低延迟查询
- **Durable Object 信令**：每个节点一个 DO 实例，管理 WebSocket 连接并实时中继信令消息
- **离线消息队列**：接收方离线时消息暂存，重连后自动投递
- **零依赖运行时**：包体仅 ~10 KiB，gzip 后 ~3 KiB

## 在线演示

- 自定义域名：https://free-p2p.gwl.net.cn/health
- Workers.dev：https://p2p-addressing.gfygo.workers.dev/health

```bash
curl https://free-p2p.gwl.net.cn/health
# {"status":"ok","time":...}
```

## 架构

```
┌─────────────────────────────────────────────────────────┐
│                    Cloudflare Edge                       │
│                                                          │
│  ┌──────────────┐      ┌──────────────────────────────┐  │
│  │   Worker     │─────▶│  KV Namespace (PEER_REGISTRY)│  │
│  │  (路由/REST) │      │  peerId → {address,port,...}  │  │
│  └──────┬───────┘      └──────────────────────────────┘  │
│         │                                                │
│         │ getByName(peerId)                              │
│         ▼                                                │
│  ┌──────────────────────────────────────────────────┐    │
│  │  Durable Object: PeerSignalingDO (每个 peer 一个) │    │
│  │  - 持有该 peer 的 WebSocket 连接                   │    │
│  │  - 中继 offer/answer/ICE candidate                │    │
│  │  - 离线消息队列 (DO Storage)                       │    │
│  └──────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────┘
         ▲                                    ▲
         │                                    │
    ┌────┴─────┐                       ┌──────┴──────┐
    │  Peer A  │  ←── WebRTC P2P ──→   │   Peer B    │
    └──────────┘                       └─────────────┘
```

## 快速开始

### 前置要求

- [Node.js](https://nodejs.org/) 18+
- [Wrangler](https://developers.cloudflare.com/workers/wrangler/) (`npm i -g wrangler`)
- Cloudflare 账号

### 部署

```bash
# 1. 克隆仓库
git clone https://github.com/GFYGO/free-p2p.git
cd free-p2p

# 2. 安装依赖
npm install

# 3. 登录 Cloudflare
wrangler login

# 4. 创建 KV 命名空间（记录返回的 id）
wrangler kv namespace create PEER_REGISTRY

# 5. 把返回的 id 填入 wrangler.jsonc 的 kv_namespaces

# 6. 部署
wrangler deploy
```

### 本地开发

```bash
npm run dev
# 服务启动在 http://localhost:8787
```

## API 参考

### 1. 宣告节点（Announce）

注册或刷新一个节点的网络地址。

```
POST /announce
Content-Type: application/json
```

**请求体：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `peerId` | string | 是 | 节点唯一标识（最长 128 字符） |
| `address` | string | 是 | 可达地址（IP 或主机名） |
| `port` | number | 是 | 监听端口（1-65535） |
| `metadata` | object | 否 | 自定义元数据 |

**示例：**

```bash
curl -X POST https://free-p2p.gwl.net.cn/announce \
  -H "Content-Type: application/json" \
  -d '{
    "peerId": "node-1",
    "address": "192.168.1.10",
    "port": 9000,
    "metadata": { "version": "1.0", "region": "cn" }
  }'
```

**响应：**

```json
{
  "success": true,
  "ttlSeconds": 300,
  "peer": {
    "peerId": "node-1",
    "address": "192.168.1.10",
    "port": 9000,
    "metadata": { "version": "1.0", "region": "cn" },
    "lastSeen": 1790920199390,
    "expiresAt": 1790920499390
  }
}
```

> 节点需在 `ttlSeconds` 内重新宣告，否则记录自动过期删除。

### 2. 查询节点（Lookup）

```
GET /lookup/:peerId
```

```bash
curl https://free-p2p.gwl.net.cn/lookup/node-1
```

**响应：**

```json
{
  "peerId": "node-1",
  "address": "192.168.1.10",
  "port": 9000,
  "metadata": { "version": "1.0", "region": "cn" },
  "lastSeen": 1790920199390,
  "expiresAt": 1790920499390,
  "online": false
}
```

`online` 字段表示该节点当前是否有活跃的 WebSocket 信令连接。

### 3. 列出所有节点

```
GET /peers
```

```bash
curl https://free-p2p.gwl.net.cn/peers
```

**响应：**

```json
{
  "count": 2,
  "peers": [
    { "peerId": "node-1", "address": "192.168.1.10", "port": 9000, ... },
    { "peerId": "node-2", "address": "10.0.0.5", "port": 9001, ... }
  ]
}
```

### 4. 注销节点

```
DELETE /peers/:peerId
```

```bash
curl -X DELETE https://free-p2p.gwl.net.cn/peers/node-1
```

### 5. 健康检查

```
GET /health
```

### 6. WebSocket 信令通道

```
GET /ws/:peerId
```

建立 WebSocket 连接后，节点之间可以互相发送信令消息（SDP offer/answer、ICE candidate 等），服务端自动中继并附加 `from`、`id`、`timestamp` 字段。

**发送消息（节点 A → 节点 B）：**

```json
{
  "type": "offer",
  "to": "node-2",
  "data": { "sdp": "v=0..." }
}
```

**节点 B 收到：**

```json
{
  "type": "offer",
  "to": "node-2",
  "from": "node-1",
  "data": { "sdp": "v=0..." },
  "id": "00ec1b46-d74d-43b2-a3da-0c4e99e0b9cd",
  "timestamp": 1790920252513
}
```

**支持的消息类型：** `offer`、`answer`、`ice-candidate`、`bye` 及自定义类型。

**离线消息：** 若接收方未连接 WebSocket，消息会暂存在 DO 存储中（最多 50 条，5 分钟有效），接收方重连后自动投递。

### WebSocket 客户端示例（Node.js）

```javascript
const ws = new WebSocket("wss://free-p2p.gwl.net.cn/ws/node-1");

ws.addEventListener("open", () => {
  // 向 node-2 发送 WebRTC offer
  ws.send(JSON.stringify({
    type: "offer",
    to: "node-2",
    data: { sdp: "v=0..." }
  }));
});

ws.addEventListener("message", (event) => {
  const msg = JSON.parse(event.data);
  console.log("收到来自", msg.from, "的", msg.type, msg.data);
});
```

## 配置

`wrangler.jsonc` 主要配置项：

| 配置 | 说明 | 默认值 |
|------|------|--------|
| `PEER_TTL_SECONDS` | 节点地址记录的过期时间（秒） | `300` |
| `PEER_REGISTRY` (KV) | 全局地址注册表命名空间 | — |
| `PEER_SIGNALING` (DO) | 信令 Durable Object | — |
| `routes` | 自定义域名路由 | `free-p2p.gwl.net.cn/*` |

### TTL 与心跳

节点地址记录在 KV 中带 TTL，超时自动删除。节点应定期（小于 TTL）重新调用 `/announce` 续约。信令连接本身由 DO 管理，断线即视为离线。

## 项目结构

```
free-p2p/
├── src/
│   ├── index.ts            # Worker 入口：HTTP 路由 + WebSocket 升级
│   ├── peerRegistry.ts     # KV 地址注册表（宣告/查找/列表/注销）
│   ├── PeerSignalingDO.ts  # Durable Object：WebSocket 信令管理
│   ├── env.ts              # 环境绑定类型
│   └── types.ts            # 共享数据类型
├── test-signaling.mjs      # 信令交互测试脚本
├── wrangler.jsonc          # Wrangler 配置
├── tsconfig.json
└── package.json
```

## 测试

```bash
# 本地启动开发服务器
npm run dev

# 运行信令交互测试（需要本地服务在 8787 端口）
node test-signaling.mjs
```

## 技术栈

- **Cloudflare Workers** — 边缘计算运行时
- **Cloudflare KV** — 分布式键值存储（地址注册表）
- **Cloudflare Durable Objects** — 有状态协调（WebSocket 信令）
- **TypeScript** — 类型安全

## License

MIT
