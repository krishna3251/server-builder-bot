"""AI Safety Validator — Strict schema, semantic, permission, and safety validation for AI outputs."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from core.errors import ValidationError
from engines.builder.schema import schema_validator
from engines.guild.permission_engine import permission_engine

log = logging.getLogger("engines.intelligence.validator")


class AISafetyValidator:
    """Enforces multi-layer validation on all AI-generated server plans before execution."""

    def parse_and_repair_json(self, raw_text: str) -> dict[str, Any]:
        """Clean markdown formatting and parse JSON, attempting repair if needed."""
        cleaned = raw_text.strip()
        # Strip ```json ... ``` or ``` ... ```
        if "```" in cleaned:
            match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
            if match:
                cleaned = match.group(1).strip()

        # Direct JSON load
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            # Repair attempt: find first { and last }
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end != -1 and end > start:
                substring = cleaned[start : end + 1]
                try:
                    return json.loads(substring)
                except json.JSONDecodeError:
                    pass
            raise ValidationError("AI output did not contain valid JSON syntax.")

    def validate_plan(self, data: dict[str, Any]) -> tuple[bool, dict[str, Any], list[str]]:
        """Run schema, semantic, permission, and safety validation."""
        warnings: list[str] = []

        # 1. Schema structure validation
        try:
            validated = schema_validator.parse_and_validate(data)
        except ValidationError as exc:
            return False, {}, [f"Schema validation error: {exc}"]

        # 2. Safety & Permission validation
        sec_warnings = schema_validator.check_security(validated)
        warnings.extend(sec_warnings)

        # 3. Administrator safety guard
        for role in validated.get("roles", []):
            perms = role.get("permissions", [])
            if any(str(p).lower() == "administrator" for p in perms):
                warnings.append(f"AI requested Administrator for role '{role.get('name')}'. Administrator stripped for safety.")
                # Automatically strip administrator from untrusted AI output
                role["permissions"] = [p for p in perms if str(p).lower() != "administrator"]

        return True, validated, warnings


ai_safety_validator = AISafetyValidator()
