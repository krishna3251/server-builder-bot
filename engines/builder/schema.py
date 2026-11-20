"""Use the same structural and permission validation for both build entry points."""
from __future__ import annotations

import copy
import json
from typing import Any

from core.errors import ValidationError
from engines.guild.permission_engine import permission_engine
from services.json_builder import _validate_schema

VALID_CHANNEL_TYPES = ("text", "voice", "forum", "stage")


class SchemaValidator:
    def parse_and_validate(self, raw_input: str | dict[str, Any]) -> dict[str, Any]:
        try:
            schema = json.loads(raw_input) if isinstance(raw_input, str) else copy.deepcopy(raw_input)
            _validate_schema(schema, channel_types=VALID_CHANNEL_TYPES)
        except (ValueError, TypeError) as error:
            raise ValidationError(str(error)) from error
        categories = set()
        for category in schema.get("categories", []):
            name = category["name"].strip().casefold()
            if name in categories:
                raise ValidationError("Category names must be unique in a manager plan.")
            categories.add(name)
            if "permission_preset" in category:
                raise ValidationError("Use permission_overwrites in manager JSON plans.")
            for channel in category.get("channels", []):
                if any(key in channel for key in ("permission_preset", "permissions", "category", "category_id")):
                    raise ValidationError("Use the enclosing category and permission_overwrites in manager JSON plans.")
                if channel.get("threads"):
                    raise ValidationError("Use /setup_custom for plans that create threads.")
                if channel.get("type") == "stage" and channel.get("topic"):
                    raise ValidationError("Set a stage topic when you open a stage session.")
        return schema

    def check_security(self, schema: dict[str, Any]) -> list[str]:
        warnings = []
        for role in schema.get("roles", []):
            grants = permission_engine.check_dangerous_permissions(role.get("permissions", []))
            if grants:
                warnings.append(f"Role '{role['name']}' requests elevated permissions: {', '.join(grants)}.")
        for category in schema.get("categories", []):
            for entry in (category, *category.get("channels", [])):
                for overwrite in entry.get("permission_overwrites", []):
                    grants = permission_engine.check_dangerous_permissions(overwrite.get("allow", []))
                    if grants:
                        warnings.append(f"{entry['name']} grants {', '.join(grants)} to {overwrite['role']}.")
        return warnings


schema_validator = SchemaValidator()
