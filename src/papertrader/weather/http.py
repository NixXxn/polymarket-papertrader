from __future__ import annotations

import httpx


class WeatherHttp:
    def __init__(self, user_agent: str, client: httpx.Client | None = None) -> None:
        self._own = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(15.0),
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        )

    def close(self) -> None:
        if self._own:
            self.client.close()
