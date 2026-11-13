"""Short-lived evidence for privileged-account spam in guild message events."""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
from time import monotonic

import discord


@dataclass
class Sample:
    at: float
    message: discord.Message
    text: str
    selected: bool = False


@dataclass
class AccountWindow:
    last_seen: float
    samples: deque[Sample] = field(default_factory=lambda: deque(maxlen=64))
    next_alert: float = 0.0


class PrivilegedSpamGuard:
    """Observe before any await so concurrent listeners cannot claim the same batch."""

    def __init__(self) -> None:
        self.windows: OrderedDict[tuple[int, int], AccountWindow] = OrderedDict()

    def observe(self, message: discord.Message, now: float | None = None) -> tuple[list[discord.Message], bool]:
        if now is None:
            now = monotonic()
        guild, author = message.guild, message.author
        if (
            guild is None
            or message.webhook_id is not None
            or author.bot
            or message.type not in (discord.MessageType.default, discord.MessageType.reply)
        ):
            return [], False
        if author.id != guild.owner_id and not (
            isinstance(author, discord.Member) and author.guild_permissions.administrator
        ):
            return [], False

        # Recently active accounts are at the end. Idle guilds need no timers.
        while self.windows and next(iter(self.windows.values())).last_seen <= now - 60:
            self.windows.popitem(last=False)
        key = (guild.id, author.id)
        window = self.windows.setdefault(key, AccountWindow(now))
        self.windows.move_to_end(key)
        window.last_seen = now
        samples = window.samples
        while samples and samples[0].at <= now - 30:
            samples.popleft()
        if any(s.message.id == message.id for s in samples):
            return [], False

        text = " ".join(message.content.casefold().split())
        samples.append(Sample(now, message, text))
        burst = [s for s in samples if s.at > now - 8]
        copies = [s for s in samples if s.text == text] if text else []
        pings = []
        if message.mention_everyone:
            pings = [s for s in samples if s.at > now - 15 and s.message.mention_everyone]

        batch = []
        for group, threshold in ((copies, 3), (pings, 3), (burst, 8)):
            if len(group) >= threshold:
                for sample in group:
                    if not sample.selected:
                        sample.selected = True
                        batch.append(sample.message)
        alert = bool(batch) and now >= window.next_alert
        if alert:
            window.next_alert = now + 60
        return batch, alert
