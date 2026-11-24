"""Phase 3 Capability Evolution: Gap detection, regression-checked skill evolution, and rollback."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from .activation import ActivationManager
from .audit import CapabilityAuditLedger
from .creation import SkillScenario, evaluate_skill_scenarios
from .discovery import DiscoveryEngine
from .models import (
    CapabilityCreationRequest,
    CapabilityKind,
    CapabilityNotFoundError,
    SkillSpec,
    estimate_token_count,
)
from .registry import CapabilityCatalogRegistry
from .skills import SkillRegistry, validate_skill_content


@dataclass(frozen=True)
class CapabilityGapReport:
    """Identified operational gap detected from creation requests, traces, or blocks."""

    gap_id: str
    gap_type: str  # "missing_capability" | "unreliable_tool" | "discovery_or_activation_gap"
    target: str
    evidence: str
    recommended_action: str


@dataclass(frozen=True)
class EvolutionComparison:
    """Side-by-side comparison between baseline and evolved skill versions."""

    baseline_version: str
    candidate_version: str
    baseline_scenarios_passed: int
    candidate_scenarios_passed: int
    scenarios_total: int
    baseline_tokens: int
    candidate_tokens: int


@dataclass(frozen=True)
class SkillEvolutionResult:
    """Outcome of attempting to evolve an existing skill version."""

    skill_id: str
    accepted: bool
    active_version: str
    comparison: EvolutionComparison
    reason: str
    scenario_failures: tuple[str, ...] = ()


class CapabilityEvolutionEngine:
    """Detects capability gaps, benchmarks evolved skills against baselines, and rolls back cleanly."""

    def __init__(
        self,
        skill_registry: SkillRegistry,
        catalog: CapabilityCatalogRegistry,
        discovery: DiscoveryEngine,
        activation: ActivationManager,
        audit_ledger: CapabilityAuditLedger,
        *,
        creation_requests_getter: Callable[[], list[CapabilityCreationRequest]],
        event_publisher: Optional[Callable[[str, dict[str, Any]], Any]] = None,
    ) -> None:
        self._skills = skill_registry
        self._catalog = catalog
        self._discovery = discovery
        self._activation = activation
        self._audit = audit_ledger
        self._get_creation_requests = creation_requests_getter
        self._publish = event_publisher

    def set_event_publisher(self, publisher: Optional[Callable[[str, dict[str, Any]], Any]]) -> None:
        self._publish = publisher

    def analyze_gaps(
        self,
        *,
        trace_entries: Optional[Iterable[dict[str, Any]]] = None,
        trace_path: Optional[Path] = None,
        failure_threshold: int = 2,
        inactive_block_threshold: int = 2,
    ) -> list[CapabilityGapReport]:
        """Detect missing capabilities, unreliable tools, and discovery/activation bottlenecks."""
        reports: list[CapabilityGapReport] = []

        # 1. Explicit creation requests logged during tasks
        for req in self._get_creation_requests():
            if req.status in ("promoted", "rejected"):
                continue
            reports.append(
                CapabilityGapReport(
                    gap_id=f"gap-req-{req.request_id}",
                    gap_type="missing_capability",
                    target=req.title,
                    evidence=f"Creation request {req.request_id} ({req.kind.value}): {req.problem_statement[:120]}",
                    recommended_action=f"Author new {req.kind.value} addressing '{req.title}'.",
                )
            )

        # 2. Repeated tool execution failures in Harness traces
        parsed_traces = list(trace_entries) if trace_entries is not None else self._load_trace_file(trace_path)
        tool_counts: dict[str, dict[str, int]] = {}
        for item in parsed_traces:
            tool = str(item.get("tool") or "").strip()
            if not tool:
                continue
            stage = str(item.get("lifecycle_stage") or "").lower()
            counts = tool_counts.setdefault(tool, {"total": 0, "failed": 0})
            counts["total"] += 1
            if stage == "failed":
                counts["failed"] += 1

        for tool, stats in sorted(tool_counts.items()):
            if stats["failed"] >= failure_threshold and (stats["failed"] / max(1, stats["total"])) >= 0.4:
                reports.append(
                    CapabilityGapReport(
                        gap_id=f"gap-tool-{tool}",
                        gap_type="unreliable_tool",
                        target=tool,
                        evidence=f"Tool '{tool}' failed {stats['failed']}/{stats['total']} recent trace invocations.",
                        recommended_action=f"Improve guidance skill or develop safer tool variant for '{tool}'.",
                    )
                )

        # 3. Repeated inactive-tool blocks in CapabilityAuditLedger
        blocked_counts: dict[str, int] = {}
        for entry in self._audit.entries(limit=500):
            if entry.action == "execution_blocked_inactive":
                blocked_counts[entry.target_id] = blocked_counts.get(entry.target_id, 0) + 1
        for tool, count in sorted(blocked_counts.items()):
            if count >= inactive_block_threshold:
                bundles = self._catalog.bundles_for_tool(tool)
                reports.append(
                    CapabilityGapReport(
                        gap_id=f"gap-act-{tool}",
                        gap_type="discovery_or_activation_gap",
                        target=tool,
                        evidence=f"Tool '{tool}' blocked {count} times while inactive (bundle: {', '.join(bundles) or 'none'}).",
                        recommended_action=f"Refine trigger phrases/discovery tags on bundle {', '.join(bundles) or tool}.",
                    )
                )

        return reports

    @staticmethod
    def _load_trace_file(trace_path: Optional[Path]) -> list[dict[str, Any]]:
        if trace_path is None or not Path(trace_path).is_file():
            return []
        out: list[dict[str, Any]] = []
        for raw in Path(trace_path).read_text(encoding="utf-8", errors="replace").splitlines()[-500:]:
            text = raw.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
                if isinstance(obj, dict):
                    out.append(obj)
            except Exception:
                continue
        return out

    def evolve_skill(
        self,
        skill_id: str,
        new_version: str,
        instructions_markdown: str,
        *,
        title: Optional[str] = None,
        summary: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
        recommended_tools: Optional[Sequence[str]] = None,
        scenarios: Sequence[SkillScenario] = (),
        actor: str = "lila",
    ) -> SkillEvolutionResult:
        """Compare candidate skill version against active baseline; commit only if non-regressing."""
        baseline = self._skills.get(skill_id)
        if baseline is None:
            raise CapabilityNotFoundError(f"Cannot evolve unknown skill: {skill_id!r}")

        validate_skill_content(skill_id, new_version, instructions_markdown)
        candidate_tokens = estimate_token_count(instructions_markdown.strip())
        base_passed, _ = evaluate_skill_scenarios(
            self._discovery,
            scenarios,
            default_target_id=skill_id,
        )

        # Temporarily register candidate to score candidate tags/summary with DiscoveryEngine
        cand_spec = self._skills.register_skill(
            skill_id,
            new_version,
            title.strip() if title else baseline.title,
            summary.strip() if summary else baseline.summary,
            instructions_markdown,
            tags=tuple(tags) if tags is not None else baseline.tags,
            recommended_tools=(
                tuple(recommended_tools) if recommended_tools is not None else baseline.recommended_tools
            ),
            origin="evolved",
            actor=actor,
        )
        cand_passed, failures = evaluate_skill_scenarios(
            self._discovery,
            scenarios,
            default_target_id=skill_id,
        )
        comparison = EvolutionComparison(
            baseline_version=baseline.version,
            candidate_version=cand_spec.version,
            baseline_scenarios_passed=base_passed,
            candidate_scenarios_passed=cand_passed,
            scenarios_total=len(scenarios),
            baseline_tokens=baseline.estimated_tokens,
            candidate_tokens=candidate_tokens,
        )

        if cand_passed < base_passed or failures:
            self._skills.set_active_version(skill_id, baseline.version, actor="evolution_gate")
            self._audit.record(
                actor,
                "skill_evolution_rejected",
                f"{skill_id}@{new_version}",
                target_kind=CapabilityKind.SKILL.value,
                digest=cand_spec.content_digest,
                details={
                    "baseline_version": baseline.version,
                    "baseline_passed": base_passed,
                    "candidate_passed": cand_passed,
                    "failures": failures,
                },
            )
            return SkillEvolutionResult(
                skill_id=skill_id,
                accepted=False,
                active_version=baseline.version,
                comparison=comparison,
                reason="Candidate skill regressed on discovery/scenario benchmark; preserved baseline.",
                scenario_failures=tuple(failures),
            )

        self._audit.record(
            actor,
            "skill_evolved",
            f"{skill_id}@{cand_spec.version}",
            target_kind=CapabilityKind.SKILL.value,
            digest=cand_spec.content_digest,
            details={
                "from_version": baseline.version,
                "scenarios_passed": cand_passed,
                "scenarios_total": len(scenarios),
                "token_delta": candidate_tokens - baseline.estimated_tokens,
            },
        )
        if callable(self._publish):
            self._publish(
                "capability.evolved",
                {
                    "skill_id": skill_id,
                    "from_version": baseline.version,
                    "to_version": cand_spec.version,
                    "scenarios_passed": cand_passed,
                    "scenarios_total": len(scenarios),
                },
            )
        return SkillEvolutionResult(
            skill_id=skill_id,
            accepted=True,
            active_version=cand_spec.version,
            comparison=comparison,
            reason="Candidate skill verified cleanly against baseline scenarios.",
        )

    def rollback_skill(
        self,
        skill_id: str,
        target_version: str,
        *,
        session_ids: Sequence[str] = ("default",),
        actor: str = "owner",
    ) -> SkillSpec:
        """Roll back an evolved skill to `target_version` and mark active sessions for KV reclamation."""
        prior = self._skills.get(skill_id)
        restored = self._skills.set_active_version(skill_id, target_version, actor=actor)
        reclaimed_sessions: list[str] = []
        for sid in session_ids:
            for handle in self._activation.active_handles(sid):
                if skill_id in handle.bundle.skill_ids:
                    self._activation.deactivate(
                        sid,
                        handle.capability_id,
                        outcome_summary=f"Skill '{skill_id}' rolled back to v{restored.version}.",
                        reason="skill_rollback",
                        actor=actor,
                    )
                    reclaimed_sessions.append(sid)
        self._audit.record(
            actor,
            "skill_rolled_back",
            f"{skill_id}@{restored.version}",
            target_kind=CapabilityKind.SKILL.value,
            digest=restored.content_digest,
            details={
                "from_version": prior.version if prior else None,
                "to_version": restored.version,
                "reclaimed_sessions": reclaimed_sessions,
            },
        )
        if callable(self._publish):
            self._publish(
                "capability.skill_rolled_back",
                {
                    "skill_id": skill_id,
                    "to_version": restored.version,
                    "reclaimed_sessions": reclaimed_sessions,
                },
            )
        return restored
