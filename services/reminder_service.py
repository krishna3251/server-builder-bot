"""Small durable reminder queue for a single bot process."""
from __future__ import annotations
import asyncio
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid
import discord
import config
from services.embed_service import info_embed
from services.error_service import report_error
from utils.storage import write_json_atomic


class ReminderService:
    def __init__(self, bot):
        self.bot = bot
        self.path = Path(config.DATA_DIR) / 'reminders.json'
        self.lock = asyncio.Lock()
        self.data = None
        self.worker = None

    async def load(self):
        if self.data is None:
            raw = await asyncio.to_thread(self.path.read_text, encoding='utf-8') if self.path.exists() else '{}'
            self.data = json.loads(raw)
            if not isinstance(self.data, dict):
                raise ValueError('Reminder storage is invalid.')

    async def add(self, user_id, due, message):
        async with self.lock:
            await self.load()
            if sum(r['user_id'] == user_id and r.get('status') != 'failed' for r in self.data.values()) >= 10:
                raise ValueError('You can have at most 10 pending reminders.')
            identifier = uuid.uuid4().hex[:10]
            updated = copy.deepcopy(self.data)
            updated[identifier] = {'user_id': user_id, 'due': due.timestamp(), 'message': message[:2000], 'attempts': 0}
            await write_json_atomic(str(self.path), updated)
            self.data = updated
            return identifier

    async def list_for(self, user_id):
        async with self.lock:
            await self.load()
            return {key: copy.deepcopy(value) for key, value in self.data.items() if value['user_id'] == user_id}

    async def cancel(self, user_id, identifier):
        async with self.lock:
            await self.load()
            item = self.data.get(identifier)
            if not item or item['user_id'] != user_id:
                return False
            updated = copy.deepcopy(self.data)
            del updated[identifier]
            await write_json_atomic(str(self.path), updated)
            self.data = updated
            return True

    async def deliver_due(self):
        async with self.lock:
            await self.load()
            now = datetime.now(timezone.utc).timestamp()
            for key, item in list(self.data.items()):
                if item['due'] > now or item.get('status') == 'failed':
                    continue
                updated = copy.deepcopy(self.data)
                try:
                    user = self.bot.get_user(item['user_id']) or await self.bot.fetch_user(item['user_id'])
                    await user.send(embed=info_embed('⏰ Your reminder', item['message']), allowed_mentions=discord.AllowedMentions.none())
                    del updated[key]
                except (discord.HTTPException, OSError) as exc:
                    updated[key]['attempts'] += 1
                    updated[key]['due'] = now + 60
                    if updated[key]['attempts'] >= 3:
                        updated[key]['status'] = 'failed'
                    report_error(exc, context='Reminder delivery')
                await write_json_atomic(str(self.path), updated)
                self.data = updated

    async def run(self):
        await self.bot.wait_until_ready()
        while True:
            try:
                await self.deliver_due()
            except Exception as exc:
                report_error(exc, context='Reminder queue; retrying')
            await asyncio.sleep(10)

    def start(self):
        self.worker = asyncio.create_task(self.run(), name='reminder-queue')

    async def close(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
