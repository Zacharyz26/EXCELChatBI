"""对话上下文的线程安全 LRU 热缓存。"""

from __future__ import annotations

from collections import OrderedDict
from threading import RLock

from packages.session.models import ConversationContext


class ConversationCache:
    """按 conversation_id 缓存最近使用的持久化上下文快照。

    SQLite 始终是真相源；消息或工件发生变化时由 SessionStore 主动失效对应项。
    """

    def __init__(self, capacity: int = 128) -> None:
        if capacity < 1:
            raise ValueError("对话缓存容量必须大于 0")
        self._capacity = capacity
        self._items: OrderedDict[str, ConversationContext] = OrderedDict()
        self._lock = RLock()

    @property
    def capacity(self) -> int:
        """缓存最大条目数。"""
        return self._capacity

    def get(self, conversation_id: str) -> ConversationContext | None:
        """读取并提升为最近使用项；未命中返回 None。"""
        with self._lock:
            value = self._items.get(conversation_id)
            if value is not None:
                self._items.move_to_end(conversation_id)
            return value

    def put(self, context: ConversationContext) -> None:
        """写入快照，超过容量时淘汰最久未使用项。"""
        conversation_id = context.conversation.id
        with self._lock:
            self._items.pop(conversation_id, None)
            self._items[conversation_id] = context
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)

    def invalidate(self, conversation_id: str) -> None:
        """失效一个对话的缓存。"""
        with self._lock:
            self._items.pop(conversation_id, None)

    def clear(self) -> None:
        """清空全部热缓存。"""
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
