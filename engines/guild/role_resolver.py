"""Role Resolver — Maps semantic role concepts (e.g. 'moderator', 'admin', 'staff') to actual guild roles."""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass
from typing import Sequence

import discord

log = logging.getLogger("engines.guild.role_resolver")

# Semantic concept -> aliases/keywords
SEMANTIC_ROLE_MAP: dict[str, list[str]] = {
    "admin": ["admin", "administrator", "management", "lead", "head admin", "sysadmin"],
    "moderator": ["moderator", "mod", "staff", "senior moderator", "trial mod", "community team"],
    "staff": ["staff", "team", "crew", "helper", "support team", "support staff"],
    "verified": ["verified", "member", "members", "community", "citizen"],
    "unverified": ["unverified", "new", "pending", "guest"],
    "support": ["support", "tech support", "helpdesk", "ticket support", "support lead"],
    "muted": ["muted", "timeout", "silenced", "jail", "quarantine"],
    "bot": ["bot", "bots", "robot", "automation"],
    "vip": ["vip", "booster", "supporter", "premium", "donator"],
}


@dataclass
class RoleMatch:
    role: discord.Role
    confidence: float
    reason: str


@dataclass
class ResolutionResult:
    query: str
    exact_match: discord.Role | None = None
    matches: list[RoleMatch] = None  # type: ignore[assignment]
    is_ambiguous: bool = False

    def __post_init__(self) -> None:
        if self.matches is None:
            self.matches = []

    @property
    def best_role(self) -> discord.Role | None:
        if self.exact_match:
            return self.exact_match
        if self.matches and not self.is_ambiguous:
            return self.matches[0].role
        return None


class RoleResolver:
    """Intelligently resolves semantic or partial role names to concrete discord.Role objects."""

    def __init__(self) -> None:
        # Cache of guild_id -> {semantic_key: role_id}
        self._guild_cache: dict[int, dict[str, int]] = {}

    def set_cached_role(self, guild_id: int, semantic_key: str, role_id: int) -> None:
        """Explicitly bind a semantic key to a specific role ID."""
        if guild_id not in self._guild_cache:
            self._guild_cache[guild_id] = {}
        self._guild_cache[guild_id][semantic_key.lower()] = role_id

    def get_cached_role(self, guild: discord.Guild, semantic_key: str) -> discord.Role | None:
        """Retrieve pre-configured semantic role if present."""
        g_cache = self._guild_cache.get(guild.id, {})
        role_id = g_cache.get(semantic_key.lower())
        if role_id:
            return guild.get_role(role_id)
        return None

    def resolve(
        self,
        guild: discord.Guild,
        query: str,
        *,
        semantic_hint: str | None = None,
        min_confidence: float = 0.5,
    ) -> ResolutionResult:
        """Resolve a query string or semantic concept against all guild roles."""
        cleaned_query = query.strip().lower()
        hint = (semantic_hint or query).strip().lower()

        # Check explicit guild cache first
        cached = self.get_cached_role(guild, hint)
        if cached:
            return ResolutionResult(
                query=query,
                exact_match=cached,
                matches=[RoleMatch(role=cached, confidence=1.0, reason="Guild cached configuration")],
                is_ambiguous=False,
            )

        # 1. Exact name match
        for role in guild.roles:
            if role.is_default():
                continue
            if role.name.strip().lower() == cleaned_query:
                return ResolutionResult(
                    query=query,
                    exact_match=role,
                    matches=[RoleMatch(role=role, confidence=1.0, reason="Exact name match")],
                    is_ambiguous=False,
                )

        matches: list[RoleMatch] = []
        target_keywords = SEMANTIC_ROLE_MAP.get(hint, [hint])

        for role in guild.roles:
            if role.is_default():
                continue

            r_name = role.name.strip().lower()

            # Exact match with an alias
            if r_name in target_keywords:
                matches.append(RoleMatch(role=role, confidence=0.95, reason=f"Matched alias '{r_name}'"))
                continue

            # Substring match
            for kw in target_keywords:
                if kw in r_name or r_name in kw:
                    sim = difflib.SequenceMatcher(None, kw, r_name).ratio()
                    confidence = 0.7 + (sim * 0.2)
                    matches.append(RoleMatch(role=role, confidence=confidence, reason=f"Keyword '{kw}' in '{r_name}'"))
                    break
            else:
                # Fuzzy ratio
                ratio = difflib.SequenceMatcher(None, cleaned_query, r_name).ratio()
                if ratio >= min_confidence:
                    matches.append(RoleMatch(role=role, confidence=ratio * 0.8, reason="Fuzzy similarity"))

        # Sort matches by descending confidence
        matches.sort(key=lambda m: m.confidence, reverse=True)

        # Filter out duplicates
        unique_matches: list[RoleMatch] = []
        seen_roles: set[int] = set()
        for m in matches:
            if m.role.id not in seen_roles:
                seen_roles.add(m.role.id)
                unique_matches.append(m)

        # Ambiguity check: multiple top matches with close confidence
        is_ambiguous = False
        if len(unique_matches) > 1:
            top_1 = unique_matches[0].confidence
            top_2 = unique_matches[1].confidence
            # If top two candidates are within 0.15 confidence and neither is exact
            if abs(top_1 - top_2) < 0.15 and top_1 < 0.95:
                is_ambiguous = True

        return ResolutionResult(
            query=query,
            exact_match=None,
            matches=unique_matches,
            is_ambiguous=is_ambiguous,
        )


role_resolver = RoleResolver()


class RoleSelectionSelect(discord.ui.Select):
    """Dropdown for user to disambiguate multiple role matches."""

    def __init__(self, candidates: Sequence[discord.Role], placeholder: str = "Select the intended role...") -> None:
        options = [
            discord.SelectOption(
                label=role.name[:100],
                value=str(role.id),
                description=f"Members: {len(role.members)} | Color: #{role.color.value:06x}",
            )
            for role in candidates[:25]
        ]
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=options)
        self.selected_role_id: int | None = None

    async def callback(self, interaction: discord.Interaction) -> None:
        self.selected_role_id = int(self.values[0])
        role = interaction.guild.get_role(self.selected_role_id) if interaction.guild else None
        role_name = role.name if role else "Selected role"
        await interaction.response.send_message(f"✅ Selected: **{role_name}**", ephemeral=True)
        self.view.stop()  # type: ignore[union-attr]


class AmbiguousRoleView(discord.ui.View):
    """Interactive view rendered when role resolution is ambiguous."""

    def __init__(self, candidates: Sequence[discord.Role], timeout: float = 120.0) -> None:
        super().__init__(timeout=timeout)
        self.select = RoleSelectionSelect(candidates)
        self.add_item(self.select)

    @property
    def selected_role_id(self) -> int | None:
        return self.select.selected_role_id
