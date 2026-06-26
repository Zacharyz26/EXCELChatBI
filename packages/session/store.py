"""会话状态读写（Redis）。key 按 session_id，设过期。"""

from __future__ import annotations

from packages.session.state import SessionState


class SessionStore:
    """Redis 会话存储。"""

    def __init__(self, host: str, port: int, ttl_seconds: int) -> None:
        self._host = host
        self._port = port
        self._ttl = ttl_seconds

    def load(self, session_id: str) -> SessionState | None:
        """读取会话状态，不存在返回 None。"""
        raise NotImplementedError("TODO: Redis GET + 反序列化")

    def save(self, state: SessionState) -> None:
        """写入会话状态并刷新过期。"""
        raise NotImplementedError("TODO: 序列化 + Redis SET EX ttl")
