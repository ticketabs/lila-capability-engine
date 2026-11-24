"""Searchable persistent catalogue of CapabilityBundles and ToolDescriptors."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from .audit import CapabilityAuditLedger
from .models import (
    CapabilityBundle,
    CapabilityKind,
    CapabilityValidationError,
    EXCLUDED_LEGACY_TOOLS,
    ToolDescriptor,
    estimate_token_count,
)
from .skills import SkillRegistry


def _optional_default_tool_registry() -> Any:
    try:
        from src.harness.tools import get_registry

        return get_registry()
    except ImportError:
        return None


def _optional_legacy_capability_ids() -> dict[str, str]:
    try:
        from src.harness.capabilities import LEGACY_CAPABILITY_IDS

        return dict(LEGACY_CAPABILITY_IDS)
    except ImportError:
        return {}


def _bundle_digest(bundle: CapabilityBundle) -> str:
    payload = {
        "capability_id": bundle.capability_id,
        "version": bundle.version,
        "skill_ids": list(bundle.skill_ids),
        "tool_names": list(bundle.tool_names),
        "auto_activate_allowed": bundle.auto_activate_allowed,
        "risk_tier": bundle.risk_tier,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


class CapabilityCatalogRegistry:
    """Maintains persistent CapabilityBundles and read-only mirrors of Harness Tools."""

    def __init__(
        self,
        skill_registry: SkillRegistry,
        audit_ledger: CapabilityAuditLedger,
    ) -> None:
        self._skills = skill_registry
        self._audit = audit_ledger
        self._bundles: dict[str, CapabilityBundle] = {}
        self._tools: dict[str, ToolDescriptor] = {}
        self._tool_to_bundles: dict[str, list[str]] = {}

    def register_bundle(
        self,
        bundle: CapabilityBundle,
        *,
        actor: str = "system",
    ) -> CapabilityBundle:
        """Validate and register a CapabilityBundle combining skills and tools."""
        cap_id = (bundle.capability_id or "").strip()
        if not cap_id:
            raise CapabilityValidationError("CapabilityBundle.capability_id must not be empty.")
        for skill_id in bundle.skill_ids:
            if self._skills.get(skill_id) is None:
                raise CapabilityValidationError(
                    f"Capability '{cap_id}' references unknown skill '{skill_id}'."
                )
        for tool_name in bundle.tool_names:
            if tool_name in EXCLUDED_LEGACY_TOOLS:
                raise CapabilityValidationError(
                    f"Capability '{cap_id}' cannot include excluded legacy tool '{tool_name}'."
                )

        est_tokens = bundle.estimated_total_tokens
        if est_tokens <= 0:
            skill_toks = sum(
                (self._skills.get(sid).estimated_tokens if self._skills.get(sid) else 0)
                for sid in bundle.skill_ids
            )
            tool_toks = sum(
                (self._tools[t].estimated_schema_tokens if t in self._tools else 28)
                for t in bundle.tool_names
            )
            est_tokens = max(20, skill_toks + tool_toks)

        normalized = CapabilityBundle(
            capability_id=cap_id,
            version=bundle.version,
            title=bundle.title.strip() or cap_id,
            summary=bundle.summary.strip(),
            skill_ids=tuple(bundle.skill_ids),
            tool_names=tuple(bundle.tool_names),
            tags=tuple(bundle.tags),
            trigger_phrases=tuple(bundle.trigger_phrases),
            estimated_total_tokens=est_tokens,
            origin=bundle.origin,
            auto_activate_allowed=bool(bundle.auto_activate_allowed),
            risk_tier=bundle.risk_tier,
        )
        # Clean previous reverse mappings when upgrading a bundle version
        for names in self._tool_to_bundles.values():
            if cap_id in names:
                names.remove(cap_id)
        for tool_name in normalized.tool_names:
            self._tool_to_bundles.setdefault(tool_name, []).append(cap_id)

        self._bundles[cap_id] = normalized
        self._audit.record(
            actor,
            "bundle_registered",
            cap_id,
            target_kind=CapabilityKind.CAPABILITY.value,
            digest=_bundle_digest(normalized),
            details={
                "version": normalized.version,
                "tools": list(normalized.tool_names),
                "skills": list(normalized.skill_ids),
                "auto_activate_allowed": normalized.auto_activate_allowed,
            },
        )
        return normalized

    def register_tool_descriptor(
        self,
        descriptor: ToolDescriptor,
        *,
        actor: str = "system",
    ) -> ToolDescriptor:
        """Register or update a standalone ToolDescriptor without requiring a Harness ToolRegistry."""
        name = (descriptor.name or "").strip()
        if not name:
            raise CapabilityValidationError("ToolDescriptor.name must not be empty.")
        if name in EXCLUDED_LEGACY_TOOLS:
            raise CapabilityValidationError(
                f"Cannot register excluded legacy tool '{name}'."
            )
        schema_text = (
            descriptor.schema_text
            or f"{name}() [risk={descriptor.risk}]: {descriptor.summary}"
        )
        est_tokens = descriptor.estimated_schema_tokens
        if est_tokens <= 0:
            est_tokens = estimate_token_count(schema_text) + 4
        normalized = ToolDescriptor(
            name=name,
            capability_id=descriptor.capability_id or f"tool.{name}",
            category=descriptor.category or "general",
            summary=descriptor.summary,
            risk=descriptor.risk or "low",
            permission_hint=descriptor.permission_hint or "auto",
            estimated_schema_tokens=est_tokens,
            trigger_keywords=tuple(descriptor.trigger_keywords) or (name.replace("_", " "),),
            schema_text=schema_text,
        )
        self._tools[name] = normalized
        self._refresh_bundle_token_estimates()
        self._audit.record(
            actor,
            "tool_descriptor_registered",
            name,
            target_kind=CapabilityKind.TOOL.value,
            details={"capability_id": normalized.capability_id, "risk": normalized.risk},
        )
        return normalized

    def sync_from_tool_registry(
        self,
        tool_registry: Optional[Any] = None,
        *,
        actor: str = "system",
    ) -> dict[str, ToolDescriptor]:
        """Mirror callable non-disabled Tool definitions without copying execution rights."""
        registry = tool_registry if tool_registry is not None else _optional_default_tool_registry()
        if registry is None or not hasattr(registry, "all"):
            self._refresh_bundle_token_estimates()
            return dict(self._tools)

        legacy_ids = _optional_legacy_capability_ids()
        synced: dict[str, ToolDescriptor] = {}
        for tool in sorted(registry.all(), key=lambda t: getattr(t, "name", "")):
            name = getattr(tool, "name", "")
            permission = getattr(tool, "permission", "auto")
            if not name or permission == "disabled" or name in EXCLUDED_LEGACY_TOOLS:
                continue
            assigned_bundles = self._tool_to_bundles.get(name, [])
            canonical_id = (
                assigned_bundles[0]
                if assigned_bundles
                else legacy_ids.get(name, f"tool.{name}")
            )
            schema_fn = getattr(tool, "as_prompt_description", None)
            schema_text = (
                schema_fn()
                if callable(schema_fn)
                else f"{name}() [risk={getattr(tool, 'risk', 'low')}]: {getattr(tool, 'description', '')}"
            )
            raw_args = getattr(tool, "args", ()) or ()
            keywords = tuple(
                k
                for k in (
                    name.replace("_", " "),
                    getattr(tool, "category", ""),
                    *(getattr(a, "name", "") for a in raw_args),
                )
                if k
            )
            desc = ToolDescriptor(
                name=name,
                capability_id=canonical_id,
                category=getattr(tool, "category", "") or "general",
                summary=getattr(tool, "description", ""),
                risk=getattr(tool, "risk", "low"),
                permission_hint=permission,
                estimated_schema_tokens=estimate_token_count(schema_text) + 4,
                trigger_keywords=keywords,
                schema_text=schema_text,
            )
            synced[name] = desc
        self._tools = synced
        self._refresh_bundle_token_estimates()
        self._audit.record(
            actor,
            "catalog_synced",
            "tool_registry",
            target_kind="catalog",
            details={"callable_tool_count": len(synced)},
        )
        return dict(synced)

    def _refresh_bundle_token_estimates(self) -> None:
        for cap_id, bundle in list(self._bundles.items()):
            skill_toks = sum(
                (self._skills.get(sid).estimated_tokens if self._skills.get(sid) else 0)
                for sid in bundle.skill_ids
            )
            tool_toks = sum(
                (self._tools[t].estimated_schema_tokens if t in self._tools else 28)
                for t in bundle.tool_names
            )
            self._bundles[cap_id] = CapabilityBundle(
                capability_id=bundle.capability_id,
                version=bundle.version,
                title=bundle.title,
                summary=bundle.summary,
                skill_ids=bundle.skill_ids,
                tool_names=bundle.tool_names,
                tags=bundle.tags,
                trigger_phrases=bundle.trigger_phrases,
                estimated_total_tokens=max(20, skill_toks + tool_toks),
                origin=bundle.origin,
                auto_activate_allowed=bundle.auto_activate_allowed,
                risk_tier=bundle.risk_tier,
            )

    def get_bundle(self, capability_id: str) -> Optional[CapabilityBundle]:
        return self._bundles.get(capability_id)

    def all_bundles(self) -> list[CapabilityBundle]:
        return [self._bundles[k] for k in sorted(self._bundles.keys())]

    def get_tool(self, tool_name: str) -> Optional[ToolDescriptor]:
        return self._tools.get(tool_name)

    def all_tools(self) -> list[ToolDescriptor]:
        return [self._tools[k] for k in sorted(self._tools.keys())]

    def bundles_for_tool(self, tool_name: str) -> tuple[str, ...]:
        return tuple(self._tool_to_bundles.get(tool_name, ()))

    def unassigned_callable_tools(
        self,
        tool_registry: Optional[Any] = None,
    ) -> list[str]:
        """Return any callable non-excluded tools missing explicit bundle assignment."""
        registry = tool_registry if tool_registry is not None else _optional_default_tool_registry()
        missing: list[str] = []
        if registry is not None and hasattr(registry, "all"):
            for tool in sorted(registry.all(), key=lambda t: getattr(t, "name", "")):
                tname = getattr(tool, "name", "")
                if not tname or getattr(tool, "permission", "auto") == "disabled" or tname in EXCLUDED_LEGACY_TOOLS:
                    continue
                if not self._tool_to_bundles.get(tname):
                    missing.append(tname)
            return missing
        for tname in sorted(self._tools.keys()):
            if tname in EXCLUDED_LEGACY_TOOLS:
                continue
            if not self._tool_to_bundles.get(tname):
                missing.append(tname)
        return missing

    def unregister_bundle(self, capability_id: str, *, actor: str = "system") -> bool:
        """Remove a bundle and its reverse tool mappings (keeps audit history intact)."""
        bundle = self._bundles.pop(capability_id, None)
        if bundle is None:
            return False
        for names in self._tool_to_bundles.values():
            if capability_id in names:
                names.remove(capability_id)
        self._audit.record(
            actor,
            "bundle_unregistered",
            capability_id,
            target_kind=CapabilityKind.CAPABILITY.value,
            details={"tools": list(bundle.tool_names)},
        )
        return True

    def remove_tool_from_bundles(self, tool_name: str, *, actor: str = "system") -> list[str]:
        """Detach a removed/rolled-back tool from all bundles cleanly."""
        affected = list(self._tool_to_bundles.pop(tool_name, ()))
        for cap_id in affected:
            bundle = self._bundles.get(cap_id)
            if bundle is None:
                continue
            remaining = tuple(t for t in bundle.tool_names if t != tool_name)
            self._bundles[cap_id] = CapabilityBundle(
                capability_id=bundle.capability_id,
                version=bundle.version,
                title=bundle.title,
                summary=bundle.summary,
                skill_ids=bundle.skill_ids,
                tool_names=remaining,
                tags=bundle.tags,
                trigger_phrases=bundle.trigger_phrases,
                estimated_total_tokens=bundle.estimated_total_tokens,
                origin=bundle.origin,
                auto_activate_allowed=bundle.auto_activate_allowed,
                risk_tier=bundle.risk_tier,
            )
        self._tools.pop(tool_name, None)
        self._refresh_bundle_token_estimates()
        if affected:
            self._audit.record(
                actor,
                "tool_removed_from_bundles",
                tool_name,
                target_kind=CapabilityKind.TOOL.value,
                details={"affected_bundles": affected},
            )
        return affected
