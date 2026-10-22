import threading
import time
from collections import OrderedDict
from collections.abc import Hashable
from typing import Final


class _Miss:
    pass


MISS: Final = _Miss()


class TTLCache:
    """Small thread-safe in-memory cache with per-entry expiry and LRU eviction."""

    def __init__(self, maxsize: int) -> None:
        self.maxsize: int = maxsize
        self._data: OrderedDict[Hashable, tuple[float, object]] = OrderedDict()
        self._lock: threading.Lock = threading.Lock()

    def get(self, key: Hashable) -> object:
        with self._lock:
            entry: tuple[float, object] | None = self._data.get(key)
            if entry is None:
                return MISS
            expires, value = entry
            if expires <= time.monotonic():
                del self._data[key]
                return MISS
            self._data.move_to_end(key)
            return value

    def set(self, key: Hashable, value: object, ttl: float) -> None:
        with self._lock:
            self._data[key] = (time.monotonic() + ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)
