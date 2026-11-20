"""Schema validator and normalizer for JSON server architectures."""

from __future__ import annotations

import json
import logging
from typing import Any

from core.errors import ValidationError
from engines.guild.permission_engine import permission_engine

log = logging.getLogger("engines.builder.schema")

VALID_CHANNEL_TYPES = {"text", "voice", "forum", "stage"}


class SchemaValidator:
    """Validates and normalizes server schema JSON dictionaries."""

    def parse_and_validate(self, raw_input: str | dict[str, Any]) -> dict[str, Any]:
        """Parse raw string or dict and enforce structural schema rules."""
        if isinstance(raw_input, str):
            try:
                schema = json.loads(raw_input)
            except json.JSONDecodeError as exc:
                raise ValidationError(f"Invalid JSON syntax: {exc}")
        elif isinstance(raw_input, dict):
            schema = dict(raw_input)
        else:
            raise ValidationError("Schema must be a JSON string or dictionary.")

        if not isinstance(schema, dict):
            raise ValidationError("Top-level schema must be a JSON object.")

        # Validate roles
        roles = schema.get("roles", [])
        if not isinstance(roles, list):
            raise ValidationError("'roles' must be an array.")

        seen_roles: set[str] = set()
        for idx, role in enumerate(roles):
            if not isinstance(role, dict):
                raise ValidationError(f"Role #{idx} must be an object.")
            name = role.get("name")
            if not name or not str(name).strip():
                raise ValidationError(f"Role #{idx} is missing a valid 'name'.")
            clean_name = str(name).strip()
            if clean_name in seen_roles:
                log.warning("Duplicate role name in schema: %s", clean_name)
            seen_roles.add(clean_name)

            # Check permissions
            perms = role.get("permissions", [])
            if not isinstance(perms, list):
                raise ValidationError(f"Permissions for role '{clean_name}' must be an array of strings.")

        # Validate categories & channels
        categories = schema.get("categories", [])
        if not isinstance(categories, list):
            raise ValidationError("'categories' must be an array.")

        for c_idx, cat in enumerate(categories):
            if not isinstance(cat, dict):
                raise ValidationError(f"Category #{c_idx} must be an object.")
            c_name = cat.get("name")
            if not c_name or not str(c_name).strip():
                raise ValidationError(f"Category #{c_idx} is missing a valid 'name'.")

            channels = cat.get("channels", [])
            if not isinstance(channels, list):
                raise ValidationError(f"'channels' in category '{c_name}' must be an array.")

            for ch_idx, ch in enumerate(channels):
                if not isinstance(ch, dict):
                    raise ValidationError(f"Channel #{ch_idx} in category '{c_name}' must be an object.")
                ch_name = ch.get("name")
                if not ch_name or not str(ch_name).strip():
                    raise ValidationError(f"Channel #{ch_idx} in category '{c_name}' is missing a valid 'name'.")

                ch_type = str(ch.get("type", "text")).lower()
                if ch_type not in VALID_CHANNEL_TYPES:
                    raise ValidationError(
                        f"Channel '{ch_name}' has unsupported type '{ch_type}'. Valid: {', '.join(VALID_CHANNEL_TYPES)}"
                    )

        return schema

    def check_security(self, schema: dict[str, Any]) -> list[str]:
        """Detect dangerous permissions or risky configurations in the schema."""
        warnings: list[str] = []
        for role in schema.get("roles", []):
            role_name = role.get("name", "unnamed")
            perms = role.get("permissions", [])
            dangerous = permission_engine.check_dangerous_permissions(perms)
            if "administrator" in dangerous:
                warnings.append(f"Role '{role_name}' requests Administrator permission.")
            elif dangerous:
                warnings.append(f"Role '{role_name}' requests elevated permissions: {', '.join(dangerous)}")

        return warnings


schema_validator = SchemaValidator()
