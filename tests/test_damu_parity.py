"""Regression coverage for the imported DAMU workflows and saved settings."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

import config
from cogs.builder_tools import BuilderTools
from cogs.local_automod import LocalAutoModCog, CHANNEL_IDS, ROLE_IDS
from cogs.server_manager import ServerManagerCog
from cogs.verification import VerificationCog
from scripts.import_damu_settings import main as import_settings
from test_command_workflows import FakeInteraction


def command_choice(value):
    return discord.app_commands.Choice(name=value.capitalize(), value=value)


class LocalAutoModTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.file = Path(folder.name) / "offenses.json"
        settings = patch.object(config, "AUTOMOD_FILE", str(self.file))
        settings.start()
        self.addCleanup(settings.stop)
        self.cog = LocalAutoModCog(Mock())
        self.interaction = FakeInteraction()
        self.guild = self.interaction.guild
        self.author = Mock(spec=discord.Member)
        self.author.id = 20
        self.author.bot = False
        self.author.guild_permissions = discord.Permissions.none()
        self.author.roles = []
        self.author.top_role.position = 1
        self.guild.owner_id = 1
        self.guild.me.id = 999
        self.guild.me.top_role.__gt__.return_value = True
        self.author.timed_out_until = None
        self.author.timeout = AsyncMock()
        self.guild.get_member.return_value = self.author
        self.cog._dm_user_warning = AsyncMock()
        self.cog._notify_owners = AsyncMock()
        self.cog._send_channel_notice = AsyncMock()

    def message(self, channel):
        channel.permissions_for = Mock(return_value=discord.Permissions(manage_messages=True))
        return SimpleNamespace(guild=self.guild, author=self.author, channel=channel, mention_everyone=False,
                               id=100, content="https://tenor.com/view/wave", attachments=[], delete=AsyncMock())

    async def test_status_acknowledges_before_loading_settings(self):
        load = self.cog._load

        async def load_after_ack():
            self.interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
            await load()

        self.cog._load = load_after_ack
        await self.cog.automod_status.callback(self.cog, self.interaction)
        self.interaction.edit_original_response.assert_awaited_once()
        self.interaction.response.send_message.assert_not_awaited()

    async def test_legacy_role_channel_ids_and_parent_thread_exemptions(self):
        self.file.write_text(json.dumps({"settings": {"10": {
            "enabled": True, ROLE_IDS: ["30"], CHANNEL_IDS: ["70"],
        }}, "offenses": {"10:20": {"count": 1, "last_at": discord.utils.utcnow().isoformat()}}}))
        public = SimpleNamespace(id=40, category_id=None)
        direct = SimpleNamespace(id=70, category_id=None)
        thread = Mock(spec=discord.Thread, id=71, parent_id=70, parent=None)
        self.author.roles = [SimpleNamespace(id=30)]
        message = self.message(public)
        await self.cog.on_message(message)
        message.delete.assert_not_awaited()
        self.author.roles = []
        for channel in (direct, thread):
            message = self.message(channel)
            await self.cog.on_message(message)
            message.content = ""
            message.attachments = [SimpleNamespace(content_type="image/gif", filename="x.gif")] * 4
            await self.cog.on_message(message)
            message.delete.assert_not_awaited()
        self.author.timeout.assert_not_awaited()
        self.assertEqual(self.cog._offenses["10:20"]["count"], 1)

    async def test_regular_gif_timeout_requires_opt_in_and_preserves_longer_timeout(self):
        message = self.message(SimpleNamespace(id=40, category_id=None))
        await self.cog.on_message(message)
        message.delete.assert_not_awaited()
        self.interaction.user.guild_permissions = discord.Permissions(manage_guild=True)
        self.guild.me.guild_permissions = discord.Permissions(manage_messages=True, moderate_members=True)
        await self.cog.automod_status.callback(self.cog, self.interaction, True)
        await self.cog.on_message(message)
        message = self.message(message.channel)
        message.id = 101
        self.author.timed_out_until = discord.utils.utcnow() + timedelta(hours=1)
        await self.cog.on_message(message)
        self.author.timeout.assert_not_awaited()
        message = self.message(message.channel)
        message.id = 102
        self.author.timed_out_until = None
        notice = AsyncMock()
        with patch.dict(self.cog._handle_violation.__func__.__globals__, notify_action=notice):
            await self.cog.on_message(message)
            notice.assert_awaited_once()
        self.author.timeout.assert_awaited_once()

    async def test_exception_command_accepts_role_and_channel_options(self):
        await self.cog.automod_exception.callback(
            self.cog, self.interaction, command_choice("add"), role=SimpleNamespace(id=50),
            channel=SimpleNamespace(id=60),
        )
        self.assertEqual(json.loads(self.file.read_text())["settings"]["10"], {ROLE_IDS: [50], CHANNEL_IDS: [60]})


class ImportedWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_manager_alias_calls_the_registered_command(self):
        interaction = FakeInteraction()
        cog = ServerManagerCog(None)
        with patch.object(cog.server_manager, "_callback", new=AsyncMock()) as open_manager:
            await cog.damu.callback(cog, interaction)
            open_manager.assert_awaited_once_with(cog, interaction)

    async def test_verification_lockdown_keeps_private_channels_and_other_overwrites(self):
        everyone, unverified, verified = Mock(), Mock(), Mock()

        def channel(identifier, public):
            return SimpleNamespace(id=identifier, name=f"channel-{identifier}",
                                   permissions_for=Mock(return_value=discord.Permissions(view_channel=public)),
                                   overwrites_for=Mock(side_effect=lambda _: discord.PermissionOverwrite(attach_files=False)),
                                   set_permissions=AsyncMock())

        public, staff, verify = channel(1, True), channel(2, False), channel(3, True)
        guild = SimpleNamespace(default_role=everyone, channels=[public, staff, verify])
        cog = VerificationCog(None)
        errors = await cog._apply_permissions(guild, verify, verified, unverified)
        self.assertEqual(errors, [])
        staff.set_permissions.assert_not_awaited()
        self.assertEqual(public.set_permissions.await_count, 3)
        self.assertTrue(all(call.kwargs["overwrite"].attach_files is False
                            for call in public.set_permissions.await_args_list))
        self.assertEqual(verify.set_permissions.await_count, 3)

    async def test_builder_delegate_stays_inside_the_approved_guild_and_schema(self):
        cog = BuilderTools(None)
        cog.settings.records = {"10": {"delegates": [20]}}
        actor = SimpleNamespace(id=20, guild_permissions=discord.Permissions(manage_channels=True))
        guild = SimpleNamespace(id=10)
        schema = {"categories": [{"name": "Community", "channels": [{"name": "general", "type": "text"}]}]}
        self.assertTrue(cog.can_delegate(guild, actor, schema))
        self.assertFalse(cog.can_delegate(SimpleNamespace(id=11), actor, schema))
        self.assertFalse(cog.can_delegate(guild, actor, {**schema, "roles": [{"name": "Owner"}]}))
        self.assertFalse(cog.can_delegate(guild, actor, {
            "categories": [{"name": "Community", "permission_overwrites": [], "channels": []}],
        }))
        actor.guild_permissions.manage_channels = False
        self.assertFalse(cog.can_delegate(guild, actor, schema))

    async def test_health_endpoints_keep_process_alive_and_gate_readiness(self):
        from main import _keep_alive_app
        with _keep_alive_app.test_client() as client:
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.get("/ready").status_code, 503)
            self.assertEqual(client.get("/status").status_code, 200)


class ImportSettingsTests(unittest.TestCase):
    def test_migration_is_dry_by_default_and_preserves_existing_files(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / "damu", Path(folder) / "builder"
            (source / "automod").mkdir(parents=True)
            (source / "automod/offenses.json").write_text(json.dumps({
                "settings": {"50": {"enabled": True, "bypass_role_ids": ["60"], "ignored_channel_ids": [70]}},
            }))
            (source / "verification_config.json").write_text('{"50": {"enabled": true}}')
            args = ["import_damu_settings.py", "--source", str(source), "--target", str(target)]
            with patch("sys.argv", args), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(import_settings(), 0)
                self.assertFalse(target.exists())
            target.mkdir()
            target_file = target / "verification_config.json"
            target_file.write_text('{"50": {"enabled": false}}')
            with patch("sys.argv", args + ["--apply"]), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(import_settings(), 1)
                self.assertFalse((target / "automod/offenses.json").exists())
                self.assertEqual(target_file.read_text(), '{"50": {"enabled": false}}')
                target_file.unlink()
                self.assertEqual(import_settings(), 0)
            rules = json.loads((target / "automod/offenses.json").read_text())["settings"]["50"]
            self.assertEqual(rules, {"enabled": False, "bypass_role_ids": ["60"], "ignored_channel_ids": [70]})
