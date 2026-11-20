"""Error types and classification for Discord API interactions."""

from __future__ import annotations

from typing import Optional


class DiscordAPIError(Exception):
    """Base exception for Discord API failures."""


class APIBlockedError(DiscordAPIError):
    """Raised when an operation is attempted while Discord API is globally blocked."""

    def __init__(self, state: str, retry_after: Optional[float] = None) -> None:
        self.state = state
        self.retry_after = retry_after
        super().__init__(
            f"Discord API is currently {state}. Retry after {retry_after}s."
            if retry_after
            else f"Discord API is currently {state}."
        )


class CloudflareBlockError(APIBlockedError):
    """Raised when Cloudflare Error 1015 / rate limit is encountered."""

    def __init__(self, retry_after: Optional[float] = None) -> None:
        super().__init__("CLOUDFLARE_BLOCKED", retry_after)


class DiscordRateLimitError(APIBlockedError):
    """Raised when standard Discord 429 rate limit is encountered."""

    def __init__(self, retry_after: Optional[float] = None) -> None:
        super().__init__("RATE_LIMITED", retry_after)
