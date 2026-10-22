import threading
import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    """In-memory, per-process limiter. Use a shared store (e.g. Redis) if you run several workers."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, window: float, now: float) -> deque[float]:
        q = self._hits[key]
        while q and now - q[0] > window:
            q.popleft()
        if not q:
            self._hits.pop(key, None)
        return q

    def count(self, key: str, window: float) -> int:
        with self._lock:
            return len(self._prune(key, window, time.monotonic()))

    def add(self, key: str) -> None:
        with self._lock:
            self._hits[key].append(time.monotonic())

    def clear(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def allow(self, key: str, limit: int, window: float = 60.0) -> bool:
        """Record a hit and return False if the limit is exceeded."""
        with self._lock:
            now = time.monotonic()
            q = self._prune(key, window, now)
            if len(q) >= limit:
                return False
            self._hits[key].append(now)
            return True


login_limiter = SlidingWindowLimiter()
device_limiter = SlidingWindowLimiter()
chat_limiter = SlidingWindowLimiter()
