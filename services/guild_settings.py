"""Read and write guild settings without overwriting an unreadable file."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from utils.storage import write_json_atomic


class GuildSettings:
    def __init__(self, filename: str) -> None:
        self.filename = Path(filename)
        self.records: dict[str, dict] = {}
        self.lock = asyncio.Lock()

    async def load(self) -> None:
        try:
            raw = await asyncio.to_thread(self.filename.read_text, encoding="utf-8")
        except FileNotFoundError:
            return
        data = json.loads(raw)
        if not isinstance(data, dict) or any(not isinstance(value, dict) for value in data.values()):
            raise ValueError(f"{self.filename.name} must contain a guild settings object.")
        self.records = data

    def get(self, guild_id: int) -> dict:
        return self.records.get(str(guild_id), {})

    async def update(self, guild_id: int, **changes) -> None:
        async with self.lock:
            key = str(guild_id)
            updated = {**self.records, key: {**self.get(guild_id), **changes}}

            async def commit():
                await write_json_atomic(str(self.filename), updated)
                self.records = updated

            task = asyncio.create_task(commit())
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
