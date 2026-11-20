"""Intelligence Planner — Orchestrates AI planning with strict 5-tier fallback guarantees."""

from __future__ import annotations

import logging
from typing import Any

import discord

from core.errors import ValidationError
from engines.guild.permission_engine import PermissionPreset
from engines.intelligence.intent import intent_parser
from engines.intelligence.validator import ai_safety_validator
from services import ai_service

log = logging.getLogger("engines.intelligence.planner")

AI_SERVER_PROMPT = """You are a Discord server architect for DAMU Server Builder.
Create a complete server architecture in valid JSON matching this schema:
{
  "server_name": "Server Name",
  "roles": [
    {"name": "Admin", "color": "red", "hoist": true, "permissions": ["manage_guild", "manage_channels", "manage_roles", "kick_members", "ban_members"]},
    {"name": "Moderator", "color": "green", "hoist": true, "permissions": ["kick_members", "ban_members", "manage_messages"]},
    {"name": "Member", "color": "blue", "hoist": false, "permissions": ["send_messages", "read_messages"]}
  ],
  "categories": [
    {
      "name": "📢 INFORMATION",
      "permission_overwrites": [{"role": "@everyone", "allow": ["view_channel", "read_messages"], "deny": ["send_messages"]}],
      "channels": [
        {"name": "announcements", "type": "text", "topic": "Official news"},
        {"name": "rules", "type": "text", "topic": "Community guidelines"}
      ]
    },
    {
      "name": "💬 COMMUNITY",
      "channels": [
        {"name": "general", "type": "text", "topic": "General chat"},
        {"name": "Lounge", "type": "voice", "bitrate": 64000}
      ]
    }
  ]
}
Output ONLY the JSON object. Do not grant administrator."""


class IntelligencePlanner:
    """Multi-tiered planner that gracefully degrades from AI to rule-based presets."""

    async def plan_request(
        self,
        guild: discord.Guild,
        user: discord.User | discord.Member,
        prompt: str,
    ) -> tuple[dict[str, Any], str, list[str]]:
        """Return (schema, planner_tier, warnings). Never raises; always returns valid plan or fallback."""
        warnings: list[str] = []

        # ── LEVEL 1 & 2: AI Generation ───────────────────────────────────────
        full_prompt = f"{AI_SERVER_PROMPT}\n\nUser Request: {prompt}"
        try:
            ai_raw = await ai_service.get_ai_response(full_prompt, user.id, persona="coder", use_history=False)
            if not ai_raw.startswith("⚠️ AI error"):
                try:
                    data = ai_safety_validator.parse_and_repair_json(ai_raw)
                    valid, validated, sec_warns = ai_safety_validator.validate_plan(data)
                    if valid and validated.get("categories"):
                        warnings.extend(sec_warns)
                        return validated, "AI_PLANNER", warnings
                except ValidationError as e:
                    log.warning("AI plan validation failed (%s), dropping to rule-based fallback.", e)
        except Exception as exc:
            log.warning("AI planner failed (%s), initiating fallback.", exc)

        # ── LEVEL 3: Rule-based Intent Planner ───────────────────────────────
        intent = intent_parser.parse(prompt)
        if intent.intent_type == "create_channel":
            params = intent.parameters
            category = {
                "name": "CHANNELS",
                "channels": [{"name": params["name"], "type": params["type"]}],
            }
            preset = params["permission_preset"]
            if preset in (PermissionPreset.STAFF, PermissionPreset.TICKET):
                category["permission_overwrites"] = [{"role": "@everyone", "deny": ["view_channel"]}]
                warnings.append("Private fallback. Review which staff roles can access the new category.")
            elif preset in (PermissionPreset.ANNOUNCEMENT, PermissionPreset.VERIFIED):
                category["permission_overwrites"] = [{"role": "@everyone", "deny": ["send_messages"]}]
                warnings.append("Read-only fallback for @everyone. Add allowed posting roles in Discord if needed.")
            warnings.append("AI unavailable. Generated plan via Rule-Based Engine.")
            return {"roles": [], "categories": [category]}, "RULE_BASED", warnings

        elif intent.intent_type == "create_category":
            cat_name = intent.parameters.get("name", "Category")
            schema = {
                "server_name": None,
                "roles": [],
                "categories": [
                    {
                        "name": cat_name,
                        "channels": [
                            {"name": "general", "type": "text"},
                        ],
                    }
                ],
            }
            warnings.append("AI unavailable. Generated plan via Rule-Based Engine.")
            return schema, "RULE_BASED", warnings

        elif intent.intent_type == "build_template":
            tmpl_name = intent.parameters.get("template", "community")
            from cogs.server_builder import TEMPLATES
            preset = TEMPLATES.get(tmpl_name, TEMPLATES.get("community", {}))
            warnings.append(f"AI unavailable. Generated plan using built-in '{tmpl_name}' template.")
            return preset, "TEMPLATE_PRESET", warnings

        # ── LEVEL 4: Built-in Community Preset ───────────────────────────────
        from cogs.server_builder import TEMPLATES
        default_template = TEMPLATES.get("community", {})
        warnings.append("AI planner offline. Safe community template loaded.")
        return default_template, "FALLBACK_PRESET", warnings


intelligence_planner = IntelligencePlanner()
