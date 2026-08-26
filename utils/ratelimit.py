"""Rate limiting in memoria (sliding window per utente).

Volutamente semplice: nessun Redis, nessuna persistenza. Se il bot riparte il
contatore si azzera - accettabile, serve solo a fermare lo spam sui comandi.

Uso:
    limiter = RateLimiter(max_calls=3, per_seconds=60)
    allowed, retry_after = limiter.hit(user_id)
    if not allowed:
        ...  # avvisa l'utente di riprovare tra retry_after secondi
"""

import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, max_calls: int, per_seconds: int) -> None:
        self.max_calls = max_calls
        self.per_seconds = per_seconds
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    def hit(self, key: int) -> tuple[bool, int]:
        """Registra un tentativo. Ritorna (consentito, secondi_di_attesa)."""
        now = time.monotonic()
        window = self._hits[key]

        # Scarta i colpi usciti dalla finestra temporale.
        while window and now - window[0] > self.per_seconds:
            window.popleft()

        if len(window) >= self.max_calls:
            retry_after = int(self.per_seconds - (now - window[0])) + 1
            return False, retry_after

        window.append(now)
        return True, 0

    def reset(self, key: int) -> None:
        self._hits.pop(key, None)

    def cleanup(self) -> None:
        """Elimina le finestre ormai vuote (evita crescita di memoria)."""
        now = time.monotonic()
        for key in list(self._hits):
            window = self._hits[key]
            while window and now - window[0] > self.per_seconds:
                window.popleft()
            if not window:
                del self._hits[key]
