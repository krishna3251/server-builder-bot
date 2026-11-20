"""Global Discord API resilience package."""

from core.discord_api.errors import (
    APIBlockedError,
    CloudflareBlockError,
    DiscordAPIError,
    DiscordRateLimitError,
)
from core.discord_api.guard import DiscordAPIGuard, api_guard
from core.discord_api.health import DiagnosticStatus
from core.discord_api.rate_limit import (
    format_concise_discord_log,
    is_cloudflare_1015,
    parse_retry_after,
    redact_sensitive,
)
from core.discord_api.state import APIState

__all__ = [
    "APIState",
    "DiscordAPIGuard",
    "api_guard",
    "DiagnosticStatus",
    "DiscordAPIError",
    "APIBlockedError",
    "CloudflareBlockError",
    "DiscordRateLimitError",
    "is_cloudflare_1015",
    "parse_retry_after",
    "format_concise_discord_log",
    "redact_sensitive",
]
