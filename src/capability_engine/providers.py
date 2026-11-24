"""Phase 3 Generic CapabilityProvider protocol & ExternalProviderRegistry (zero hardcoded connectors)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Protocol, Sequence

from .activation import ActivationManager
from .audit import CapabilityAuditLedger
from .models import CapabilityBundle, CapabilityKind, CapabilityValidationError, SkillSpec
from .registry import CapabilityCatalogRegistry
from .skills import SkillRegistry


class CapabilityProvider(Protocol):
    """Generic standalone interface external subsystems can implement optionally."""

    provider_id: str
    version: str

    def describe_skills(self) -> Sequence[SkillSpec]: ...

    def describe_bundles(self) -> Sequence[CapabilityBundle]: ...


@dataclass(frozen=True)
class ProviderRegistration:
    """Tracked registration of an external standalone capability provider."""

    provider_id: str
    version: str
    skill_ids: tuple[str, ...]
    bundle_ids: tuple[str, ...]
    registered_at_utc: str


class ExternalProviderRegistry:
    """Registers and cleanly detaches optional standalone providers without tight coupling."""

    def __init__(
        self,
        skill_registry: SkillRegistry,
        catalog: CapabilityCatalogRegistry,
        activation: ActivationManager,
        audit_ledger: CapabilityAuditLedger,
        *,
        event_publisher: Optional[Callable[[str, dict[str, Any]], Any]] = None,
    ) -> None:
        self._skills = skill_registry
        self._catalog = catalog
        self._activation = activation
        self._audit = audit_ledger
        self._publish = event_publisher
        self._providers: dict[str, ProviderRegistration] = {}

    def set_event_publisher(self, publisher: Optional[Callable[[str, dict[str, Any]], Any]]) -> None:
        self._publish = publisher

    def register_provider(
        self,
        provider: CapabilityProvider,
        *,
        tool_registry: Optional[Any] = None,
        allow_auto_activate: bool = False,
        actor: str = "owner",
    ) -> ProviderRegistration:
        """Ingest skills, optional tools, and bundles from an external provider safely."""
        pid = str(getattr(provider, "provider_id", "") or "").strip()
        pver = str(getattr(provider, "version", "") or "").strip() or "1.0.0"
        if not pid:
            raise CapabilityValidationError("CapabilityProvider.provider_id must not be empty.")

        reg_tools_fn = getattr(provider, "register_tools", None)
        if tool_registry is not None and callable(reg_tools_fn):
            reg_tools_fn(tool_registry)

        skill_ids: list[str] = []
        for s in provider.describe_skills():
            registered = self._skills.register_skill(
                s.skill_id,
                s.version,
                s.title,
                s.summary,
                s.instructions_markdown,
                tags=s.tags,
                recommended_tools=s.recommended_tools,
                resource_paths=s.resource_paths,
                origin=f"provider:{pid}",
                actor=actor,
            )
            skill_ids.append(registered.skill_id)

        bundle_ids: list[str] = []
        for b in provider.describe_bundles():
            safe_auto = bool(allow_auto_activate and b.auto_activate_allowed)
            registered_bundle = self._catalog.register_bundle(
                CapabilityBundle(
                    capability_id=b.capability_id,
                    version=b.version,
                    title=b.title,
                    summary=b.summary,
                    skill_ids=b.skill_ids,
                    tool_names=b.tool_names,
                    tags=b.tags,
                    trigger_phrases=b.trigger_phrases,
                    estimated_total_tokens=b.estimated_total_tokens,
                    origin=f"provider:{pid}",
                    auto_activate_allowed=safe_auto,
                    risk_tier=b.risk_tier,
                ),
                actor=actor,
            )
            bundle_ids.append(registered_bundle.capability_id)

        if tool_registry is not None:
            self._catalog.sync_from_tool_registry(tool_registry, actor=actor)

        rec = ProviderRegistration(
            provider_id=pid,
            version=pver,
            skill_ids=tuple(skill_ids),
            bundle_ids=tuple(bundle_ids),
            registered_at_utc=datetime.now(timezone.utc).isoformat(),
        )
        self._providers[pid] = rec
        self._audit.record(
            actor,
            "provider_registered",
            pid,
            target_kind="provider",
            details={
                "version": pver,
                "bundle_ids": list(bundle_ids),
                "skill_ids": list(skill_ids),
                "auto_activate_allowed": bool(allow_auto_activate),
            },
        )
        if callable(self._publish):
            self._publish(
                "capability.provider_registered",
                {"provider_id": pid, "version": pver, "bundle_ids": list(bundle_ids)},
            )
        return rec

    def unregister_provider(
        self,
        provider_id: str,
        *,
        session_ids: Sequence[str] = ("default",),
        actor: str = "owner",
    ) -> bool:
        """Detach a provider's bundles cleanly and trigger Context Reclamation in active sessions."""
        reg = self._providers.pop(provider_id, None)
        if reg is None:
            return False
        for sid in session_ids:
            for bundle_id in reg.bundle_ids:
                if bundle_id in self._activation.active_capability_ids(sid):
                    self._activation.deactivate(
                        sid,
                        bundle_id,
                        outcome_summary=f"External provider '{provider_id}' unregistered.",
                        reason="provider_unregistered",
                        actor=actor,
                    )
        for bundle_id in reg.bundle_ids:
            self._catalog.unregister_bundle(bundle_id, actor=actor)

        self._audit.record(
            actor,
            "provider_unregistered",
            provider_id,
            target_kind="provider",
            details={"removed_bundles": list(reg.bundle_ids)},
        )
        if callable(self._publish):
            self._publish(
                "capability.provider_unregistered",
                {"provider_id": provider_id, "removed_bundles": list(reg.bundle_ids)},
            )
        return True

    def list_providers(self) -> list[ProviderRegistration]:
        return [self._providers[k] for k in sorted(self._providers.keys())]
