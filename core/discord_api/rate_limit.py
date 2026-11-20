"""Cloudflare 1015 detection, Retry-After extraction, and structured logging."""

from __future__ import annotations

import email.utils
import json
import logging
import re
import time
from typing import Any, Mapping, Optional

log = logging.getLogger("core.discord_api.rate_limit")

# ── Cloudflare Patterns ───────────────────────────────────────────────────────
_CF_SIGNATURES = [
    re.compile(r"error\s*code:?\s*1015", re.IGNORECASE),
    re.compile(r"error\s*1015", re.IGNORECASE),
    re.compile(r"you\s*are\s*being\s*rate\s*limited", re.IGNORECASE),
    re.compile(r"cloudflare", re.IGNORECASE),
    re.compile(r"attention\s*required", re.IGNORECASE),
]

_REDACT_PATTERNS = [
    (re.compile(r"(mongodb(?:\+srv)?://)[^@]+@", re.IGNORECASE), r"\1***:***@"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._-]+", re.IGNORECASE), r"\1***REDACTED***"),
    (re.compile(r"(Bot\s+)[A-Za-z0-9._-]+", re.IGNORECASE), r"\1***REDACTED***"),
    (re.compile(r"(DISCORD_TOKEN\s*=\s*)[^\s]+", re.IGNORECASE), r"\1***REDACTED***"),
    (re.compile(r"(api[_-]?key\s*[:=]\s*)[^\s,]+", re.IGNORECASE), r"\1***REDACTED***"),
]


def redact_sensitive(text: str) -> str:
    """Scrub sensitive information such as tokens and database URIs."""
    if not text:
        return ""
    result = text
    for pattern, repl in _REDACT_PATTERNS:
        result = pattern.sub(repl, result)
    return result


def is_cloudflare_1015(
    status: int,
    body: str = "",
    headers: Optional[Mapping[str, str]] = None,
) -> bool:
    """Check if an HTTP response represents a Cloudflare 1015 rate limit."""
    if status != 429 and status != 403:
        return False

    body_str = body or ""
    headers_dict = {str(k).lower(): str(v).lower() for k, v in (headers or {}).items()}

    # Check for explicit 1015 error mentions
    if re.search(r"error\s*code:?\s*1015", body_str, re.IGNORECASE) or re.search(r"\b1015\b", body_str):
        return True

    is_discord_json = body_str.strip().startswith("{") and ("retry_after" in body_str or "global" in body_str)
    if is_discord_json:
        # Standard Discord REST JSON response is not Cloudflare 1015
        return False

    has_cf_ray = "cf-ray" in headers_dict
    has_cf_server = "server" in headers_dict and "cloudflare" in headers_dict["server"]

    # Check for Cloudflare edge block
    if has_cf_ray and has_cf_server and status == 429:
        return True

    # Check for Cloudflare HTML block page
    is_html = "<!doctype html" in body_str.lower() or "<html" in body_str.lower()
    if is_html and (has_cf_server or has_cf_ray or "cloudflare" in body_str.lower()):
        if any(sig.search(body_str) for sig in _CF_SIGNATURES):
            return True

    return False




def parse_retry_after(
    headers: Optional[Mapping[str, str]] = None,
    body: Any = None,
) -> Optional[float]:
    """Extract retry-after in seconds from headers or response body."""
    headers_dict = {str(k).lower(): str(v) for k, v in (headers or {}).items()}

    # 1. Check Retry-After header
    if "retry-after" in headers_dict:
        raw_val = headers_dict["retry-after"].strip()
        try:
            return max(0.0, float(raw_val))
        except ValueError:
            # Maybe an HTTP-date format (RFC 7231)
            try:
                date_tuple = email.utils.parsedate_to_datetime(raw_val)
                diff = date_tuple.timestamp() - time.time()
                return max(0.0, float(diff))
            except Exception:
                pass

    # 2. Check JSON body
    if isinstance(body, dict):
        if "retry_after" in body:
            try:
                return max(0.0, float(body["retry_after"]))
            except (ValueError, TypeError):
                pass

    if isinstance(body, str) and body:
        # Try parsing JSON if body looks like json
        trimmed = body.strip()
        if trimmed.startswith("{") and trimmed.endswith("}"):
            try:
                data = json.loads(trimmed)
                if isinstance(data, dict) and "retry_after" in data:
                    return max(0.0, float(data["retry_after"]))
            except Exception:
                pass

        # Try regex search in body
        match = re.search(r"['\"]?retry_after['\"]?\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)", body)
        if match:
            try:
                return max(0.0, float(match.group(1)))
            except ValueError:
                pass

    return None


def format_concise_discord_log(
    status: int,
    provider: str,
    error: str,
    retry_after: Optional[float],
    state: str,
) -> str:
    """Format a clean, concise log entry without dumping HTML responses."""
    retry_str = f"{retry_after:.1f}" if retry_after is not None else "none"
    return (
        f"[DISCORD_API] status={status} provider={provider} "
        f"error={error} retry_after={retry_str} state={state}"
    )
