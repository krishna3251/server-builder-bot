"""Execution context for operations orchestrated by the DAMU Engine."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import discord

from core.models import generate_build_id


@dataclass
class ExecutionContext:
    guild: discord.Guild
    user: discord.User | discord.Member
    interaction: discord.Interaction | None = None
    build_id: str = field(default_factory=generate_build_id)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    audit_logs: list[str] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    cancelled: bool = False
    _cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

    def log(self, entry: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.audit_logs.append(f"[{ts}] {entry}")

    def cancel(self) -> None:
        self.cancelled = True
        self._cancel_event.set()

    def is_cancelled(self) -> bool:
        return self.cancelled or self._cancel_event.is_set()

    @property
    def bot_member(self) -> discord.Member | None:
        return self.guild.me
