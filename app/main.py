"""
DeepSeek reasoning_content 代理服务 - 主入口
- 代理端点: /v1/chat/completions
- WebUI 管理界面: /
- 单进程同时服务于两个端口（通过 docker-compose 端口映射实现）
"""
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from cache import cache
from config import AppConfig

# ── 日志 ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("ds-proxy")

# ── 请求日志环形缓冲 ─────────────────────────────────
MAX_LOG_ENTRIES = 200
request_logs: list[dict] = []


def add_log(entry: dict):
    request_logs.append(entry)
    if len(request_logs) > MAX_LOG_ENTRIES:
        request_logs.pop(0)


# ── 生命周期 ────────────────────────────────────────────


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info("DS Proxy 启动")
    yield
    logger.info("DS Proxy 关闭")


app = FastAPI(title="DeepSeek Reasoning Proxy", lifespan=lifespan)

# CORS 允许所有来源（前后端分离场景）
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载静态文件目录，提供前端 UI
app.mount("/static", StaticFiles(directory="static", html=True), name="static")

# 全局 HTTP 客户端（连接复用，长超时应对流式）
client = httpx.AsyncClient(timeout=120.0)


# ══════════════════════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════════════════════


def extract_reasoning_from_response(data: dict) -> Optional[str]:
    """从非流式响应中提取 reasoning_content"""
    choices = data.get("choices", [])
    if choices:
        # choices[0] 可能是 message 或 delta
        msg = choices[0].get("delta") or choices[0].get("message", {})
        return msg.get("reasoning_content")
    return None


def extract_reasoning_from_chunk(chunk_text: str) -> Optional[str]:
    """从 SSE 数据块中提取 reasoning_content"""
    for line in chunk_text.split("\n"):
        line = line.strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if payload in ("[DONE]", ""):
            continue
        try:
            data = json.loads(payload)
            delta = data.get("choices", [{}])[0].get("delta", {})
            rc = delta.get("reasoning_content")
            if rc:
                return rc
        except (json.JSONDecodeError, IndexError, KeyError):
            pass
    return None


def check_and_inject_reasoning(body: dict) -> dict:
    """
    检查缓存，如果有 reasoning_content 则注入到 messages 中。
    在最后一个 user 消息之前插入一条 assistant 消息（携带 reasoning_content）。
    这样上游看到的对话历史就包含了之前的 thinking 过程。
    """
    session_id = cache.get_session_id(body)
    cached = cache.get(session_id)
    if not cached:
        return body

    messages = body.get("messages", [])
    if not messages:
        return body

    # 从后往前找最后一个 user 消息的位置
    last_user_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            last_user_idx = i
            break

    if last_user_idx is None:
        return body

    # 构建带 reasoning_content 的 assistant 消息
    assistant_msg = {
        "role": "assistant",
        "content": "",
        "reasoning_content": cached,
    }
    new_messages = list(messages)
    new_messages.insert(last_user_idx, assistant_msg)
    body = dict(body)
    body["messages"] = new_messages
    logger.info("✓ 注入 reasoning_content  len=%d  session=%s...", len(cached), session_id[:8])
    return body


def _build_headers(request: Request) -> dict:
    """构建转发请求头——保留 Authorization 等关键头部"""
    forwarded = {}
    for key in ("authorization", "content-type", "x-api-key"):
        val = request.headers.get(key)
        if val:
            forwarded[key] = val
    return forwarded


def build_error_response(status: int, msg: str) -> JSONResponse:
    """统一错误响应格式"""
    return JSONResponse(
        {"error": {"message": msg, "type": "proxy_error", "code": status}},
        status_code=status,
    )


# ══════════════════════════════════════════════════════
# 核心代理
# ══════════════════════════════════════════════════════


async def proxy_streaming(request: Request, body: dict, upstream_url: str):
    """流式转发——逐块透传，缓存首次出现的 reasoning_content"""
    headers = _build_headers(request)
    session_id = cache.get_session_id(body)
    cached_first = False

    try:
        async with client.stream("POST", upstream_url, json=body, headers=headers) as resp:
            add_log({
                "time": time.strftime("%H:%M:%S"),
                "status": resp.status_code,
                "method": "POST",
                "error": "" if resp.status_code == 200 else f"上游返回 {resp.status_code}",
            })

            if resp.status_code != 200:
                error_body = await resp.aread()
                return Response(
                    content=error_body,
                    status_code=resp.status_code,
                    headers={k: v for k, v in resp.headers.items() if k.lower() not in ("content-length", "transfer-encoding", "content-encoding")},
                    media_type=resp.headers.get("content-type", "application/json"),
                )

            async def _stream():
                nonlocal cached_first
                async for chunk in resp.aiter_text():
                    if not cached_first:
                        rc = extract_reasoning_from_chunk(chunk)
                        if rc:
                            cache.set(session_id, rc)
                            cached_first = True
                            logger.info("✓ 缓存 reasoning_content  session=%s...", session_id[:8])
                    yield chunk

            return StreamingResponse(
                _stream(),
                status_code=200,
                headers={k: v for k, v in resp.headers.items() if k.lower() not in ("content-length", "transfer-encoding", "content-encoding")},
                media_type="text/event-stream",
            )
    except httpx.TimeoutException:
        add_log({"time": time.strftime("%H:%M:%S"), "status": 0, "method": "POST", "error": "上游超时"})
        return build_error_response(504, "上游请求超时")
    except httpx.ConnectError as e:
        add_log({"time": time.strftime("%H:%M:%S"), "status": 0, "method": "POST", "error": f"连接失败: {e}"})
        return build_error_response(502, f"无法连接到上游: {e}")
    except Exception as e:
        add_log({"time": time.strftime("%H:%M:%S"), "status": 0, "method": "POST", "error": str(e)})
        return build_error_response(500, f"代理错误: {e}")


async def proxy_non_streaming(request: Request, body: dict, upstream_url: str):
    """非流式转发——缓存 reasoning_content 后返回完整响应"""
    headers = _build_headers(request)
    session_id = cache.get_session_id(body)

    try:
        async with client.stream("POST", upstream_url, json=body, headers=headers) as resp:
            data = await resp.aread()
            add_log({
                "time": time.strftime("%H:%M:%S"),
                "status": resp.status_code,
                "method": "POST",
                "error": "" if resp.status_code == 200 else data[:200].decode(errors="replace"),
            })

            if resp.status_code == 200:
                try:
                    resp_json = json.loads(data)
                    rc = extract_reasoning_from_response(resp_json)
                    if rc:
                        cache.set(session_id, rc)
                        logger.info("✓ 缓存 reasoning_content  session=%s...", session_id[:8])
                except json.JSONDecodeError:
                    pass

            return Response(
                content=data,
                status_code=resp.status_code,
                headers={k: v for k, v in resp.headers.items() if k.lower() not in ("content-length", "transfer-encoding", "content-encoding")},
                media_type=resp.headers.get("content-type", "application/json"),
            )
    except httpx.TimeoutException:
        add_log({"time": time.strftime("%H:%M:%S"), "status": 0, "method": "POST", "error": "上游超时"})
        return build_error_response(504, "上游请求超时")
    except httpx.ConnectError as e:
        add_log({"time": time.strftime("%H:%M:%S"), "status": 0, "method": "POST", "error": f"连接失败: {e}"})
        return build_error_response(502, f"无法连接到上游: {e}")
    except Exception as e:
        add_log({"time": time.strftime("%H:%M:%S"), "status": 0, "method": "POST", "error": str(e)})
        return build_error_response(500, f"代理错误: {e}")


# ══════════════════════════════════════════════════════
# API 路由
# ══════════════════════════════════════════════════════


@app.get("/")
async def root_redirect():
    """根路径重定向到静态页面的 index.html"""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/static/index.html")


@app.post("/v1/chat/completions")
async def proxy_chat(request: Request):
    """
    核心代理端点
    1. 解析请求体
    2. 注入缓存的 reasoning_content（如有）
    3. 转发到上游
    4. 缓存上游返回的 reasoning_content
    5. 返回响应
    """
    cfg = AppConfig()
    upstream_url = f"{cfg.upstream_url}/chat/completions"

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return build_error_response(400, "请求体不是有效的 JSON")

    body = check_and_inject_reasoning(body)

    is_stream = body.get("stream", False)
    if is_stream:
        return await proxy_streaming(request, body, upstream_url)
    else:
        return await proxy_non_streaming(request, body, upstream_url)


# ── 管理 API ─────────────────────────────────────────


@app.get("/api/config")
async def get_config():
    """获取当前上游地址配置"""
    return AppConfig().as_dict()


@app.post("/api/config")
async def update_config(data: dict):
    """更新上游地址"""
    url = data.get("upstream_url", "").strip()
    if not url:
        return JSONResponse({"error": "地址不能为空"}, status_code=400)
    if not url.startswith(("http://", "https://")):
        return JSONResponse({"error": "地址必须以 http:// 或 https:// 开头"}, status_code=400)
    cfg = AppConfig()
    cfg.upstream_url = url
    return {"ok": True, "upstream_url": cfg.upstream_url}


@app.get("/api/status")
async def get_status():
    """检查代理运行状态和上游连通性"""
    cfg = AppConfig()
    # 用 TCP 级联检测，不依赖 auth 头
    from urllib.parse import urlparse
    parsed = urlparse(cfg.upstream_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        import asyncio
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=5.0
        )
        writer.close()
        await writer.wait_closed()
        return {"status": "running", "upstream_status": "reachable"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/api/logs")
async def get_logs():
    """返回最近的请求日志"""
    return {"logs": request_logs[-50:]}


@app.post("/api/cache/clear")
async def clear_cache():
    """清空所有 reasoning_content 缓存"""
    count = len(cache._store)
    cache.clear()
    return {"ok": True, "cleared": count}


@app.get("/api/cache/stats")
async def cache_stats():
    """缓存统计信息"""
    return cache.stats()


@app.post("/api/cache/remove")
async def remove_cache(data: dict):
    """移除指定会话的缓存"""
    sid = data.get("session_id", "")
    if sid:
        cache.remove(sid)
    return {"ok": True}