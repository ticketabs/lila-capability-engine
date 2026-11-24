"""Phase 2 Capability Creation: Skill authoring and M3/M10 sandboxed tool development."""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from .activation import ActivationManager
from .audit import CapabilityAuditLedger
from .discovery import DiscoveryEngine
from .models import (
    CapabilityBundle,
    CapabilityKind,
    CapabilityValidationError,
    EXCLUDED_LEGACY_TOOLS,
    META_CONTROL_TOOLS,
    SkillSpec,
)
from .registry import CapabilityCatalogRegistry, _optional_default_tool_registry
from .skills import SkillRegistry, validate_skill_content

_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_DISABLED_TOOL_NAMES = frozenset({"memory_store", "memory_recall", "memory_list"})


@dataclass(frozen=True)
class SkillScenario:
    """Discovery regression check for a newly authored or evolved Skill."""

    query: str
    expected_id: Optional[str] = None
    should_match: bool = True


@dataclass(frozen=True)
class SkillAuthoringResult:
    """Outcome of authoring and validating a procedural Skill (and optional bundle)."""

    skill: SkillSpec
    bundle: Optional[CapabilityBundle]
    scenarios_passed: int
    scenarios_total: int
    scenario_failures: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolDevelopmentResult:
    """Outcome of submitting and M3-sandbox-testing a quarantined candidate tool."""

    capability_id: str
    version: str
    bundle_id: str
    status: str
    tests_passed: bool
    security_review_passed: bool
    sandbox_output: str
    review_notes: tuple[str, ...] = ()
    awaiting_owner_approval: bool = True


def evaluate_skill_scenarios(
    discovery: DiscoveryEngine,
    scenarios: Sequence[SkillScenario],
    *,
    default_target_id: str,
    session_id: str = "scenario_eval",
) -> tuple[int, list[str]]:
    """Evaluate positive and negative discovery scenarios against current registries."""
    passed = 0
    failures: list[str] = []
    for idx, sc in enumerate(scenarios, start=1):
        matches = discovery.discover(
            sc.query,
            kind="all",
            limit=5,
            session_id=session_id,
            actor="scenario_eval",
        )
        found_ids = [m.item_id for m in matches]
        target = sc.expected_id or default_target_id
        present = target in found_ids
        if sc.should_match and present:
            passed += 1
        elif (not sc.should_match) and (not present):
            passed += 1
        else:
            expectation = "expected" if sc.should_match else "unexpected"
            failures.append(f"scenario_{idx}: {expectation} match '{target}' for query {sc.query!r}")
    return passed, failures


class SkillAuthoringService:
    """Lets Lila create versioned procedural Skills + optional capability bundles safely."""

    def __init__(
        self,
        skill_registry: SkillRegistry,
        catalog: CapabilityCatalogRegistry,
        discovery: DiscoveryEngine,
        audit_ledger: CapabilityAuditLedger,
        *,
        event_publisher: Optional[Callable[[str, dict[str, Any]], Any]] = None,
    ) -> None:
        self._skills = skill_registry
        self._catalog = catalog
        self._discovery = discovery
        self._audit = audit_ledger
        self._publish = event_publisher

    def set_event_publisher(self, publisher: Optional[Callable[[str, dict[str, Any]], Any]]) -> None:
        self._publish = publisher

    def author_skill(
        self,
        skill_id: str,
        version: str,
        title: str,
        summary: str,
        instructions_markdown: str,
        *,
        tags: Sequence[str] = (),
        recommended_tools: Sequence[str] = (),
        scenarios: Sequence[SkillScenario] = (),
        bundle_id: Optional[str] = None,
        bundle_title: str = "",
        bundle_summary: str = "",
        bundle_tools: Sequence[str] = (),
        trigger_phrases: Sequence[str] = (),
        auto_activate_allowed: bool = False,
        actor: str = "lila",
    ) -> SkillAuthoringResult:
        """Validate, register, scenario-test, and audit a newly authored Skill."""
        clean_tools = tuple(str(t).strip() for t in recommended_tools if str(t).strip())
        clean_bundle_tools = tuple(str(t).strip() for t in bundle_tools if str(t).strip())
        for tname in (*clean_tools, *clean_bundle_tools):
            if tname in EXCLUDED_LEGACY_TOOLS or tname in _DISABLED_TOOL_NAMES:
                raise CapabilityValidationError(
                    f"Authored skill/bundle cannot reference prohibited or disabled tool '{tname}'."
                )

        spec = self._skills.register_skill(
            skill_id,
            version,
            title,
            summary,
            instructions_markdown,
            tags=tuple(tags),
            recommended_tools=clean_tools,
            origin="lila_authored",
            actor=actor,
        )

        created_bundle: Optional[CapabilityBundle] = None
        if bundle_id:
            clean_bundle_id = bundle_id.strip()
            created_bundle = self._catalog.register_bundle(
                CapabilityBundle(
                    capability_id=clean_bundle_id,
                    version=version,
                    title=bundle_title.strip() or title.strip(),
                    summary=bundle_summary.strip() or summary.strip(),
                    skill_ids=(spec.skill_id,),
                    tool_names=clean_bundle_tools or clean_tools,
                    tags=tuple(tags),
                    trigger_phrases=tuple(trigger_phrases),
                    origin="lila_authored",
                    auto_activate_allowed=bool(auto_activate_allowed),
                    risk_tier="moderate" if (bundle_tools or not auto_activate_allowed) else "low",
                ),
                actor=actor,
            )

        target_id = created_bundle.capability_id if created_bundle is not None else spec.skill_id
        passed, failures = evaluate_skill_scenarios(
            self._discovery,
            scenarios,
            default_target_id=target_id,
        )
        self._audit.record(
            actor,
            "skill_authored",
            f"{spec.skill_id}@{spec.version}",
            target_kind=CapabilityKind.SKILL.value,
            digest=spec.content_digest,
            details={
                "bundle_id": created_bundle.capability_id if created_bundle else None,
                "scenarios_passed": passed,
                "scenarios_total": len(scenarios),
            },
        )
        if callable(self._publish):
            self._publish(
                "capability.skill_authored",
                {
                    "skill_id": spec.skill_id,
                    "version": spec.version,
                    "bundle_id": created_bundle.capability_id if created_bundle else None,
                    "scenarios_passed": passed,
                    "scenarios_total": len(scenarios),
                },
            )
        return SkillAuthoringResult(
            skill=spec,
            bundle=created_bundle,
            scenarios_passed=passed,
            scenarios_total=len(scenarios),
            scenario_failures=tuple(failures),
        )


class ToolDevelopmentPipeline:
    """Bridges Phase 2 candidate tool creation to M3 sandboxes and M10 owner-gated promotion."""

    def __init__(
        self,
        catalog: CapabilityCatalogRegistry,
        activation: ActivationManager,
        audit_ledger: CapabilityAuditLedger,
        *,
        promotion_service: Any = None,
        tool_registry: Optional[Any] = None,
        event_publisher: Optional[Callable[[str, dict[str, Any]], Any]] = None,
    ) -> None:
        self._catalog = catalog
        self._activation = activation
        self._audit = audit_ledger
        self._promotion = promotion_service
        self._tool_registry = tool_registry
        self._publish = event_publisher
        self._bundle_plans: dict[str, dict[str, Any]] = {}

    def attach_promotion_service(
        self,
        promotion_service: Any,
        *,
        tool_registry: Optional[Any] = None,
    ) -> None:
        self._promotion = promotion_service
        if tool_registry is not None:
            self._tool_registry = tool_registry

    def set_event_publisher(self, publisher: Optional[Callable[[str, dict[str, Any]], Any]]) -> None:
        self._publish = publisher

    @staticmethod
    def test_in_sandbox(
        code: str,
        test_code: str = "",
        *,
        timeout_seconds: float = 10.0,
    ) -> dict[str, Any]:
        """Run optional isolated build/test checks inside M3 without blocking host workflows."""
        try:
            from src.harness.isolation import execute_python_in_m3_sandbox

            outcome = execute_python_in_m3_sandbox(
                code,
                test_code=test_code,
                timeout_seconds=timeout_seconds,
            )
            return {
                "passed": outcome.state == "succeeded" and outcome.exit_code == 0,
                "state": outcome.state,
                "exit_code": outcome.exit_code,
                "backend_used": outcome.backend_used,
                "reason_code": outcome.reason_code,
                "stdout": outcome.stdout,
                "stderr": outcome.stderr,
                "credentials_redacted": outcome.credentials_redacted,
            }
        except ImportError:
            script = (code or "") + ("\n\n" + test_code if test_code else "")
            with tempfile.TemporaryDirectory(prefix="cap_sandbox_") as tmp_dir:
                proc = subprocess.run(
                    [sys.executable, "-I", "-c", script],
                    cwd=tmp_dir,
                    capture_output=True,
                    text=True,
                    timeout=max(1.0, float(timeout_seconds)),
                    env={"PATH": "/usr/bin:/bin"},
                )
            return {
                "passed": proc.returncode == 0,
                "state": "succeeded" if proc.returncode == 0 else "failed",
                "exit_code": proc.returncode,
                "backend_used": "standalone_subprocess",
                "reason_code": "ok" if proc.returncode == 0 else "nonzero_exit",
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "credentials_redacted": True,
            }

    def develop_candidate_tool(
        self,
        capability_id: str,
        version: str,
        source_code: str,
        test_code: str,
        *,
        bundle_id: str = "cap.learned-tools",
        bundle_title: str = "Self-Developed Tools",
        tags: Sequence[str] = (),
        trigger_phrases: Sequence[str] = (),
        origin: str = "lila_phase2",
        actor: str = "lila",
    ) -> ToolDevelopmentResult:
        """Quarantine, AST-check, and M3-sandbox-test a candidate tool (never promotes)."""
        if self._promotion is None:
            raise CapabilityValidationError("No M10 CapabilityPromotionService attached.")

        clean_tool_id = (capability_id or "").strip()
        if (
            not _TOOL_NAME_RE.match(clean_tool_id)
            or clean_tool_id in EXCLUDED_LEGACY_TOOLS
            or clean_tool_id in META_CONTROL_TOOLS
            or clean_tool_id in _DISABLED_TOOL_NAMES
        ):
            raise CapabilityValidationError(
                f"Invalid or reserved candidate tool name: {clean_tool_id!r}"
            )

        clean_bundle_id = (bundle_id or "cap.learned-tools").strip() or "cap.learned-tools"
        cand = self._promotion.submit_candidate(
            capability_id=clean_tool_id,
            version=version.strip(),
            source_code=source_code,
            test_code=test_code,
            provenance={"origin": origin, "actor": actor, "bundle_id": clean_bundle_id},
        )
        evaluated = self._promotion.evaluate_candidate(clean_tool_id)
        self._bundle_plans[clean_tool_id] = {
            "bundle_id": clean_bundle_id,
            "bundle_title": bundle_title.strip() or clean_bundle_id,
            "tags": tuple(tags) or ("learned", clean_tool_id),
            "trigger_phrases": tuple(trigger_phrases),
        }
        self._audit.record(
            actor,
            "tool_candidate_evaluated",
            clean_tool_id,
            target_kind=CapabilityKind.TOOL.value,
            digest=cand.source_digest,
            details={
                "version": version,
                "bundle_id": clean_bundle_id,
                "status": evaluated.status.value,
                "tests_passed": evaluated.tests_passed,
                "security_review_passed": evaluated.security_review_passed,
            },
        )
        return ToolDevelopmentResult(
            capability_id=evaluated.capability_id,
            version=evaluated.version,
            bundle_id=clean_bundle_id,
            status=evaluated.status.value,
            tests_passed=evaluated.tests_passed,
            security_review_passed=evaluated.security_review_passed,
            sandbox_output=evaluated.sandbox_output,
            review_notes=tuple(evaluated.review_notes),
            awaiting_owner_approval=True,
        )

    def promote_developed_tool(
        self,
        capability_id: str,
        *,
        owner_approved: bool,
        bundle_id: Optional[str] = None,
        bundle_title: Optional[str] = None,
        actor: str = "owner",
    ) -> dict[str, Any]:
        """Promote a verified candidate after explicit owner approval and assign its bundle."""
        if self._promotion is None:
            raise CapabilityValidationError("No M10 CapabilityPromotionService attached.")

        ver = self._promotion.promote(capability_id, owner_approved=owner_approved)
        t_reg = self._tool_registry if self._tool_registry is not None else _optional_default_tool_registry()
        plan = self._bundle_plans.get(capability_id, {})
        target_bundle_id = (bundle_id or plan.get("bundle_id") or "cap.learned-tools").strip()
        existing_bundle = self._catalog.get_bundle(target_bundle_id)
        if existing_bundle is None:
            new_bundle = CapabilityBundle(
                capability_id=target_bundle_id,
                version="1.0.0",
                title=(bundle_title or plan.get("bundle_title") or target_bundle_id),
                summary="Owner-approved self-developed tools.",
                skill_ids=(),
                tool_names=(capability_id,),
                tags=tuple(plan.get("tags") or ("learned", capability_id)),
                trigger_phrases=tuple(plan.get("trigger_phrases") or ()),
                origin="lila_phase2",
                auto_activate_allowed=False,
                risk_tier="moderate",
            )
            self._catalog.register_bundle(new_bundle, actor=actor)
        elif capability_id not in existing_bundle.tool_names:
            merged = (*existing_bundle.tool_names, capability_id)
            self._catalog.register_bundle(
                CapabilityBundle(
                    capability_id=existing_bundle.capability_id,
                    version=existing_bundle.version,
                    title=existing_bundle.title,
                    summary=existing_bundle.summary,
                    skill_ids=existing_bundle.skill_ids,
                    tool_names=merged,
                    tags=existing_bundle.tags,
                    trigger_phrases=existing_bundle.trigger_phrases,
                    origin=existing_bundle.origin,
                    auto_activate_allowed=existing_bundle.auto_activate_allowed,
                    risk_tier=existing_bundle.risk_tier,
                ),
                actor=actor,
            )

        self._catalog.sync_from_tool_registry(t_reg, actor=actor)
        self._audit.record(
            actor,
            "tool_promoted",
            capability_id,
            target_kind=CapabilityKind.TOOL.value,
            digest=ver.active_capabilities.get(capability_id, ""),
            details={"catalog_version": ver.version, "bundle_id": target_bundle_id},
        )
        if callable(self._publish):
            self._publish(
                "capability.promoted",
                {
                    "capability_id": capability_id,
                    "catalog_version": ver.version,
                    "bundle_id": target_bundle_id,
                    "active_capabilities": list(ver.active_capabilities.keys()),
                },
            )
        return {
            "catalog_version": ver.to_dict(),
            "bundle_id": target_bundle_id,
            "hot_loaded": capability_id in getattr(self._promotion, "hot_loaded_tools", ()),
        }

    def sync_after_promotion(
        self,
        capability_id: str,
        *,
        catalog_version: int,
        digest: str = "",
        actor: str = "owner",
    ) -> str:
        """Ensure a promoted tool (even promoted via legacy route) has an explicit bundle."""
        t_reg = self._tool_registry if self._tool_registry is not None else _optional_default_tool_registry()
        existing = self._catalog.bundles_for_tool(capability_id)
        target_bundle_id = existing[0] if existing else self._bundle_plans.get(capability_id, {}).get("bundle_id", "cap.learned-tools")
        if not existing:
            bundle = self._catalog.get_bundle(target_bundle_id)
            merged_tools = (*bundle.tool_names, capability_id) if bundle else (capability_id,)
            self._catalog.register_bundle(
                CapabilityBundle(
                    capability_id=target_bundle_id,
                    version=bundle.version if bundle else "1.0.0",
                    title=bundle.title if bundle else "Self-Developed Tools",
                    summary=bundle.summary if bundle else "Owner-approved self-developed tools.",
                    skill_ids=bundle.skill_ids if bundle else (),
                    tool_names=merged_tools,
                    tags=bundle.tags if bundle else ("learned", capability_id),
                    trigger_phrases=bundle.trigger_phrases if bundle else (),
                    origin=bundle.origin if bundle else "lila_phase2",
                    auto_activate_allowed=False,
                    risk_tier="moderate",
                ),
                actor=actor,
            )
        self._catalog.sync_from_tool_registry(t_reg, actor=actor)
        self._audit.record(
            actor,
            "tool_promoted",
            capability_id,
            target_kind=CapabilityKind.TOOL.value,
            digest=digest,
            details={"catalog_version": catalog_version, "bundle_id": target_bundle_id},
        )
        return target_bundle_id

    def rollback_developed_tools(
        self,
        target_version: Optional[int] = None,
        *,
        session_ids: Sequence[str] = ("default",),
        actor: str = "owner",
    ) -> dict[str, Any]:
        """Roll back M10 tool catalog, clean bundle assignments, and trigger context reclamation."""
        if self._promotion is None:
            raise CapabilityValidationError("No M10 CapabilityPromotionService attached.")

        before_tools = set(getattr(self._promotion, "hot_loaded_tools", ()))
        ver = self._promotion.rollback(target_version)
        after_tools = set(getattr(self._promotion, "hot_loaded_tools", ()))
        removed = sorted(before_tools - after_tools)
        added = sorted(after_tools - before_tools)
        affected_bundles = self.sync_after_rollback(
            removed_tools=removed,
            added_tools=added,
            catalog_version=ver.version,
            session_ids=session_ids,
            actor=actor,
        )
        if callable(self._publish):
            self._publish(
                "capability.rolled_back",
                {
                    "catalog_version": ver.version,
                    "removed_tools": removed,
                    "affected_bundles": affected_bundles,
                },
            )
        return {
            "catalog_version": ver.to_dict(),
            "removed_tools": removed,
            "affected_bundles": affected_bundles,
        }

    def sync_after_rollback(
        self,
        *,
        removed_tools: Sequence[str],
        added_tools: Sequence[str] = (),
        catalog_version: int,
        session_ids: Sequence[str] = ("default",),
        actor: str = "owner",
    ) -> list[str]:
        """Detach rolled-back tools from bundles and reclaim active session contexts."""
        affected_bundles: set[str] = set()
        for tname in removed_tools:
            affected = self._catalog.remove_tool_from_bundles(tname, actor=actor)
            affected_bundles.update(affected)
        for tname in added_tools:
            affected_bundles.add(
                self.sync_after_promotion(tname, catalog_version=catalog_version, actor=actor)
            )
        t_reg = self._tool_registry if self._tool_registry is not None else _optional_default_tool_registry()
        self._catalog.sync_from_tool_registry(t_reg, actor=actor)
        for sid in session_ids:
            for cap_id in affected_bundles:
                if cap_id in self._activation.active_capability_ids(sid):
                    self._activation.deactivate(
                        sid,
                        cap_id,
                        outcome_summary="Bundle updated during M10 catalog rollback.",
                        reason="catalog_rollback",
                        actor=actor,
                    )
        self._audit.record(
            actor,
            "tool_rolled_back",
            f"catalog_v{catalog_version}",
            target_kind="catalog",
            details={
                "removed_tools": list(removed_tools),
                "affected_bundles": sorted(affected_bundles),
            },
        )
        return sorted(affected_bundles)
