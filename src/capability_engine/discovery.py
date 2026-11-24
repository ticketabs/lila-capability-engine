"""Model-driven and lightweight assisted discovery over Skills, Tools, and Capabilities."""

from __future__ import annotations

import re
from typing import Iterable, Literal

from .audit import CapabilityAuditLedger
from .models import CapabilityBundle, CapabilityKind, DiscoveryMatch, SkillSpec, ToolDescriptor
from .registry import CapabilityCatalogRegistry
from .skills import SkillRegistry

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")

_STOPWORDS = frozenset({
    "a", "am", "an", "and", "are", "as", "at", "be", "by", "can", "check", "could",
    "do", "does", "for", "from", "have", "hello", "help", "hi", "how", "i", "if",
    "in", "into", "is", "it", "just", "lila", "me", "my", "need", "of", "on", "or",
    "our", "please", "running", "see", "show", "some", "tell", "that", "the", "this",
    "to", "up", "use", "want", "we", "what", "when", "whether", "which", "with", "you",
    "your",
})


def _meaningful_tokens(text: str) -> set[str]:
    raw_tokens = _WORD_RE.findall((text or "").lower())
    expanded: set[str] = set()
    for tok in raw_tokens:
        for part in re.split(r"[._-]+", tok):
            if len(part) >= 2 and part not in _STOPWORDS:
                expanded.add(part)
    return expanded


def _score_candidate(
    *,
    query_lower: str,
    query_tokens: set[str],
    item_id: str,
    title: str,
    summary: str,
    tags: Iterable[str] = (),
    phrases: Iterable[str] = (),
    extra_terms: Iterable[str] = (),
) -> float:
    if not query_lower.strip():
        return 0.0

    phrase_bonus = 0.0
    for phrase in phrases:
        clean_phrase = phrase.strip().lower()
        if clean_phrase and clean_phrase in query_lower:
            phrase_bonus = max(phrase_bonus, 0.72)

    id_tokens = _meaningful_tokens(item_id)
    title_tokens = _meaningful_tokens(title)
    tag_tokens = set()
    for tag in tags:
        tag_tokens |= _meaningful_tokens(tag)
    extra_toks = set()
    for term in extra_terms:
        extra_toks |= _meaningful_tokens(term)
    summary_tokens = _meaningful_tokens(summary)

    if query_lower.strip() == item_id.lower():
        return 1.0

    if not query_tokens:
        return round(min(1.0, phrase_bonus), 4)

    strong_corpus = id_tokens | title_tokens | tag_tokens | extra_toks
    strong_hits = len(query_tokens & strong_corpus)
    summary_hits = len((query_tokens - strong_corpus) & summary_tokens)
    denom = max(1, min(len(query_tokens), 4))
    token_score = min(1.0, (strong_hits * 1.0 + summary_hits * 0.55) / denom) * 0.85

    return round(min(1.0, max(token_score, phrase_bonus + strong_hits * 0.12)), 4)


class DiscoveryEngine:
    """Ranks capabilities/skills/tools for both model queries and assisted turn matching."""

    def __init__(
        self,
        catalog: CapabilityCatalogRegistry,
        skill_registry: SkillRegistry,
        audit_ledger: CapabilityAuditLedger,
        *,
        min_score: float = 0.18,
        auto_activate_threshold: float = 0.52,
    ) -> None:
        self._catalog = catalog
        self._skills = skill_registry
        self._audit = audit_ledger
        self._min_score = float(min_score)
        self._auto_activate_threshold = float(auto_activate_threshold)

    def discover(
        self,
        query: str,
        *,
        kind: str = "all",
        limit: int = 5,
        active_ids: Iterable[str] = (),
        session_id: str = "default",
        actor: str = "lila",
    ) -> list[DiscoveryMatch]:
        """Perform model-driven discovery across Capabilities, Skills, and Tools."""
        active_set = set(active_ids)
        normalized_kind = (kind or "all").strip().lower()
        q_lower = (query or "").strip().lower()
        q_tokens = _meaningful_tokens(q_lower)
        matches: list[DiscoveryMatch] = []

        if normalized_kind in ("all", CapabilityKind.CAPABILITY.value):
            for bundle in self._catalog.all_bundles():
                m = self._score_bundle(
                    bundle, q_lower, q_tokens, active_set, source="model_query"
                )
                if m is not None and m.score >= self._min_score:
                    matches.append(m)

        if normalized_kind in ("all", CapabilityKind.SKILL.value):
            for skill in self._skills.all_latest():
                m = self._score_skill(skill, q_lower, q_tokens)
                if m is not None and m.score >= self._min_score:
                    matches.append(m)

        if normalized_kind in ("all", CapabilityKind.TOOL.value):
            for tool in self._catalog.all_tools():
                m = self._score_tool(tool, q_lower, q_tokens, active_set)
                if m is not None and m.score >= self._min_score:
                    matches.append(m)

        matches.sort(key=lambda item: (-item.score, item.estimated_tokens, item.item_id))
        bounded = matches[: max(1, min(int(limit), 12))]
        self._audit.record(
            actor,
            "capability_discovered",
            query[:120],
            target_kind=normalized_kind,
            details={
                "session_id": session_id,
                "match_ids": [m.item_id for m in bounded],
            },
        )
        return bounded

    def suggest_for_turn(
        self,
        user_text: str,
        *,
        active_ids: Iterable[str] = (),
        limit: int = 3,
        session_id: str = "default",
    ) -> tuple[list[DiscoveryMatch], list[DiscoveryMatch]]:
        """Run Assisted Discovery for one turn.

        Returns `(auto_activate_matches, suggestion_matches)`.
        Only explicitly marked low-risk bundles (`auto_activate_allowed=True`) at
        or above `auto_activate_threshold` can enter `auto_activate_matches`.
        """
        active_set = set(active_ids)
        q_lower = (user_text or "").strip().lower()
        q_tokens = _meaningful_tokens(q_lower)
        if not q_tokens:
            return [], []

        scored: list[DiscoveryMatch] = []
        for bundle in self._catalog.all_bundles():
            if bundle.capability_id in active_set:
                continue
            match = self._score_bundle(
                bundle, q_lower, q_tokens, active_set, source="assisted_matcher"
            )
            if match is not None and match.score >= max(self._min_score, 0.30):
                scored.append(match)

        scored.sort(key=lambda item: (-item.score, item.estimated_tokens, item.item_id))
        top = scored[: max(1, min(int(limit), 5))]
        auto_activate: list[DiscoveryMatch] = []
        suggestions: list[DiscoveryMatch] = []
        for item in top:
            if item.auto_activate_allowed and item.score >= self._auto_activate_threshold:
                auto_activate.append(item)
            else:
                suggestions.append(item)

        if top:
            self._audit.record(
                "assisted_matcher",
                "turn_capabilities_matched",
                session_id,
                target_kind="session",
                details={
                    "auto_activate": [m.item_id for m in auto_activate],
                    "suggested": [m.item_id for m in suggestions],
                },
            )
        return auto_activate, suggestions

    def _approval_tools_for_bundle(self, bundle: CapabilityBundle) -> tuple[str, ...]:
        tools: list[str] = []
        for tname in bundle.tool_names:
            desc = self._catalog.get_tool(tname)
            if desc is None or desc.permission_hint != "auto":
                tools.append(tname)
        return tuple(tools)

    def _score_bundle(
        self,
        bundle: CapabilityBundle,
        q_lower: str,
        q_tokens: set[str],
        active_set: set[str],
        *,
        source: Literal["model_query", "assisted_matcher"],
    ) -> DiscoveryMatch | None:
        score = _score_candidate(
            query_lower=q_lower,
            query_tokens=q_tokens,
            item_id=bundle.capability_id,
            title=bundle.title,
            summary=bundle.summary,
            tags=bundle.tags,
            phrases=bundle.trigger_phrases,
            extra_terms=(*bundle.tool_names, *bundle.skill_ids),
        )
        if score <= 0.0:
            return None
        return DiscoveryMatch(
            item_id=bundle.capability_id,
            kind=CapabilityKind.CAPABILITY,
            title=bundle.title,
            summary=bundle.summary,
            score=score,
            source=source,
            estimated_tokens=bundle.estimated_total_tokens,
            already_active=bundle.capability_id in active_set,
            auto_activate_allowed=bundle.auto_activate_allowed,
            requires_approval_tools=self._approval_tools_for_bundle(bundle),
        )

    @staticmethod
    def _score_skill(
        skill: SkillSpec, q_lower: str, q_tokens: set[str]
    ) -> DiscoveryMatch | None:
        score = _score_candidate(
            query_lower=q_lower,
            query_tokens=q_tokens,
            item_id=skill.skill_id,
            title=skill.title,
            summary=skill.summary,
            tags=skill.tags,
            extra_terms=skill.recommended_tools,
        )
        if score <= 0.0:
            return None
        return DiscoveryMatch(
            item_id=skill.skill_id,
            kind=CapabilityKind.SKILL,
            title=skill.title,
            summary=skill.summary,
            score=score,
            source="model_query",
            estimated_tokens=skill.estimated_tokens,
            already_active=False,
            auto_activate_allowed=False,
        )

    def _score_tool(
        self,
        tool: ToolDescriptor,
        q_lower: str,
        q_tokens: set[str],
        active_set: set[str],
    ) -> DiscoveryMatch | None:
        score = _score_candidate(
            query_lower=q_lower,
            query_tokens=q_tokens,
            item_id=tool.name,
            title=tool.name,
            summary=tool.summary,
            tags=(tool.category, *tool.trigger_keywords),
        )
        if score <= 0.0:
            return None
        parent_bundles = self._catalog.bundles_for_tool(tool.name)
        return DiscoveryMatch(
            item_id=tool.name,
            kind=CapabilityKind.TOOL,
            title=f"{tool.name} (bundle: {', '.join(parent_bundles) or tool.capability_id})",
            summary=tool.summary,
            score=score,
            source="model_query",
            estimated_tokens=tool.estimated_schema_tokens,
            already_active=any(b in active_set for b in parent_bundles),
            auto_activate_allowed=False,
            requires_approval_tools=(tool.name,) if tool.permission_hint != "auto" else (),
        )
