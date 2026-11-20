"""Interaction Safety Layer — Guarantees safe interaction acknowledgements and lifecycle handling.

Eliminates Discord 10062 'Unknown interaction' errors by strictly enforcing acknowledgement
order and handling timeouts and response states gracefully. Coordinated with DiscordAPIGuard.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, Sequence

import discord

from core.discord_api.guard import api_guard
from core.discord_api.state import APIState

log = logging.getLogger("core.interaction")

# Discord interaction response deadline is 3 seconds; token lifecycle is 15 minutes.
MAX_INTERACTION_AGE_SECONDS = 900.0  # 15 minutes


class InteractionResultReason(str, Enum):
    """Detailed reason for an interaction operation outcome."""

    SUCCESS = "SUCCESS"
    ALREADY_RESPONDED = "ALREADY_RESPONDED"
    INTERACTION_EXPIRED = "INTERACTION_EXPIRED"
    UNKNOWN_INTERACTION = "UNKNOWN_INTERACTION"
    RATE_LIMITED = "RATE_LIMITED"
    CLOUDFLARE_BLOCKED = "CLOUDFLARE_BLOCKED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    OTHER_HTTP_ERROR = "OTHER_HTTP_ERROR"


@dataclass
class InteractionResult:
    """Structured result returned by interaction safety helpers."""

    success: bool
    reason: InteractionResultReason
    message: str = ""
    retry_at: Optional[str] = None
    response_sent: bool = False
    message_obj: Any = None

    def __bool__(self) -> bool:
        """Allow evaluating result directly as boolean for backward compatibility."""
        return self.success

    @property
    def is_blocked(self) -> bool:
        """Return True if failure was due to rate-limiting or Cloudflare block."""
        return self.reason in (
            InteractionResultReason.RATE_LIMITED,
            InteractionResultReason.CLOUDFLARE_BLOCKED,
        )


def is_acknowledged(interaction: discord.Interaction) -> bool:
    """Return True if the interaction response has already been sent or deferred."""
    try:
        return interaction.response.is_done()
    except Exception:
        return False


def interaction_alive(interaction: discord.Interaction) -> bool:
    """Check if the interaction is within its valid lifetime (15 minutes)."""
    try:
        if not getattr(interaction, "created_at", None):
            return True
        now = datetime.now(timezone.utc)
        elapsed = (now - interaction.created_at).total_seconds()
        return elapsed < MAX_INTERACTION_AGE_SECONDS
    except Exception:
        return True


async def safe_defer(
    interaction: discord.Interaction,
    *,
    ephemeral: bool = True,
    thinking: bool = False,
) -> InteractionResult:
    """Immediately acknowledge and defer an interaction if not already acknowledged.

    Coordinated with DiscordAPIGuard. Returns structured InteractionResult.
    """
    if is_acknowledged(interaction):
        return InteractionResult(
            success=True,
            reason=InteractionResultReason.ALREADY_RESPONDED,
            response_sent=True,
        )

    if not interaction_alive(interaction):
        log.warning(
            "Interaction %s is expired (age > 15m). Cannot defer.",
            getattr(interaction, "id", "unknown"),
        )
        return InteractionResult(
            success=False,
            reason=InteractionResultReason.INTERACTION_EXPIRED,
            message="Interaction expired (age > 15m).",
        )

    # Pre-flight check via Global API Guard
    allowed, api_state, retry_at = api_guard.can_execute()
    if not allowed:
        reason = (
            InteractionResultReason.CLOUDFLARE_BLOCKED
            if api_state == APIState.CLOUDFLARE_BLOCKED
            else InteractionResultReason.RATE_LIMITED
        )
        log.warning(
            "[INTERACTION] safe_defer blocked by API guard (state=%s, retry_at=%s)",
            api_state.value,
            retry_at,
        )
        return InteractionResult(
            success=False,
            reason=reason,
            retry_at=retry_at,
            message=f"Discord API is currently {api_state.value}.",
        )

    try:
        # discord.py defer() accepts thinking kwarg in newer versions; pass if needed
        kwargs = {"ephemeral": ephemeral}
        if thinking:
            kwargs["thinking"] = True
        await interaction.response.defer(**kwargs)

        api_guard.record_success()
        return InteractionResult(
            success=True,
            reason=InteractionResultReason.SUCCESS,
            response_sent=True,
        )
    except discord.InteractionResponded:
        return InteractionResult(
            success=True,
            reason=InteractionResultReason.ALREADY_RESPONDED,
            response_sent=True,
        )
    except discord.HTTPException as exc:
        status = getattr(exc, "status", None)
        body = getattr(exc, "text", "") or str(exc)
        code = getattr(exc, "code", None)

        if status == 429:
            api_guard.record_failure(status=429, body=body, error=exc)
            reason = (
                InteractionResultReason.CLOUDFLARE_BLOCKED
                if api_guard.state == APIState.CLOUDFLARE_BLOCKED
                else InteractionResultReason.RATE_LIMITED
            )
            return InteractionResult(
                success=False,
                reason=reason,
                retry_at=api_guard.retry_at,
                message="Discord API rate limited during defer.",
            )
        elif code == 40060:
            return InteractionResult(success=True, reason=InteractionResultReason.ALREADY_RESPONDED, response_sent=True)
        elif code == 10062:
            return InteractionResult(
                success=False,
                reason=InteractionResultReason.INTERACTION_EXPIRED,
                message="Unknown interaction (10062).",
            )
        elif status == 403:
            return InteractionResult(
                success=False,
                reason=InteractionResultReason.FORBIDDEN,
                message="Missing permissions to defer.",
            )
        elif status == 404:
            return InteractionResult(
                success=False,
                reason=InteractionResultReason.NOT_FOUND,
                message="Interaction not found.",
            )
        else:
            return InteractionResult(
                success=False,
                reason=InteractionResultReason.OTHER_HTTP_ERROR,
                message=str(exc),
            )
    except Exception as exc:
        log.exception("[INTERACTION] Unexpected error in safe_defer: %s", exc)
        return InteractionResult(
            success=False,
            reason=InteractionResultReason.OTHER_HTTP_ERROR,
            message=str(exc),
        )


async def safe_send(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    embeds: Sequence[discord.Embed] | None = None,
    view: discord.ui.View | None = None,
    ephemeral: bool = False,
    file: discord.File | None = None,
    files: Sequence[discord.File] | None = None,
    delete_after: float | None = None,
) -> InteractionResult:
    """Safely deliver a message to the user regardless of interaction state.

    If not acknowledged: uses interaction.response.send_message.
    If already acknowledged: uses interaction.followup.send.
    Returns structured InteractionResult.
    """
    # Pre-flight check via Global API Guard
    allowed, api_state, retry_at = api_guard.can_execute()
    if not allowed:
        reason = (
            InteractionResultReason.CLOUDFLARE_BLOCKED
            if api_state == APIState.CLOUDFLARE_BLOCKED
            else InteractionResultReason.RATE_LIMITED
        )
        log.warning(
            "[INTERACTION] safe_send blocked by API guard (state=%s, retry_at=%s)",
            api_state.value,
            retry_at,
        )
        return InteractionResult(
            success=False,
            reason=reason,
            retry_at=retry_at,
            message=f"Discord API is currently {api_state.value}.",
        )

    send_kwargs: dict[str, Any] = {"ephemeral": ephemeral}
    if content is not None:
        send_kwargs["content"] = content
    if embed is not None:
        send_kwargs["embed"] = embed
    if embeds is not None:
        send_kwargs["embeds"] = embeds
    if view is not None:
        send_kwargs["view"] = view
    if file is not None:
        send_kwargs["file"] = file
    if files is not None:
        send_kwargs["files"] = files
    if delete_after is not None:
        send_kwargs["delete_after"] = delete_after

    try:
        msg: Any = None
        if interaction.response.is_done():
            msg = await interaction.followup.send(**send_kwargs)
        else:
            await interaction.response.send_message(**send_kwargs)

        api_guard.record_success()
        return InteractionResult(
            success=True,
            reason=InteractionResultReason.SUCCESS,
            response_sent=True,
            message_obj=msg,
        )
    except discord.HTTPException as exc:
        status = getattr(exc, "status", None)
        body = getattr(exc, "text", "") or str(exc)
        code = getattr(exc, "code", None)

        if status == 429:
            api_guard.record_failure(status=429, body=body, error=exc)
            reason = (
                InteractionResultReason.CLOUDFLARE_BLOCKED
                if api_guard.state == APIState.CLOUDFLARE_BLOCKED
                else InteractionResultReason.RATE_LIMITED
            )
            return InteractionResult(
                success=False,
                reason=reason,
                retry_at=api_guard.retry_at,
                message="Discord API rate limited during response.",
            )
        elif code == 10062:
            return InteractionResult(
                success=False,
                reason=InteractionResultReason.INTERACTION_EXPIRED,
                message="Interaction expired.",
            )
        elif status == 403:
            return InteractionResult(
                success=False,
                reason=InteractionResultReason.FORBIDDEN,
                message="Missing permissions.",
            )
        elif status == 404:
            return InteractionResult(
                success=False,
                reason=InteractionResultReason.NOT_FOUND,
                message="Not found.",
            )
        else:
            return InteractionResult(
                success=False,
                reason=InteractionResultReason.OTHER_HTTP_ERROR,
                message=str(exc),
            )
    except Exception as exc:
        log.exception("[INTERACTION] Unexpected error in safe_send: %s", exc)
        return InteractionResult(
            success=False,
            reason=InteractionResultReason.OTHER_HTTP_ERROR,
            message=str(exc),
        )


async def safe_followup(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    embeds: Sequence[discord.Embed] | None = None,
    view: discord.ui.View | None = None,
    ephemeral: bool = False,
    file: discord.File | None = None,
    files: Sequence[discord.File] | None = None,
) -> InteractionResult:
    """Explicitly send a followup message."""
    return await safe_send(
        interaction,
        content=content,
        embed=embed,
        embeds=embeds,
        view=view,
        ephemeral=ephemeral,
        file=file,
        files=files,
    )


async def safe_edit(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    embeds: Sequence[discord.Embed] | None = None,
    view: discord.ui.View | None = None,
    file: discord.File | None = None,
    files: Sequence[discord.File] | None = None,
) -> InteractionResult:
    """Safely edit the current message or original response."""
    allowed, api_state, retry_at = api_guard.can_execute()
    if not allowed:
        reason = (
            InteractionResultReason.CLOUDFLARE_BLOCKED
            if api_state == APIState.CLOUDFLARE_BLOCKED
            else InteractionResultReason.RATE_LIMITED
        )
        return InteractionResult(
            success=False,
            reason=reason,
            retry_at=retry_at,
        )

    edit_kwargs: dict[str, Any] = {}
    if content is not None:
        edit_kwargs["content"] = content
    if embed is not None:
        edit_kwargs["embed"] = embed
    if embeds is not None:
        edit_kwargs["embeds"] = embeds
    if view is not None:
        edit_kwargs["view"] = view
    if file is not None:
        edit_kwargs["file"] = file
    if files is not None:
        edit_kwargs["files"] = files

    try:
        if not is_acknowledged(interaction):
            await interaction.response.edit_message(**edit_kwargs)
        else:
            await interaction.edit_original_response(**edit_kwargs)

        api_guard.record_success()
        return InteractionResult(
            success=True,
            reason=InteractionResultReason.SUCCESS,
            response_sent=True,
        )
    except discord.HTTPException as exc:
        if exc.status == 429:
            api_guard.record_failure(status=429, body=getattr(exc, "text", ""), error=exc)
            return InteractionResult(
                success=False,
                reason=InteractionResultReason.RATE_LIMITED,
                retry_at=api_guard.retry_at,
            )
        return InteractionResult(
            success=False,
            reason=InteractionResultReason.OTHER_HTTP_ERROR,
            message=str(exc),
        )
    except Exception as exc:
        log.error("Unexpected error in safe_edit: %s", exc, exc_info=True)
        return InteractionResult(
            success=False,
            reason=InteractionResultReason.OTHER_HTTP_ERROR,
            message=str(exc),
        )


async def safe_modal(
    interaction: discord.Interaction,
    modal: discord.ui.Modal,
) -> bool:
    """Safely send a modal.

    Modals CANNOT be sent if the interaction has already been acknowledged or deferred.
    Returns True if sent, False otherwise.
    """
    if is_acknowledged(interaction):
        log.warning("Cannot send modal on an already-acknowledged interaction.")
        await safe_followup(
            interaction,
            content="⚠️ This action requested a form input, but the interaction was already acknowledged. Please click the button again.",
            ephemeral=True,
        )
        return False

    try:
        await interaction.response.send_modal(modal)
        return True
    except discord.NotFound:
        log.warning("safe_modal failed: 10062 Unknown interaction.")
        return False
    except discord.InteractionResponded:
        log.warning("safe_modal failed: interaction was already responded to.")
        return False
    except discord.HTTPException as exc:
        log.warning("safe_modal HTTPException: %s", exc)
        return False
    except Exception as exc:
        log.error("Unexpected error in safe_modal: %s", exc, exc_info=True)
        return False


class InteractionSafety:
    """Unified wrapper around interaction safety helpers."""

    safe_defer = staticmethod(safe_defer)
    safe_send = staticmethod(safe_send)
    safe_followup = staticmethod(safe_followup)
    safe_edit = staticmethod(safe_edit)
    safe_modal = staticmethod(safe_modal)
    is_acknowledged = staticmethod(is_acknowledged)
    interaction_alive = staticmethod(interaction_alive)
