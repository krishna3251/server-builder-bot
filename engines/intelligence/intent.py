"""Intent Parser — Extracts structured actions and policies from natural language requests."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from engines.guild.permission_engine import PermissionPreset


@dataclass
class UserIntent:
    intent_type: str  # create_channel, create_category, create_role, build_template, analyze_server, unknown
    confidence: float
    parameters: dict[str, Any] = field(default_factory=dict)
    raw_prompt: str = ""


class IntentParser:
    """Deterministic intent recognition engine that operates offline without AI dependency."""

    def parse(self, prompt: str) -> UserIntent:
        clean = prompt.strip().lower()

        # 1. Analyze / audit intent
        if any(term in clean for term in ("analyze", "audit", "diagnose", "check server", "inspect")):
            return UserIntent(
                intent_type="analyze_server",
                confidence=0.9,
                raw_prompt=prompt,
            )

        # 2. Template / Builder intent
        for template in ("gaming", "community", "study", "creator", "professional"):
            if template in clean and any(term in clean for term in ("server", "setup", "template", "build")):
                return UserIntent(
                    intent_type="build_template",
                    confidence=0.85,
                    parameters={"template": template},
                    raw_prompt=prompt,
                )

        # 3. Create Category intent
        cat_match = re.search(r"(?:create|add|make|new)\s+(?:a\s+)?category(?:\s+(?:named|called))?\s+['\"]?([^'\"\n]+)['\"]?", clean)
        if cat_match:
            cat_name = cat_match.group(1).strip()
            return UserIntent(
                intent_type="create_category",
                confidence=0.9,
                parameters={"name": cat_name},
                raw_prompt=prompt,
            )

        # 4. Create Channel intent
        ch_match = re.search(r"(?:create|add|make|new)\s+(?:a\s+)?(?:(voice|text|forum|stage)\s+)?channel(?:\s+(?:named|called))?\s+['\"]?([^'\"\n]+)['\"]?", clean)
        if ch_match or any(term in clean for term in ("channel", "chat")):
            ch_type = ch_match.group(1) if ch_match and ch_match.group(1) else "text"
            ch_name = ch_match.group(2).strip() if ch_match and ch_match.group(2) else "new-channel"

            # Check for policy / privacy modifiers
            preset = PermissionPreset.PUBLIC
            if "staff" in clean or "moderator" in clean or "private" in clean:
                preset = PermissionPreset.STAFF
            elif "ticket" in clean or "support" in clean:
                preset = PermissionPreset.TICKET
            elif "announcement" in clean:
                preset = PermissionPreset.ANNOUNCEMENT
            elif "member" in clean or "verified" in clean:
                preset = PermissionPreset.VERIFIED

            if "voice" in clean:
                ch_type = "voice"
            elif "forum" in clean:
                ch_type = "forum"

            return UserIntent(
                intent_type="create_channel",
                confidence=0.85,
                parameters={
                    "name": ch_name.replace(" ", "-"),
                    "type": ch_type,
                    "permission_preset": preset.value,
                },
                raw_prompt=prompt,
            )

        # 5. Role creation intent
        role_match = re.search(r"(?:create|add|make|new)\s+(?:a\s+)?role(?:\s+(?:named|called))?\s+['\"]?([^'\"\n]+)['\"]?", clean)
        if role_match:
            role_name = role_match.group(1).strip()
            return UserIntent(
                intent_type="create_role",
                confidence=0.85,
                parameters={"name": role_name},
                raw_prompt=prompt,
            )

        return UserIntent(
            intent_type="unknown",
            confidence=0.2,
            raw_prompt=prompt,
        )


intent_parser = IntentParser()
