"""DAMU Error System — Error classification, custom exceptions, and user-facing formatting."""

from __future__ import annotations

import asyncio
import enum
import logging
from typing import Any

import discord

log = logging.getLogger("core.errors")


class ErrorCategory(str, enum.Enum):
    TRANSIENT = "TRANSIENT"
    PERMISSION = "PERMISSION"
    NOT_FOUND = "NOT_FOUND"
    RATE_LIMIT = "RATE_LIMIT"
    INVALID_INPUT = "INVALID_INPUT"
    HIERARCHY = "HIERARCHY"
    DISCORD_API = "DISCORD_API"
    DATABASE = "DATABASE"
    UNKNOWN = "UNKNOWN"


class DamuError(Exception):
    """Base exception for DAMU Server Builder operations."""

    def __init__(
        self,
        message: str,
        category: ErrorCategory = ErrorCategory.UNKNOWN,
        user_message: str | None = None,
        required_permission: str | None = None,
        recoverable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.user_message = user_message or message
        self.required_permission = required_permission
        self.recoverable = recoverable
        self.details = details or {}


class PermissionError(DamuError):
    def __init__(self, message: str, required_permission: str = "Manage Channels", details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            category=ErrorCategory.PERMISSION,
            user_message=f"I don't have permission to perform this action. Required: `{required_permission}`.",
            required_permission=required_permission,
            recoverable=False,
            details=details,
        )


class HierarchyError(DamuError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            category=ErrorCategory.HIERARCHY,
            user_message="Role hierarchy prevents this action. The bot's role or your role must be higher than the target role.",
            recoverable=False,
            details=details,
        )


class ResourceNotFoundError(DamuError):
    def __init__(self, resource_type: str, name_or_id: str | int, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=f"{resource_type} '{name_or_id}' was not found.",
            category=ErrorCategory.NOT_FOUND,
            user_message=f"The requested {resource_type.lower()} was not found.",
            recoverable=False,
            details=details,
        )


class RateLimitError(DamuError):
    def __init__(self, retry_after: float = 5.0, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=f"Rate limited by Discord API. Retry after {retry_after:.1f}s.",
            category=ErrorCategory.RATE_LIMIT,
            user_message=f"Discord is rate-limiting actions. Please wait {retry_after:.1f} seconds.",
            recoverable=True,
            details={"retry_after": retry_after, **(details or {})},
        )


class DatabaseError(DamuError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            category=ErrorCategory.DATABASE,
            user_message="The database is temporarily unavailable. Your changes could not be saved.",
            recoverable=True,
            details=details,
        )


class ValidationError(DamuError):
    def __init__(self, message: str, field: str | None = None, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            category=ErrorCategory.INVALID_INPUT,
            user_message=message,
            recoverable=False,
            details={"field": field, **(details or {})},
        )


def classify_error(exc: BaseException) -> ErrorCategory:
    """Classify an arbitrary exception into a standardized ErrorCategory."""
    if isinstance(exc, DamuError):
        return exc.category

    if isinstance(exc, discord.Forbidden):
        return ErrorCategory.PERMISSION

    if isinstance(exc, discord.NotFound):
        return ErrorCategory.NOT_FOUND

    if isinstance(exc, discord.RateLimited):
        return ErrorCategory.RATE_LIMIT

    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return ErrorCategory.TRANSIENT

    if isinstance(exc, discord.HTTPException):
        status = getattr(exc, "status", 0)
        code = getattr(exc, "code", 0)
        if status == 429 or code == 20028:
            return ErrorCategory.RATE_LIMIT
        if status in (500, 502, 503, 504):
            return ErrorCategory.TRANSIENT
        if code in (50013, 50028):
            return ErrorCategory.PERMISSION
        if code in (10003, 10011, 10014, 10062):
            return ErrorCategory.NOT_FOUND
        return ErrorCategory.DISCORD_API

    exc_name = type(exc).__name__.lower()
    if "mongo" in exc_name or "pymongo" in exc_name or "motor" in exc_name:
        return ErrorCategory.DATABASE

    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return ErrorCategory.INVALID_INPUT

    return ErrorCategory.UNKNOWN


def format_user_error(exc: BaseException, operation_name: str = "complete this action") -> discord.Embed:
    """Convert any exception into a clean, professional user-facing embed without tracebacks."""
    category = classify_error(exc)

    title = "❌ DAMU COULD NOT COMPLETE THIS ACTION"
    description = f"**Operation:** {operation_name}\n"

    if isinstance(exc, DamuError):
        description += f"\n**Reason:** {exc.user_message}"
        if exc.required_permission:
            description += f"\n**Required Permission:** `{exc.required_permission}`"
    elif isinstance(exc, discord.Forbidden):
        description += "\n**Reason:** I don't have permission to perform this Discord API operation."
        description += "\n**Required:** Ensure DAMU has `Manage Channels`, `Manage Roles`, and proper role hierarchy."
    elif isinstance(exc, discord.NotFound):
        code = getattr(exc, "code", 0)
        if code == 10062:
            description += "\n**Reason:** The Discord interaction expired before completion."
        else:
            description += "\n**Reason:** The targeted channel, category, or role was not found."
    elif isinstance(exc, discord.RateLimited):
        retry_after = getattr(exc, "retry_after", 5.0)
        description += f"\n**Reason:** Discord rate-limit reached. Please retry in **{retry_after:.1f}s**."
    elif category == ErrorCategory.DATABASE:
        description += "\n**Reason:** Ticket database is temporarily unavailable. Safety guard prevented inconsistent state."
    elif category == ErrorCategory.TRANSIENT:
        description += "\n**Reason:** Network connection timed out. Please try again in a moment."
    else:
        description += "\n**Reason:** An unexpected internal error occurred. Please contact server administrators."

    embed = discord.Embed(
        title=title,
        description=description,
        colour=0xE74C3C,
    )
    embed.set_footer(text=f"Category: {category.value} | DAMU Safety Engine")
    return embed
