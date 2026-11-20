"""Central error classifier for the startup lifecycle.

Categorises startup exceptions, extracts rate-limit metadata (including Cloudflare 1015
and Retry-After headers), and distinguishes retryable transient failures from fatal ones.
"""

from __future__ import annotations

import asyncio
import enum
import re
import socket
from dataclasses import dataclass
from typing import Any

import aiohttp
import discord


class StartupErrorCategory(str, enum.Enum):
    """Classification categories for startup failures."""

    RATE_LIMITED = "RATE_LIMITED"
    INVALID_TOKEN = "INVALID_TOKEN"
    FORBIDDEN = "FORBIDDEN"
    NETWORK_ERROR = "NETWORK_ERROR"
    DNS_ERROR = "DNS_ERROR"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    DISCORD_GATEWAY_ERROR = "DISCORD_GATEWAY_ERROR"
    DATABASE_ERROR = "DATABASE_ERROR"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    UNKNOWN = "UNKNOWN"


@dataclass
class ClassificationResult:
    """Structured result of classifying a startup error."""

    category: StartupErrorCategory
    is_retryable: bool
    status_code: int | None = None
    retry_after: float | None = None
    is_cloudflare_1015: bool = False
    message: str = ""
    remediation_hint: str = ""


# Regex to sanitize tokens and secrets from exception messages
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_\-\.]{50,}")
_URI_PATTERN = re.compile(r"mongodb(?:\+srv)?://[^\s@]+@[^\s]+")


def _sanitize_message(raw_msg: str) -> str:
    """Strip any accidental tokens or database URIs from log messages."""
    sanitized = _TOKEN_PATTERN.sub("[REDACTED_SECRET]", raw_msg)
    sanitized = _URI_PATTERN.sub("mongodb://[REDACTED_AUTH]@", sanitized)
    return sanitized


def _extract_retry_after(exc: BaseException) -> float | None:
    """Safely extract raw Retry-After numeric value from exception or response headers."""
    import math

    # 1. Direct attribute on discord.RateLimited or discord.HTTPException
    val = getattr(exc, "retry_after", None)
    if val is not None:
        try:
            fval = float(val)
            if not math.isnan(fval) and not math.isinf(fval):
                return fval
        except (ValueError, TypeError):
            pass

    # 2. Check response headers if available
    response = getattr(exc, "response", None)
    if response is not None:
        headers = getattr(response, "headers", None) or {}
        retry_hdr = headers.get("Retry-After") or headers.get("retry-after")
        if retry_hdr:
            try:
                fval = float(retry_hdr)
                if not math.isnan(fval) and not math.isinf(fval):
                    return fval
            except (ValueError, TypeError):
                pass

    return None


def _check_cloudflare_1015(exc: BaseException) -> bool:
    """Detect Cloudflare Error 1015 (rate limit ban) in response body or error string."""
    text_pieces: list[str] = [str(exc)]
    exc_text = getattr(exc, "text", None)
    if exc_text:
        text_pieces.append(str(exc_text))

    combined = " ".join(text_pieces).lower()
    return "1015" in combined and (
        "rate limit" in combined
        or "cloudflare" in combined
        or "banned you temporarily" in combined
    )


def classify_startup_error(exc: BaseException) -> ClassificationResult:
    """Inspect and classify an exception occurring during Discord startup or connection.

    Rule: Only HTTP 429 enters rate-limit recovery. 401/invalid token never endlessly retries.
    """
    raw_msg = _sanitize_message(f"{type(exc).__name__}: {exc}")

    # ── 1. Discord Rate Limited / HTTP 429 ─────────────────────────────────────
    status_code: int | None = None
    if isinstance(exc, discord.HTTPException):
        status_code = exc.status
    elif isinstance(exc, aiohttp.ClientResponseError):
        status_code = exc.status
    elif hasattr(exc, "status"):
        try:
            status_code = int(getattr(exc, "status"))
        except (ValueError, TypeError):
            pass

    if isinstance(exc, discord.RateLimited) or status_code == 429:
        retry_after = _extract_retry_after(exc)
        is_cf_1015 = _check_cloudflare_1015(exc)
        hint = (
            "Discord or Cloudflare is temporarily rate limiting startup requests. "
            "Exponential backoff with jitter is actively maintaining connection cooldown."
        )
        return ClassificationResult(
            category=StartupErrorCategory.RATE_LIMITED,
            is_retryable=True,
            status_code=429,
            retry_after=retry_after,
            is_cloudflare_1015=is_cf_1015,
            message=raw_msg,
            remediation_hint=hint,
        )

    # ── 2. Invalid Token / 401 ────────────────────────────────────────────────
    if isinstance(exc, discord.LoginFailure) or status_code == 401:
        return ClassificationResult(
            category=StartupErrorCategory.INVALID_TOKEN,
            is_retryable=False,
            status_code=401,
            message="Discord authentication failed (401). Bot token was rejected.",
            remediation_hint=(
                "Verify that DISCORD_TOKEN in your hosting environment variables "
                "contains the current Bot Token for this exact Discord application."
            ),
        )

    # ── 3. Forbidden / 403 ────────────────────────────────────────────────────
    if isinstance(exc, discord.Forbidden) or status_code == 403:
        return ClassificationResult(
            category=StartupErrorCategory.FORBIDDEN,
            is_retryable=False,
            status_code=403,
            message="Discord API returned 403 Forbidden.",
            remediation_hint=(
                "Check bot application permissions and verify required Privileged "
                "Gateway Intents (e.g. Message Content, Server Members) in Discord Developer Portal."
            ),
        )

    # ── 4. Discord Gateway / ConnectionClosed ─────────────────────────────────
    if isinstance(exc, discord.ConnectionClosed):
        code = getattr(exc, "code", None)
        if code == 4004:  # Authentication failed
            return ClassificationResult(
                category=StartupErrorCategory.INVALID_TOKEN,
                is_retryable=False,
                status_code=401,
                message=f"Discord gateway closed with 4004: Authentication failed.",
                remediation_hint="Update DISCORD_TOKEN in environment variables.",
            )
        if code == 4014:  # Disallowed intent(s)
            return ClassificationResult(
                category=StartupErrorCategory.FORBIDDEN,
                is_retryable=False,
                status_code=403,
                message=f"Discord gateway closed with 4014: Disallowed intents.",
                remediation_hint="Enable Message Content and Server Members intents in Discord Portal.",
            )
        # Other gateway codes (e.g. 4000, 4008, 1000) are retryable
        return ClassificationResult(
            category=StartupErrorCategory.DISCORD_GATEWAY_ERROR,
            is_retryable=True,
            status_code=code,
            message=f"Discord gateway disconnected (code: {code}).",
            remediation_hint="Gateway reconnectable error. Re-establishing session.",
        )

    # ── 5. DNS Errors ─────────────────────────────────────────────────────────
    if isinstance(exc, (socket.gaierror, socket.herror)) or (
        isinstance(exc, aiohttp.ClientConnectorError)
        and isinstance(getattr(exc, "os_error", None), (socket.gaierror, socket.herror))
    ) or "getaddrinfo failed" in str(exc).lower():
        return ClassificationResult(
            category=StartupErrorCategory.DNS_ERROR,
            is_retryable=True,
            message=raw_msg,
            remediation_hint="DNS resolution failure. Retrying with backoff.",
        )

    # ── 6. Connection Errors ──────────────────────────────────────────────────
    if isinstance(
        exc,
        (
            ConnectionResetError,
            ConnectionRefusedError,
            ConnectionAbortedError,
            ConnectionError,
            aiohttp.ServerDisconnectedError,
            aiohttp.ClientOSError,
        ),
    ):
        return ClassificationResult(
            category=StartupErrorCategory.CONNECTION_ERROR,
            is_retryable=True,
            message=raw_msg,
            remediation_hint="Network connection reset or refused. Re-attempting connection.",
        )

    # ── 7. Network / Timeout Errors ───────────────────────────────────────────
    if (
        isinstance(exc, (asyncio.TimeoutError, TimeoutError, aiohttp.ClientError, discord.GatewayNotFound))
        or (status_code and status_code in (500, 502, 503, 504))
    ):
        return ClassificationResult(
            category=StartupErrorCategory.NETWORK_ERROR,
            is_retryable=True,
            status_code=status_code,
            message=raw_msg,
            remediation_hint="Temporary network or Discord upstream outage. Backing off.",
        )

    # ── 8. Database Errors ────────────────────────────────────────────────────
    exc_module = getattr(type(exc), "__module__", "")
    if "mongo" in exc_module.lower() or "motor" in exc_module.lower():
        return ClassificationResult(
            category=StartupErrorCategory.DATABASE_ERROR,
            is_retryable=True,
            message=raw_msg,
            remediation_hint="MongoDB connection issue. Ticket system will fall back to cache.",
        )

    # ── 9. Configuration Errors ───────────────────────────────────────────────
    if isinstance(exc, (RuntimeError, ValueError)) and (
        "token" in str(exc).lower() or "discord_token" in str(exc).lower()
    ):
        return ClassificationResult(
            category=StartupErrorCategory.CONFIGURATION_ERROR,
            is_retryable=False,
            message=raw_msg,
            remediation_hint="Review hosting environment variables for required configuration.",
        )

    # ── 10. Unknown ───────────────────────────────────────────────────────────
    return ClassificationResult(
        category=StartupErrorCategory.UNKNOWN,
        is_retryable=False,
        message=raw_msg,
        remediation_hint="Unexpected exception encountered during startup.",
    )
