# DeepSeek Reasoning Proxy

自动缓存并回填 DeepSeek API `reasoning_content`（思考过程）的 HTTP 代理服务。

## 问题背景

- 你有一个上游中转站（例如 `https://api.deepseek.com/v1`），它不处理 DeepSeek 的 "thinking mode" 所需的 `reasoning_content` 回传逻辑。
- 客户端（如 Cursor）发送的请求遵循 OpenAI 协议，不包含 `reasoning_content` 字段。
- 这个代理在客户端和上游之间自动缓存并回填 `reasoning_content`，使得多轮对话能正常显示 DeepSeek 的思考过程。

## 架构

```
客户端 (Cursor 等)                 这个代理                     上游中转站
     │                              │                            │
     │ POST /v1/chat/completions     │                            │
     │─────────────────────────────>│                            │
     │                              │ 注入缓存的 reasoning        │
     │                              │ (如果有之前缓存的思考过程)   │
     │                              │                            │
     │                              │ POST /v1/chat/completions   │
     │                              │───────────────────────────>│
     │                              │                            │
     │                              │ <── 响应 + reasoning_content
     │                              │                            │
     │                              │ 缓存 reasoning_content     │
     │ <── 透传响应 ───────────────│                            │
```

## 快速开始

### 使用 Docker Compose 运行

```bash
# 克隆项目后进入目录
docker compose up -d --build
```

启动后有两组端口映射：

| 宿主机端口 | 说明 |
|-----------|------|
| `8080`    | WebUI 管理界面（浏览器打开 http://localhost:8080） |
| `9000`    | API 代理端点（客户端配置的地址） |

### 客户端配置

将客户端的 API 地址配置为 `http://localhost:9000/v1/chat/completions`。

例如 Cursor -> Settings -> Models -> OpenAI API Key：

- **API Key**: 你的 DeepSeek API Key
- **Base URL**: `http://localhost:9000`
- **Model**: `deepseek-chat`（或其他支持 thinking 的模型）

## WebUI 管理界面

浏览器打开 `http://localhost:8080`：

- **上游地址配置**：修改上游中转站地址（默认 `https://api.deepseek.com/v1`），保存后立即生效
- **运行状态**：显示代理是否正常运行，上游连通性检查
- **缓存统计**：当前缓存的 `reasoning_content` 条目数
- **清空缓存**：手动清除所有缓存的思考过程
- **请求日志**：最近 50 条请求记录

## 文件结构

```
├── docker-compose.yml     # Docker Compose 配置
└── app/
    ├── Dockerfile          # 镜像构建文件
    ├── main.py            # FastAPI 应用（代理逻辑 + 管理 API）
    ├── config.py          # 配置管理（上游地址持久化）
    ├── cache.py           # 内存缓存（reasoning_content 缓存管理）
    ├── requirements.txt   # Python 依赖
    └── static/
        └── index.html     # WebUI 管理界面
```

## 配置持久化

上游地址存储在 `data/config.json`，通过 Docker volume (`ds-proxy-data`) 持久化，重启容器后保留。

## API 端点

### 代理端点
- `POST /v1/chat/completions` — 代理转发，自动注入/缓存 reasoning_content

### 管理 API
| 路径 | 方法 | 说明 |
|------|------|------|
| `/api/config` | GET | 获取上游地址 |
| `/api/config` | POST | 更新上游地址 |
| `/api/status` | GET | 检查运行状态 |
| `/api/logs` | GET | 获取请求日志 |
| `/api/cache/clear` | POST | 清空缓存 |
| `/api/cache/stats` | GET | 缓存统计 |
| `/api/cache/remove` | POST | 移除指定会话缓存 |

## 缓存逻辑

1. 从请求体中提取 `thread_id` 或 `conversation_id`（如果没有，则用 messages 的 MD5 hash）
2. 检查缓存是否有该会话上一次返回的 `reasoning_content`
3. 如果有，注入到 messages 中（在最后一个 user 消息前插入 assistant 消息）
4. 转发到上游
5. 收到响应后，提取 `reasoning_content` 并缓存
6. 缓存 TTL：1 小时

支持流式（SSE）和非流式两种响应模式。