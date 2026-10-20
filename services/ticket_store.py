"""Ticket persistence: MongoDB when configured, atomic local JSON otherwise."""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import motor.motor_asyncio
import config
from utils.storage import write_json_atomic


class TicketStore:
    def __init__(self):
        self.client = None
        self.col = None
        self.path = Path(config.DATA_DIR) / 'tickets' / 'guild_configs.json'
        self._lock = asyncio.Lock()
        self._data = None
        if config.MONGO_URI.strip():
            self.client = motor.motor_asyncio.AsyncIOMotorClient(config.MONGO_URI, serverSelectionTimeoutMS=5000)
            self.col = self.client['ticket_bot']['guild_configs']

    @property
    def backend(self):
        return 'MongoDB' if self.col is not None else 'Local JSON (persistent disk required)'

    def close(self):
        if self.client is not None:
            self.client.close()

    async def _load(self):
        if self._data is None:
            if self.path.exists():
                raw = await asyncio.to_thread(self.path.read_text, encoding='utf-8')
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError('Ticket storage must contain a JSON object.')
                self._data = value
            else:
                self._data = {}

    async def get_guild(self, guild_id):
        if self.col is not None:
            return await self.col.find_one({'_id': str(guild_id)}) or {}
        async with self._lock:
            await self._load()
            return copy.deepcopy(self._data.get(str(guild_id), {}))

    async def save_guild(self, guild_id, data):
        snapshot = copy.deepcopy(data)
        snapshot.pop('_id', None)
        if self.col is not None:
            await self.col.update_one({'_id': str(guild_id)}, {'$set': snapshot}, upsert=True)
            return
        async with self._lock:
            await self._load()
            updated = copy.deepcopy(self._data)
            updated[str(guild_id)] = snapshot
            await write_json_atomic(str(self.path), updated)
            self._data = updated

    async def get_key(self, guild_id, key, default=None):
        doc = await self.get_guild(guild_id)
        for part in key.split('.'):
            if not isinstance(doc, dict) or part not in doc:
                return default
            doc = doc[part]
        return doc

    async def _change_key(self, guild_id, key, value=None, *, delete=False):
        if self.col is not None:
            await self.col.update_one({'_id': str(guild_id)}, {'$unset' if delete else '$set': {key: '' if delete else value}}, upsert=not delete)
            return
        async with self._lock:
            await self._load()
            updated = copy.deepcopy(self._data)
            doc = updated.setdefault(str(guild_id), {})
            parts = key.split('.')
            for part in parts[:-1]:
                doc = doc.setdefault(part, {})
            if delete:
                doc.pop(parts[-1], None)
            else:
                doc[parts[-1]] = value
            await write_json_atomic(str(self.path), updated)
            self._data = updated

    async def set_key(self, guild_id, key, value):
        await self._change_key(guild_id, key, value)

    async def delete_key(self, guild_id, key):
        await self._change_key(guild_id, key, delete=True)

    async def pending_guilds(self):
        if self.col is not None:
            async for doc in self.col.find({'pending_deletions': {'$exists': True}}):
                yield doc
        else:
            async with self._lock:
                await self._load()
                docs = [dict(copy.deepcopy(v), _id=k) for k, v in self._data.items() if v.get('pending_deletions')]
            for doc in docs:
                yield doc
