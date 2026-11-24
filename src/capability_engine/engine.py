"""Standalone CapabilityEngine facade orchestrating discovery, activation, reclamation, and audit."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .activation import ActivationManager
from .audit import CapabilityAuditLedger, atomic_write_private_json
from .builtin_bundles import register_builtin_capabilities
from .context_bridge import ContextBridge
from .creation import SkillAuthoringService, ToolDevelopmentPipeline
from .discovery import DiscoveryEngine
from .evolution import CapabilityEvolutionEngine
from .models import (
    CapabilityActionResult,
    CapabilityCreationRequest,
    CapabilityEngineError,
    CapabilityKind,
    TurnCapabilityContext,
)
from .providers import ExternalProviderRegistry
from .registry import CapabilityCatalogRegistry
from .skills import SkillRegistry


def _make_action_result(
    success: bool,
    output: str,
    *,
    error: Optional[str] = None,
    data: Optional[Any] = None,
) -> Any:
    try:
        from src.harness.tools import ToolResult

        return ToolResult(success, output, error=error, data=data)
    except ImportError:
        return CapabilityActionResult(success=success, output=output, error=error, data=data)


class CapabilityEngine:
    """Self-contained subsystem managing dynamic capabilities, skills, creation, and evolution."""

    def __init__(
        self,
        *,
        storage_dir: Optional[Path] = None,
        max_active_tokens: int = 1200,
        default_idle_ttl: int = 4,
        promotion_service: Any = None,
        tool_registry: Optional[Any] = None,
        event_publisher: Optional[Any] = None,
    ) -> None:
        self.storage_dir: Optional[Path] = Path(storage_dir) if storage_dir is not None else None
        audit_path = self.storage_dir / "audit_ledger.jsonl" if self.storage_dir is not None else None
        self._requests_path: Optional[Path] = (
            self.storage_dir / "creation_requests.json" if self.storage_dir is not None else None
        )

        self.audit = CapabilityAuditLedger(storage_path=audit_path)
        self.skills = SkillRegistry(self.audit, storage_dir=self.storage_dir)
        self.catalog = CapabilityCatalogRegistry(self.skills, self.audit)
        self.discovery = DiscoveryEngine(self.catalog, self.skills, self.audit)
        self.activation = ActivationManager(
            self.catalog,
            self.skills,
            self.audit,
            max_active_tokens=max_active_tokens,
            default_idle_ttl=default_idle_ttl,
        )
        self.context_bridge = ContextBridge(self.catalog, self.skills, self.activation)
        self._creation_requests: list[CapabilityCreationRequest] = []
        if self._requests_path is not None and self._requests_path.exists():
            self._load_creation_requests()

        self.skill_authoring = SkillAuthoringService(
            self.skills,
            self.catalog,
            self.discovery,
            self.audit,
            event_publisher=event_publisher,
        )
        self.tool_development = ToolDevelopmentPipeline(
            self.catalog,
            self.activation,
            self.audit,
            promotion_service=promotion_service,
            tool_registry=tool_registry,
            event_publisher=event_publisher,
        )
        self.evolution = CapabilityEvolutionEngine(
            self.skills,
            self.catalog,
            self.discovery,
            self.activation,
            self.audit,
            creation_requests_getter=self.list_creation_requests,
            event_publisher=event_publisher,
        )
        self.providers = ExternalProviderRegistry(
            self.skills,
            self.catalog,
            self.activation,
            self.audit,
            event_publisher=event_publisher,
        )

    def prepare_turn(
        self,
        session_id: str,
        user_text: str,
        *,
        task_id: Optional[str] = None,
        enable_assisted: bool = True,
        tool_registry: Optional[Any] = None,
    ) -> TurnCapabilityContext:
        """Advance session lifecycle, apply controlled auto-activation, and build turn context."""
        self.activation.advance_turn(session_id)
        auto_activated: list[str] = []
        suggestions = []

        if enable_assisted and (user_text or "").strip():
            auto_matches, sug_matches = self.discovery.suggest_for_turn(
                user_text,
                active_ids=self.activation.active_capability_ids(session_id),
                session_id=session_id,
            )
            suggestions = sug_matches
            for match in auto_matches:
                bundle = self.catalog.get_bundle(match.item_id)
                if bundle is None or not bundle.auto_activate_allowed:
                    continue
                try:
                    self.activation.activate(
                        session_id,
                        match.item_id,
                        reason=f"Assisted low-risk match (score={match.score:.2f})",
                        task_id=task_id,
                        activation_source="assisted_matcher",
                        actor="assisted_matcher",
                    )
                    auto_activated.append(match.item_id)
                except CapabilityEngineError:
                    suggestions.append(match)

        reclamation_plan = self.activation.consume_reclamation_plan(session_id)
        transient = self.context_bridge.render_transient_context(
            session_id,
            suggestions=suggestions,
            tool_registry=tool_registry,
        )
        task_records_ctx = self.context_bridge.render_task_records(session_id)
        return TurnCapabilityContext(
            session_id=session_id,
            turn=self.activation.current_turn(session_id),
            auto_activated=tuple(auto_activated),
            suggestions=tuple(suggestions),
            active_capability_ids=self.activation.active_capability_ids(session_id),
            active_tool_names=self.activation.active_tool_names(session_id),
            reclamation_plan=reclamation_plan,
            transient_context=transient,
            task_records_context=task_records_ctx,
        )

    def handle_meta_tool_call(
        self,
        session_id: str,
        name: str,
        args: Dict[str, Any],
        *,
        task_id: Optional[str] = None,
        actor: str = "lila",
    ) -> Any:
        """Execute one of the 4 Capability Engine control operations cleanly."""
        try:
            if name == "discover_capabilities":
                return self._meta_discover(session_id, args, actor=actor)
            if name == "activate_capability":
                return self._meta_activate(session_id, args, default_task_id=task_id, actor=actor)
            if name == "deactivate_capability":
                return self._meta_deactivate(session_id, args, actor=actor)
            if name == "request_capability_creation":
                return self._meta_request_creation(session_id, args, actor=actor)
            return _make_action_result(False, "", error=f"Unknown capability control action: {name}")
        except CapabilityEngineError as exc:
            return _make_action_result(False, "", error=str(exc))

    def _meta_discover(self, session_id: str, args: Dict[str, Any], *, actor: str) -> Any:
        query = str(args.get("query") or "").strip()
        if not query:
            return _make_action_result(False, "", error="discover_capabilities requires a non-empty 'query'.")
        kind = str(args.get("kind") or "all").strip()
        limit = int(args.get("limit") or 5)
        matches = self.discovery.discover(
            query,
            kind=kind,
            limit=limit,
            active_ids=self.activation.active_capability_ids(session_id),
            session_id=session_id,
            actor=actor,
        )
        data = [
            {
                "id": m.item_id,
                "kind": m.kind.value,
                "title": m.title,
                "summary": m.summary,
                "score": m.score,
                "estimated_tokens": m.estimated_tokens,
                "already_active": m.already_active,
                "requires_approval_tools": list(m.requires_approval_tools),
            }
            for m in matches
        ]
        if not matches:
            return _make_action_result(
                True,
                f"No matching capabilities found for {query!r}. Use request_capability_creation if a new skill or tool is needed.",
                data={"matches": []},
            )
        lines = [f"Found {len(matches)} match(es) for {query!r}:"]
        for m in matches:
            status = " [ACTIVE]" if m.already_active else ""
            lines.append(f"- {m.item_id} ({m.kind.value}, ~{m.estimated_tokens} tok, score={m.score:.2f}){status}: {m.summary}")
        return _make_action_result(True, "\n".join(lines), data={"matches": data})

    def _meta_activate(
        self,
        session_id: str,
        args: Dict[str, Any],
        *,
        default_task_id: Optional[str],
        actor: str,
    ) -> Any:
        cap_id = str(args.get("capability_id") or "").strip()
        if not cap_id:
            return _make_action_result(False, "", error="activate_capability requires 'capability_id'.")
        reason = str(args.get("reason") or "")
        task_id = str(args.get("task_id") or default_task_id or "").strip() or None
        ttl_raw = args.get("ttl_turns")
        ttl_turns = int(ttl_raw) if ttl_raw is not None else None
        handle = self.activation.activate(
            session_id,
            cap_id,
            reason=reason,
            task_id=task_id,
            ttl_turns=ttl_turns,
            activation_source="model",
            actor=actor,
        )
        return _make_action_result(
            True,
            f"Activated capability '{handle.capability_id}' (~{handle.token_cost} tok; "
            f"{len(handle.loaded_skills)} skill(s), {len(handle.bundle.tool_names)} tool(s)). "
            "Execution still remains governed by runtime and owner permissions.",
            data={
                "capability_id": handle.capability_id,
                "task_id": handle.task_id,
                "tools": list(handle.bundle.tool_names),
                "skills": [s.skill_id for s in handle.loaded_skills],
            },
        )

    def _meta_deactivate(self, session_id: str, args: Dict[str, Any], *, actor: str) -> Any:
        cap_id = str(args.get("capability_id") or "").strip()
        if not cap_id:
            return _make_action_result(False, "", error="deactivate_capability requires 'capability_id'.")
        summary = str(args.get("outcome_summary") or "")
        rec = self.activation.deactivate(
            session_id,
            cap_id,
            outcome_summary=summary,
            reason="explicit",
            actor=actor,
        )
        if rec is None:
            return _make_action_result(True, f"Capability '{cap_id}' was not active.", data={"deactivated": False})
        return _make_action_result(
            True,
            f"Deactivated '{cap_id}' and scheduled context reclamation. Summary saved: {rec.summary}",
            data={"deactivated": True, "task_record": rec.summary},
        )

    def _meta_request_creation(
        self, session_id: str, args: Dict[str, Any], *, actor: str
    ) -> Any:
        kind_raw = str(args.get("kind") or "capability").strip().lower()
        kind = {
            "skill": CapabilityKind.SKILL,
            "tool": CapabilityKind.TOOL,
            "capability": CapabilityKind.CAPABILITY,
        }.get(kind_raw)
        if kind is None:
            return _make_action_result(False, "", error=f"Invalid capability creation kind: {kind_raw!r}")
        title = str(args.get("title") or "").strip()
        problem = str(args.get("problem") or "").strip()
        if not title or not problem:
            return _make_action_result(False, "", error="request_capability_creation requires 'title' and 'problem'.")
        proposed_inputs = str(args.get("proposed_inputs") or "").strip()
        req = self.request_capability_creation(
            kind, title, problem, proposed_inputs=proposed_inputs, actor=actor, session_id=session_id
        )
        return _make_action_result(
            True,
            f"Logged {req.kind.value} creation request '{req.request_id}' ({req.title}). "
            "New executable tools require Phase 2 sandbox evaluation and explicit owner approval before registration.",
            data={"request_id": req.request_id, "status": req.status, "kind": req.kind.value},
        )

    def request_capability_creation(
        self,
        kind: CapabilityKind,
        title: str,
        problem: str,
        *,
        proposed_inputs: str = "",
        suggested_tests: tuple[str, ...] = (),
        actor: str = "lila",
        session_id: str = "default",
    ) -> CapabilityCreationRequest:
        """Record an audited request for a missing Skill, Tool, or Capability."""
        req = CapabilityCreationRequest(
            request_id=f"creq-{uuid.uuid4().hex[:12]}",
            kind=kind,
            title=title.strip(),
            problem_statement=problem.strip(),
            proposed_inputs=proposed_inputs.strip(),
            suggested_tests=tuple(suggested_tests),
            status="logged",
            created_at_utc=datetime.now(timezone.utc).isoformat(),
        )
        self._creation_requests.append(req)
        self._persist_creation_requests()
        self.audit.record(
            actor,
            "capability_creation_requested",
            req.request_id,
            target_kind=req.kind.value,
            details={
                "session_id": session_id,
                "title": req.title,
                "status": req.status,
            },
        )
        return req

    def list_creation_requests(self) -> list[CapabilityCreationRequest]:
        return list(self._creation_requests)

    def _persist_creation_requests(self) -> None:
        if self._requests_path is None:
            return
        payload = [
            {
                "request_id": r.request_id,
                "kind": r.kind.value,
                "title": r.title,
                "problem_statement": r.problem_statement,
                "proposed_inputs": r.proposed_inputs,
                "suggested_tests": list(r.suggested_tests),
                "status": r.status,
                "created_at_utc": r.created_at_utc,
            }
            for r in self._creation_requests
        ]
        atomic_write_private_json(self._requests_path, payload)

    def _load_creation_requests(self) -> None:
        assert self._requests_path is not None
        raw_items = json.loads(self._requests_path.read_text(encoding="utf-8"))
        self._creation_requests = [
            CapabilityCreationRequest(
                request_id=item["request_id"],
                kind=CapabilityKind(item["kind"]),
                title=item["title"],
                problem_statement=item["problem_statement"],
                proposed_inputs=item.get("proposed_inputs", ""),
                suggested_tests=tuple(item.get("suggested_tests") or ()),
                status=item.get("status", "logged"),
                created_at_utc=item.get("created_at_utc", ""),
            )
            for item in raw_items
        ]

    def set_event_publisher(self, publisher: Any) -> None:
        """Attach an SSE event publisher across Phase 2 creation and Phase 3 evolution services."""
        for svc in (self.skill_authoring, self.tool_development, self.evolution, self.providers):
            svc.set_event_publisher(publisher)

    def attach_promotion_service(
        self,
        promotion_service: Any,
        *,
        tool_registry: Optional[Any] = None,
    ) -> None:
        """Bind M10 CapabilityPromotionService + ToolRegistry to Phase 2 tool development."""
        self.tool_development.attach_promotion_service(
            promotion_service, tool_registry=tool_registry
        )

    def status_summary(self, session_id: str = "default") -> Dict[str, Any]:
        """Return compact non-sensitive status of all Capability Engine phases."""
        ok_audit, issues = self.audit.verify_integrity()
        return {
            "bundles": len(self.catalog.all_bundles()),
            "skills": len(self.skills.all_latest()),
            "callable_tools": len(self.catalog.all_tools()),
            "providers": [p.provider_id for p in self.providers.list_providers()],
            "creation_requests": len(self._creation_requests),
            "active_capabilities": list(self.activation.active_capability_ids(session_id)),
            "active_tokens": self.activation.active_token_usage(session_id),
            "audit_entries": len(self.audit.entries(limit=10000)),
            "audit_integrity_ok": ok_audit,
            "audit_issues": issues,
        }


def build_default_capability_engine(
    tool_registry: Optional[Any] = None,
    *,
    storage_dir: Optional[Path] = None,
    max_active_tokens: int = 1200,
    default_idle_ttl: int = 4,
    promotion_service: Any = None,
    event_publisher: Optional[Any] = None,
) -> CapabilityEngine:
    """Construct a CapabilityEngine pre-populated with built-in Skills and the starter catalogue."""
    engine = CapabilityEngine(
        storage_dir=storage_dir,
        max_active_tokens=max_active_tokens,
        default_idle_ttl=default_idle_ttl,
        promotion_service=promotion_service,
        tool_registry=tool_registry,
        event_publisher=event_publisher,
    )
    register_builtin_capabilities(engine.skills, engine.catalog, tool_registry=tool_registry)
    return engine
