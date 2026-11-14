# p2punch

> Cloudflare Workers 负责「在线人员发现 + 公网 IP / UDP 端点登记」，Python 负责「STUN NAT 探测 + UDP 打洞 + 直连数据通道」。

这是一个可以直接跑起来的最小 P2P 打洞方案：

- **控制面在 Cloudflare Workers**：观察每个节点的公网 IP（`CF-Connecting-IP`）、维护在线状态、按房间返回其他在线节点的 UDP 端点与 NAT 类型。
- **数据面完全 P2P**：业务数据不经过 Cloudflare，双方拿到地址后直接向对方 UDP 端口发包打洞。
- **Python 客户端零第三方依赖**：只用标准库实现 STUN、注册/心跳、UDP 打洞、HMAC 认证和聊天。
- **在线状态带 TTL**：客户端定时心跳，停止心跳后自动离线并从房间移除。

> 适用场景：学习 NAT 穿透、内网联机 Demo、边缘节点发现。**不包含 TURN 中继**；双方都是对称 NAT 时无法直连，见「限制与兜底」。

## 架构

```
┌───────────────┐        ┌──────────────────────────────────┐        ┌───────────────┐
│   Peer A      │        │        Cloudflare Worker         │        │   Peer B      │
│  (Python)     │        │                                  │        │  (Python)     │
│               │  HTTPS │  POST /api/announce              │ HTTPS  │               │
│  STUN 探测 ───┼───────▶│  RoomDO: 房间内 presence + TTL   │◀───────┼─── STUN 探测  │
│  公网 UDP 端点 │        │  - CF-Connecting-IP 观察公网 IP   │        │   公网 UDP 端点│
│               │◀───────│  - 返回 peers[]（IP/端口/NAT）    │───────▶│               │
└──────┬────────┘        └──────────────────────────────────┘        └───────┬───────┘
       │                                                                      │
       └──────────── 认证 UDP：PUNCH / ACK / DATA / PING / BYE ──────────────┘
                              数据不经过 Cloudflare
```

一次完整的连接过程：

1. 客户端绑定一个 UDP socket，向多个 STUN 服务器查询自己的公网 `IP:端口`，并比较不同目的地返回的映射，判断是锥形 NAT 还是对称 NAT。
2. 客户端 `POST /api/announce` 注册：提交 UDP 公网端点、局域网端点、NAT 类型和一个用于打洞认证的 `punchKey`。Worker 同时记录它看到的 HTTP 公网 IP（`CF-Connecting-IP`）。
3. 任意一方再次 announce / 查询 peers 时，拿到对方的 `observedIp`、`publicEndpoint`、`natType`、`punchKey`。
4. 双方同时向对方的候选端点发送带 HMAC 标签的 `PUNCH`。任何收到的合法包都会让本端学习到对方**真实的源地址**（这是对称 NAT 场景下唯一可用的端口）。
5. 收到 `PUNCH` 回 `ACK`，双方进入 `CONNECTED`，之后用 `DATA` 直接传数据；每 10 秒 `PING` 保活。
6. 每 15 秒 announce 一次续约；45 秒没有心跳就从房间移除。

## 快速开始

### 1. 部署 Worker

```bash
npm install
npx wrangler login
npx wrangler deploy
```

部署后会得到一个 `https://p2punch.<your-subdomain>.workers.dev` 地址。

可选：给 API 加一层共享密钥（推荐公网部署时启用）：

```bash
npx wrangler secret put ROOM_SECRET
# 之后所有 /api/* 请求都需要带 X-Room-Secret: <值>
```

本地开发：

```bash
npm run dev
# http://127.0.0.1:8787
```

### 2. 安装 Python 客户端

要求 Python 3.9+，无第三方依赖。

```bash
# 方式一：安装为命令
cd client
pip install -e .
p2punch --help

# 方式二：不安装，直接用源码
PYTHONPATH=client/src python -m p2punch --help
```

## 使用

### 查看自己的公网 IP 和 NAT 类型

```bash
p2punch ip --server https://p2punch.xxx.workers.dev
```

输出示例：

```
[12:00:01] observed HTTP IP: 203.0.113.10 (colo SJC, country US)
[12:00:02] public UDP endpoint: 203.0.113.10:54321
[12:00:02] NAT type: cone (endpoint-independent mapping (restricted filtering))
  stun.cloudflare.com:3478 -> 203.0.113.10:54321  (24 ms)
  stun.l.google.com:19302 -> 203.0.113.10:54321  (31 ms)
```

- `observed HTTP IP`：Cloudflare 看到的 TCP/HTTP 公网 IP。
- `public UDP endpoint`：STUN 看到的 UDP 公网 `IP:端口`，打洞用的是这个。
- `NAT type`：`cone` / `full-cone` / `symmetric` / `unknown`。

### 列出一个房间里的在线节点

```bash
p2punch peers --room my-room --id alice --server https://p2punch.xxx.workers.dev
```

输出会包含每个节点的观察 IP、公网 UDP 端点、局域网端点和 NAT 类型。

### 常驻在线并连接另一个节点

终端 A：

```bash
p2punch run --room my-room --id alice --server https://p2punch.xxx.workers.dev
```

终端 B：

```bash
p2punch run --room my-room --id bob --server https://p2punch.xxx.workers.dev --peer alice
```

连接成功后直接输入文字回车发送。交互命令：

| 命令 | 说明 |
|------|------|
| `/peers` | 列出当前在线节点 |
| `/connect <id>` | 连接指定节点（会断开旧连接） |
| `/disconnect` | 断开当前连接 |
| `/whoami` | 显示本机 ID、房间、UDP 端口 |
| `/quit` | 退出（自动 leave） |

适合脚本/CI 的非交互用法：

```bash
# 连接 bob，发送一条消息，保持 5 秒接收回复后退出
p2punch run --room my-room --id alice --peer bob \
  --send "hello from alice" --wait 5 --no-stdin
```

### 本机回环端到端测试（不需要 STUN）

```bash
# 终端 0
npm run dev

# 终端 1：先启动 bob，等待 alice
PYTHONPATH=client/src python -m p2punch run \
  --server http://127.0.0.1:8787 --room e2e --id bob \
  --bind 127.0.0.1 --no-stun --peer alice --wait 12 --no-stdin

# 终端 2：启动 alice，连接 bob 并发送消息
PYTHONPATH=client/src python -m p2punch run \
  --server http://127.0.0.1:8787 --room e2e --id alice \
  --bind 127.0.0.1 --no-stun --peer bob \
  --send "hello-from-alice" --wait 5 --no-stdin
```

bob 的终端会打印：

```
<alice> hello-from-alice
```

## Worker API

所有 `/api/*` 接口返回 JSON；错误格式为 `{"ok": false, "error": "..."}`。

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/health` | 健康检查 |
| `GET` | `/api/whoami` | 返回调用方被观察到的公网 IP、国家、colo，不需要注册 |
| `POST` | `/api/announce` | 注册/心跳，返回当前在线的其他节点 |
| `GET` | `/api/peers?room=<room>` | 列出房间内在线节点（需要节点认证头） |
| `POST` | `/api/leave` | 主动下线 |

认证方式：

- `POST /api/announce` 的 JSON body 里带 `peerId` + `secret`（首次注册时建立绑定，之后同 `peerId` 必须用同一个 `secret`）。
- `GET /api/peers` 使用请求头 `X-Peer-Id`、`X-Peer-Secret`。
- 如果 Worker 配置了 `ROOM_SECRET`，所有 `/api/*` 还要带 `X-Room-Secret`。

### announce 请求

```bash
curl -X POST https://p2punch.xxx.workers.dev/api/announce \
  -H 'Content-Type: application/json' \
  -d '{
    "room": "my-room",
    "peerId": "alice",
    "secret": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "punchKey": "0123456789abcdef0123456789abcdef",
    "natType": "cone",
    "publicEndpoint": {"host": "203.0.113.10", "port": 54321, "family": 4},
    "localEndpoint":  {"host": "192.168.1.10", "port": 54321, "family": 4},
    "metadata": {"version": "0.2"}
  }'
```

响应（节选）：

```json
{
  "ok": true,
  "room": "my-room",
  "self": {
    "peerId": "alice",
    "observedIp": "203.0.113.10",
    "publicEndpoint": {"host": "203.0.113.10", "port": 54321, "family": 4},
    "natType": "cone",
    "punchKey": "0123456789abcdef0123456789abcdef"
  },
  "peers": [
    {
      "peerId": "bob",
      "observedIp": "198.51.100.20",
      "publicEndpoint": {"host": "198.51.100.20", "port": 62000, "family": 4},
      "localEndpoint": {"host": "10.0.0.5", "port": 62000, "family": 4},
      "natType": "symmetric",
      "punchKey": "fedcba9876543210fedcba9876543210"
    }
  ],
  "ttlSeconds": 45,
  "heartbeatSeconds": 15
}
```

`peers[]` 只包含**其他**节点；`online` 字段恒为 `true`，因为离线节点已经被 TTL 清理。

## NAT 组合与成功率

| 本端 \ 对端 | 锥形 NAT | 对称 NAT |
|-------------|----------|----------|
| **锥形 NAT** | 通常成功 | 通常成功（对端会从真实源端口发包，本端学习后回复） |
| **对称 NAT** | 通常成功 | **基本失败**，需要中继或 IPv6 |

为什么「锥形 + 对称」通常能成功：对称 NAT 向对端发包时会使用一个**新的映射端口**，它注册在 Worker 上的 STUN 端口对这条连接无效。但对端向这个无效端口发包时，对称 NAT 会丢弃；同时对称 NAT 自己向对端的合法端口发包，对端收到后从包源地址学习到真实端口，随后回复到真实端口即可双向通信。

如果两端都是对称 NAT，双方都没有一个可被对方预测/学习的端口，纯打洞无解。

## 安全与隐私

- **房间码就是访问边界**：任何知道房间码的人都能看到房间内节点的公网 IP 和 UDP 端点。请使用足够长的随机房间码（例如 `secrets.token_urlsafe(24)`），公网部署时再启用 `ROOM_SECRET`。
- Worker 只保存 `secret` 的 SHA-256，不保存明文；客户端把每个 `(server, room, peerId)` 的 secret 存在 `~/.p2punch/state.json`，权限 `0600`。
- UDP 包用双方 `punchKey` 派生出的会话密钥做 HMAC-SHA256 截断认证，可防伪造/注入；**但不加密 payload**，它是明文 UDP。需要机密性请在应用层再加一层加密（如 Noise/DTLS）。
- 生产环境建议在 Cloudflare 侧加 Cloudflare Access、WAF 规则或速率限制；Worker 本身只做了字段校验和房间容量限制，没有做通用限流。

## 限制与兜底

- **没有 TURN 中继**：本项目的目标是演示「地址发现 + 打洞」。对称 NAT 对对称 NAT 需要 TURN/中继。
- **UDP 被封**：部分公司网络/移动网络禁止出站 UDP，STUN 和打洞都会失败；可换网络或用中继。
- **数据报上限 1100 字节**：`DATA` 一个包一条消息，不做分片、重传或有序保证；适合短消息/控制信令。要传文件请在应用层做分片和可靠传输。
- **单连接**：一个进程同一时间只维持一条 UDP 会话；`/connect` 会先关闭旧会话。
- **在线状态是临时的**：Durable Object 被回收后内存表会丢失，客户端下一次心跳会重新注册。
- **IPv6**：`--family 6` 会尝试 IPv6 的 STUN 与打洞；双方必须使用同一地址族（IPv4 客户端连不上 IPv6 端点）。IPv6 直连可以完全绕过 NAT。
- **HTTP IP ≠ UDP 端点**：Worker 看到的 `observedIp` 是 HTTP 公网 IP；打洞必须使用 STUN 得到的 `publicEndpoint`。STUN 失败时客户端会退化为登记局域网端点。
- **STUN 服务器**：默认使用 Cloudflare/Google 的公共 STUN；如果被屏蔽，用 `--stun-server host:port` 指定自己的 STUN。

### 对称 NAT 的兜底方案

1. **TURN 中继**（标准做法）：部署 coturn，客户端通过 TURN 分配 relay 地址，双方都连中继。
2. **公网节点应用层中转**：例如让双方都连同一个有公网 IP 的 WebSocket/TCP 服务器。
3. **IPv6**：双方都有公网 IPv6 时直接连接。
4. **UPnP / NAT-PMP / PCP**：向路由器申请端口映射（需要路由器支持并授权）。

本仓库不实现 TURN；如果要把 Demo 变成生产级 P2P，建议直接使用 WebRTC（ICE + TURN）或 libp2p（内置 NAT 探测、打洞和 relay）。

## 测试

```bash
# Python 单元测试（协议、STUN、打洞、注册客户端）
PYTHONPATH=client/src python -m unittest discover -s client/tests -v

# Worker 类型检查与打包检查
npm run typecheck
npx wrangler deploy --dry-run
```

单元测试覆盖：

- 数据包编解码、HMAC 篡改检测、超大包/截断包拒绝
- 假 STUN 服务器的锥形/对称 NAT 判定、IPv6 XOR-MAPPED-ADDRESS 解析
- 真实 UDP socket 上的打洞握手、源地址学习、BYE 断开
- 假 HTTP 服务上的 announce / peers / leave / 错误 secret

## 项目结构

```
p2punch/
├── src/                        # Cloudflare Worker（控制面）
│   ├── index.ts                # HTTP 路由、参数校验、CF-Connecting-IP
│   ├── room.ts                 # 每个房间一个 Durable Object：presence + TTL
│   ├── types.ts                # API 类型
│   └── env.ts                  # Worker 绑定
├── wrangler.jsonc              # Worker/DO 配置
├── package.json
├── client/                     # Python P2P 客户端（数据面）
│   ├── pyproject.toml
│   ├── src/p2punch/
│   │   ├── stun.py             # STUN 客户端 + NAT 类型探测
│   │   ├── rendezvous.py       # Worker HTTP 客户端
│   │   ├── protocol.py         # UDP 包格式 + 会话密钥
│   │   ├── connection.py       # 打洞状态机 + 保活 + 数据收发
│   │   └── cli.py              # ip / peers / run 命令
│   └── tests/                  # unittest（19 个用例）
└── README.md
```

## License

MIT
