import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from cogs.builder_tools import compare_permissions
from cogs.server_builder import TEMPLATES
from cogs.server_manager import ChangePreviewView, ManagerView, PermissionManagerView
from core.context import ExecutionContext
from core.engine import DamuEngine
from core.errors import ValidationError
from core.models import Action, ActionStatus, ActionType, BuildPlan, RiskLevel
from core.result import format_preview_embed
from engines.builder.planner import builder_planner
from engines.builder.schema import schema_validator
from engines.guild.analyzer import ServerAnalyzer
from engines.guild.channel_engine import ChannelEngine
from engines.guild.role_engine import RoleEngine
from services.ticket_permissions import ticket_log_is_private


def guild_fixture():
    guild = SimpleNamespace(id=321, name="Test", owner_id=1)
    bot = MagicMock(spec=discord.Member, id=2, guild=guild, guild_permissions=discord.Permissions.all())
    admin = SimpleNamespace(id=1, guild=guild, guild_permissions=discord.Permissions.all())
    everyone = MagicMock(spec=discord.Role, id=321, guild=guild, managed=False)
    everyone.name = "@everyone"
    everyone.is_default.return_value = True
    everyone.permissions = discord.Permissions.none()
    guild.me = bot
    guild.default_role = everyone
    guild.roles = [everyone]
    guild.categories = []
    guild.channels = []
    guild.get_role = lambda role_id: next((role for role in guild.roles if role.id == role_id), None)
    guild.get_channel = lambda channel_id: next((channel for channel in guild.channels if channel.id == channel_id), None)
    guild.bitrate_limit = 96000
    guild.create_category = AsyncMock()
    guild.create_role = AsyncMock()
    guild.create_text_channel = AsyncMock()
    guild.edit = AsyncMock()
    return guild, admin


def channel_fixture(guild, name, identifier, kind=discord.TextChannel):
    channel = MagicMock(spec=kind, id=identifier, guild=guild, category_id=None)
    channel.name = name
    channel.overwrites = {}
    channel.permissions_for.return_value = discord.Permissions.all()
    if kind is discord.TextChannel:
        channel.is_news.return_value = False
    return channel


class PermissionAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_audit_reports_elevated_grants_and_real_status_with_visibility_scope(self):
        guild, _ = guild_fixture()
        guild.default_role.permissions = discord.Permissions(mention_everyone=True, manage_messages=True)
        viewer = SimpleNamespace(guild_permissions=discord.Permissions(manage_guild=True))
        rules = channel_fixture(guild, "📜rules", 111)
        chat = channel_fixture(guild, "rules-discussion", 112)
        hidden = channel_fixture(guild, "private-staff", 113)
        hidden.permissions_for.side_effect = lambda who: (
            discord.Permissions.none() if who is viewer else discord.Permissions.all()
        )
        chat.overwrites = {guild.default_role: discord.PermissionOverwrite(manage_webhooks=True)}
        top = MagicMock(spec=discord.Role, id=10)
        top.name = "Bot role"
        top.is_default.return_value = False
        top.permissions = discord.Permissions.none()
        guild.roles.append(top)
        guild.me.top_role = top
        guild.channels = [rules, chat, hidden]
        guild.fetch_automod_rules = AsyncMock(return_value=[SimpleNamespace(enabled=False)])
        local = SimpleNamespace(is_enabled=AsyncMock(return_value=True))
        tickets = SimpleNamespace(config_manager=SimpleNamespace(get_guild=AsyncMock(return_value={})))
        client = SimpleNamespace(get_cog={"Local AutoMod": local, "TicketSystem": tickets}.get)

        report = await ServerAnalyzer().analyze(guild, viewer=viewer, client=client)

        self.assertEqual(report.channels_outside_scope, 1)
        self.assertNotIn(hidden.name, str(report))
        self.assertEqual(report.dangerous_roles, ["@everyone: manage_messages, mention_everyone"])
        self.assertEqual(report.hierarchy_issues, [])
        self.assertTrue(any("manage_webhooks" in issue for issue in report.permission_conflicts))
        self.assertEqual([fix.target_id for fix in report.suggested_fixes], [rules.id])
        self.assertEqual((report.tickets_status, report.local_automod_status, report.native_automod_status),
                         ("Unconfigured", "Enabled", "Disabled"))
        self.assertFalse(report.is_healthy)

    async def test_scan_reports_missing_bot_without_crashing(self):
        guild, _ = guild_fixture()
        guild.me = None
        report = await ServerAnalyzer().analyze(guild)
        self.assertIn("Bot member unavailable", report.missing_bot_perms)
        self.assertEqual(report.native_automod_status, "Unknown")

    async def test_repair_preserves_other_fields_and_uses_channel_manage_roles(self):
        guild, _ = guild_fixture()
        guild.me.guild_permissions = discord.Permissions.none()
        channel = channel_fixture(guild, "rules", 100)
        guild.channels = [channel]
        channel.permissions_for.return_value = discord.Permissions(view_channel=True, manage_roles=True)
        channel.overwrites_for.return_value = discord.PermissionOverwrite(view_channel=True, attach_files=False)
        await ChannelEngine().update_permissions(guild, {
            "channel_id": channel.id, "target_name": "@everyone", "overwrites": {"send_messages": False},
        })
        self.assertEqual(channel.set_permissions.call_args.kwargs["overwrite"],
                         discord.PermissionOverwrite(view_channel=True, attach_files=False, send_messages=False))
        channel.permissions_for.return_value = discord.Permissions(view_channel=True, manage_channels=True)
        with self.assertRaisesRegex(Exception, "Manage Roles"):
            await ChannelEngine().update_permissions(guild, {
                "channel_id": channel.id, "target_name": "@everyone", "overwrites": {"send_messages": False},
            })
        self.assertEqual(channel.set_permissions.await_count, 1)

    async def test_default_category_policy_is_explicit_on_creation_and_public_preset_rejected(self):
        guild, _ = guild_fixture()
        category = channel_fixture(guild, "private", 100, discord.CategoryChannel)
        category.overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
        category.permissions_for.side_effect = lambda target: (
            discord.Permissions.none() if target is guild.default_role else discord.Permissions.all()
        )
        await ChannelEngine().create_channel(guild, {"name": "staff-chat", "category": category})
        self.assertEqual(guild.create_text_channel.call_args.kwargs["overwrites"], category.overwrites)
        with self.assertRaisesRegex(ValidationError, "private category"):
            await ChannelEngine().create_channel(guild, {
                "name": "public-chat", "category": category, "permission_preset": "PUBLIC",
            })
        self.assertEqual(guild.create_text_channel.await_count, 1)

    async def test_build_places_channel_under_returned_category_before_gateway_updates(self):
        guild, admin = guild_fixture()
        existing_category = channel_fixture(guild, "private", 200, discord.CategoryChannel)
        existing_category.overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
        guild.create_category.return_value = existing_category
        guild.create_text_channel.return_value = channel_fixture(guild, "staff-chat", 201)
        schema = {"server_name": "New test", "categories": [{
            "name": "private", "permission_overwrites": [{"role": "@everyone", "deny": ["view_channel"]}],
            "channels": [{"name": "staff-chat"}],
        }]}
        plan = builder_planner.create_plan_from_schema(guild, admin, schema)
        builder = DamuEngine()
        ctx = ExecutionContext(guild, admin)
        self.assertEqual(plan.risk_level, RiskLevel.HIGH)
        self.assertFalse((await builder.execute(plan, ctx)).success)
        self.assertTrue((await builder.execute(plan, ctx, confirmed=True)).success)
        guild.create_category.assert_awaited_once()
        self.assertIs(guild.create_text_channel.call_args.kwargs["category"], existing_category)
        self.assertFalse(guild.create_text_channel.call_args.kwargs["overwrites"][guild.default_role].view_channel)
        guild.edit.assert_awaited_once()
        self.assertFalse((await builder.execute(plan, ctx, confirmed=True)).success)

    async def test_bogus_plan_action_or_dependency_is_rejected_before_creating_resources(self):
        guild, admin = guild_fixture()
        ctx = ExecutionContext(guild, admin)
        engine = DamuEngine()
        with self.assertRaises(ValidationError):
            await engine.plan({"action": "unknown"}, ctx)
        for action in [
            Action(ActionType.CONFIGURE_MODULE, "module"),
            Action(ActionType.CREATE_CHANNEL, "child", {"name": "child", "category_target": "missing"}),
        ]:
            plan = BuildPlan(guild.id, admin.id, "Test", actions=[
                Action(ActionType.CREATE_CATEGORY, "test", {"name": "test"}), action,
            ])
            self.assertFalse((await engine.execute(plan, ctx, confirmed=True)).success)
        guild.create_category.assert_not_awaited()

    async def test_failure_does_not_claim_that_a_completed_deletion_was_rolled_back(self):
        guild, admin = guild_fixture()
        channel = channel_fixture(guild, "old-channel", 200)
        guild.channels.append(channel)
        plan = BuildPlan(guild.id, admin.id, "Test", actions=[
            Action(ActionType.DELETE_CHANNEL, channel.name, {"channel_id": channel.id}),
            Action(ActionType.CREATE_CHANNEL, "new", {"name": "new"}),
        ])
        guild.create_text_channel.side_effect = RuntimeError("No channel created")
        result = await DamuEngine().execute(plan, ExecutionContext(guild, admin), confirmed=True)
        self.assertFalse(result.success)
        self.assertEqual([action.status for action in plan.actions], [ActionStatus.COMPLETED, ActionStatus.FAILED])
        self.assertIn("may require manual review", result.recovery)

    async def test_role_admin_cannot_be_confirmed_via_input(self):
        guild, _ = guild_fixture()
        for permissions in [["administrator"], discord.Permissions(administrator=True)]:
            with self.assertRaisesRegex(ValidationError, "explicit confirmation"):
                await RoleEngine().create_role(guild, {
                    "name": "Admin", "permissions": permissions, "confirm_administrator": True,
                })
        guild.create_role.assert_not_awaited()

    async def test_manager_controls_recheck_permissions_and_render(self):
        guild, admin = guild_fixture()
        permission_view = PermissionManagerView(guild, admin.id)
        self.assertIsInstance(permission_view.inspect_permissions, discord.ui.ChannelSelect)
        schema = TEMPLATES["community"]
        plan = builder_planner.create_plan_from_schema(guild, admin, schema)
        view = ChangePreviewView(guild, admin.id, plan)
        self.assertEqual(plan.risk_level, RiskLevel.CRITICAL)
        self.assertEqual(view.apply.label, "Confirm Administrator Grants")
        allowed = SimpleNamespace(guild_id=guild.id, user=admin)
        self.assertTrue(await ManagerView.interaction_check(view, allowed))
        admin.guild_permissions = discord.Permissions(manage_guild=True)
        allowed.response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock())
        self.assertFalse(await ManagerView.interaction_check(view, allowed))
        allowed.response.send_message.assert_awaited_once()


class PlanComparisonTests(unittest.TestCase):
    def test_compare_checks_inherited_rule_even_when_child_has_own_override(self):
        guild, _ = guild_fixture()
        category = channel_fixture(guild, "Info", 200, discord.CategoryChannel)
        channel = channel_fixture(guild, "rules", 201)
        guild.categories = [category]
        category.channels = [channel]
        category.overwrites_for.return_value = discord.PermissionOverwrite(view_channel=False, send_messages=False)
        channel.overwrites_for.return_value = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        schema = {"categories": [{
            "name": "Info",
            "permission_overwrites": [{"role": "@everyone", "deny": ["view_channel", "send_messages"]}],
            "channels": [{"name": "rules", "permission_overwrites": [{"role": "@everyone", "allow": ["send_messages"]}]}],
        }]}
        issues = compare_permissions(guild, schema)
        self.assertEqual(len(issues), 1)
        self.assertIn("read_messages", issues[0])
        self.assertNotIn("send_messages", issues[0])

    def test_large_previews_fit_and_templates_validate(self):
        guild, admin = guild_fixture()
        for key in ("gaming", "community", "study"):
            plan = builder_planner.create_plan_from_schema(guild, admin, TEMPLATES[key])
            self.assertEqual(plan.risk_level, RiskLevel.CRITICAL)
        plan.actions = [Action(ActionType.CREATE_CHANNEL, "x" * 100) for _ in range(30)]
        plan.warnings = ["y" * 401] * 8
        self.assertTrue(all(len(field.value) <= 1024 for field in format_preview_embed(plan).fields))


    def test_ticket_log_is_private_only_for_ticket_staff_and_administrators(self):
        guild, _ = guild_fixture()
        channel = channel_fixture(guild, "ticket-log", 200)
        member_role = MagicMock(spec=discord.Role, id=201)
        member_role.permissions = discord.Permissions.none()
        guild.roles.append(member_role)
        self.assertFalse(ticket_log_is_private(channel, {201}))
        channel.permissions_for.side_effect = lambda target: (
            discord.Permissions.none() if target is guild.default_role else discord.Permissions.all()
        )
        self.assertFalse(ticket_log_is_private(channel, set()))
        self.assertTrue(ticket_log_is_private(channel, {201}))
        self.assertFalse(ticket_log_is_private(channel, {201, guild.id}))
        channel.overwrites = {discord.Object(id=202, type=discord.User): discord.PermissionOverwrite(view_channel=True)}
        self.assertFalse(ticket_log_is_private(channel, {201}))

    def test_channel_cannot_grant_administrator(self):
        with self.assertRaisesRegex(ValidationError, "server role permission"):
            schema_validator.parse_and_validate({
                "roles": [{"name": "Staff"}],
                "categories": [{"name": "Info", "permission_overwrites": [
                    {"role": "Staff", "allow": ["administrator"]},
                ]}],
            })


if __name__ == "__main__":
    unittest.main()
