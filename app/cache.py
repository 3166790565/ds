"""
缓存管理模块
- 内存字典缓存 reasoning_content
- key = session_id（thread_id / conversation_id / 对话根消息hash）
- value = reasoning_content 字符串
- TTL 1 小时，自动过期清理
"""
import time
import hashlib
import json
from typing import Optional


class ReasoningCache:
    """缓存 reasoning_content，线程安全（GIL 保障 dict 操作原子性）"""

    def __init__(self, ttl: int = 3600):
        # ttl: 缓存过期时间，单位秒，默认 1 小时
        self._ttl = ttl
        # _store: {key: (reasoning_content, expiry_timestamp)}
        self._store: dict[str, tuple[str, float]] = {}

    # ---- 会话 ID 生成 ----

    @staticmethod
    def _conversation_root_hash(messages: list) -> str:
        """
        生成稳定的对话标识。
        取 system 消息 + 第一条 user 消息作为「对话根」，
        同一个对话的所有后续轮次都会生成相同的 hash。
        """
        # 找到第一条 user 消息的索引
        first_user_idx = None
        for i, m in enumerate(messages):
            if m.get("role") == "user":
                first_user_idx = i
                break

        if first_user_idx is not None:
            prefix = messages[: first_user_idx + 1]
        else:
            prefix = messages

        # 只保留 role 和 content，忽略其他无关字段
        cleaned = [
            {"role": m["role"], "content": m.get("content", "")}
            for m in prefix
            if m.get("role") in ("user", "assistant", "system")
        ]
        raw = json.dumps(cleaned, sort_keys=True, ensure_ascii=False)
        return hashlib.md5(raw.encode()).hexdigest()

    def get_session_id(self, body: dict) -> str:
        """从请求体中提取会话标识（优先 thread_id / conversation_id）"""
        session_id = (
            body.get("thread_id")
            or body.get("conversation_id")
            or self._conversation_root_hash(body.get("messages", []))
        )
        return session_id

    # ---- 核心读写 ----

    def get(self, session_id: str) -> Optional[str]:
        """获取缓存的 reasoning_content，已过期返回 None"""
        item = self._store.get(session_id)
        if item is None:
            return None
        content, expiry = item
        if time.time() > expiry:
            del self._store[session_id]
            return None
        return content

    def set(self, session_id: str, reasoning_content: str):
        """写入缓存"""
        self._store[session_id] = (reasoning_content, time.time() + self._ttl)

    # ---- 管理操作 ----

    def clear(self):
        """清空所有缓存"""
        self._store.clear()

    def remove(self, session_id: str):
        """移除指定会话的缓存"""
        self._store.pop(session_id, None)

    def stats(self) -> dict:
        """返回缓存统计信息"""
        now = time.time()
        active = sum(1 for _, expiry in self._store.values() if expiry > now)
        expired = len(self._store) - active
        return {
            "total_keys": len(self._store),
            "active": active,
            "expired": expired,
        }


# 全局单例
cache = ReasoningCache()