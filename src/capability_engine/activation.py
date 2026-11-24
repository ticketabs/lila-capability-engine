"""Task-scoped capability lifecycle, idle fallback, and Context Reclamation tracking."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional

from .audit import CapabilityAuditLedger
from .models import (
    ActiveCapabilityHandle,
    CapabilityBudgetError,
    CapabilityEngineError,
    CapabilityKind,
    CapabilityNotFoundError,
    CapabilityState,
    ContextReclamationPlan,
    TaskRecord,
)
from .registry import CapabilityCatalogRegistry
from .skills import SkillRegistry


def _sanitize_summary(text: str, limit: int = 200) -> str:
    clean = " ".join(str(text or "").split()).strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1] + "…"


@dataclass
class _SessionState:
    current_turn: int = 0
    active: dict[str, ActiveCapabilityHandle] = field(default_factory=dict)
    active_tasks: set[str] = field(default_factory=set)
    task_records: list[TaskRecord] = field(default_factory=list)
    invoked_tools_by_cap: dict[str, set[str]] = field(default_factory=dict)
    reclamation_pending: bool = False
    pending_deactivated: list[str] = field(default_factory=list)
    pending_reclaimed_tokens: int = 0


class ActivationManager:
    """Manages active capability handles, token budgets, task scopes, and KV reclamation signals."""

    def __init__(
        self,
        catalog: CapabilityCatalogRegistry,
        skill_registry: SkillRegistry,
        audit_ledger: CapabilityAuditLedger,
        *,
        max_active_tokens: int = 1200,
        default_idle_ttl: int = 4,
    ) -> None:
        self._catalog = catalog
        self._skills = skill_registry
        self._audit = audit_ledger
        self._max_active_tokens = max(200, int(max_active_tokens))
        self._default_idle_ttl = max(1, int(default_idle_ttl))
        self._lock = threading.RLock()
        self._sessions: dict[str, _SessionState] = {}

    def _session(self, session_id: str) -> _SessionState:
        key = (session_id or "default").strip() or "default"
        return self._sessions.setdefault(key, _SessionState())

    def current_turn(self, session_id: str) -> int:
        with self._lock:
            return self._session(session_id).current_turn

    def active_handles(self, session_id: str) -> list[ActiveCapabilityHandle]:
        with self._lock:
            state = self._session(session_id)
            return [state.active[k] for k in sorted(state.active.keys())]

    def active_capability_ids(self, session_id: str) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._session(session_id).active.keys()))

    def active_tool_names(self, session_id: str) -> tuple[str, ...]:
        with self._lock:
            names: set[str] = set()
            for handle in self._session(session_id).active.values():
                if handle.state in (CapabilityState.ACTIVE, CapabilityState.EXECUTING):
                    names.update(handle.bundle.tool_names)
            return tuple(sorted(names))

    def active_token_usage(self, session_id: str) -> int:
        with self._lock:
            return sum(h.token_cost for h in self._session(session_id).active.values())

    def task_records(self, session_id: str, limit: int = 5) -> list[TaskRecord]:
        with self._lock:
            records = self._session(session_id).task_records
            if limit <= 0:
                return []
            return list(records[-limit:])

    def activate(
        self,
        session_id: str,
        capability_id: str,
        *,
        reason: str = "",
        task_id: Optional[str] = None,
        ttl_turns: Optional[int] = None,
        activation_source: str = "model",
        actor: str = "lila",
    ) -> ActiveCapabilityHandle:
        """Load a capability bundle into the session's active set (within token budget)."""
        clean_id = (capability_id or "").strip()
        bundle = self._catalog.get_bundle(clean_id)
        if bundle is None:
            raise CapabilityNotFoundError(f"Unknown capability bundle: {clean_id!r}")

        loaded_skills = tuple(
            s for sid in bundle.skill_ids if (s := self._skills.get(sid)) is not None
        )
        loaded_tools = tuple(
            t for tname in bundle.tool_names if (t := self._catalog.get_tool(tname)) is not None
        )
        ttl = max(1, int(ttl_turns if ttl_turns is not None else self._default_idle_ttl))

        with self._lock:
            state = self._session(session_id)
            clean_task_id = (task_id or "").strip() or None
            if clean_task_id:
                state.active_tasks.add(clean_task_id)

            existing = state.active.get(clean_id)
            if existing is not None:
                existing.last_used_turn = state.current_turn
                existing.ttl_turns = max(existing.ttl_turns, ttl)
                if clean_task_id and not existing.task_id:
                    existing.task_id = clean_task_id
                return existing

            needed = bundle.estimated_total_tokens
            if needed > self._max_active_tokens:
                raise CapabilityBudgetError(
                    f"Capability '{clean_id}' requires ~{needed} tokens, "
                    f"exceeding max active budget ({self._max_active_tokens})."
                )

            self._make_room_locked(session_id, state, needed)
            handle = ActiveCapabilityHandle(
                capability_id=clean_id,
                state=CapabilityState.ACTIVE,
                activated_turn=state.current_turn,
                last_used_turn=state.current_turn,
                ttl_turns=ttl,
                token_cost=needed,
                bundle=bundle,
                task_id=clean_task_id,
                activation_source=activation_source,
                loaded_skills=loaded_skills,
                loaded_tools=loaded_tools,
            )
            state.active[clean_id] = handle
            state.invoked_tools_by_cap[clean_id] = set()
            self._audit.record(
                actor,
                "capability_activated",
                clean_id,
                target_kind=CapabilityKind.CAPABILITY.value,
                details={
                    "session_id": session_id,
                    "task_id": clean_task_id,
                    "reason": _sanitize_summary(reason, 120),
                    "activation_source": activation_source,
                    "token_cost": needed,
                },
            )
            return handle

    def _make_room_locked(self, session_id: str, state: _SessionState, needed: int) -> None:
        used = sum(h.token_cost for h in state.active.values())
        if used + needed <= self._max_active_tokens:
            return
        candidates = sorted(
            (h for h in state.active.values() if not h.executing_tools),
            key=lambda h: (1 if h.task_id in state.active_tasks else 0, h.last_used_turn, h.activated_turn),
        )
        for cand in candidates:
            if used + needed <= self._max_active_tokens:
                break
            self._unload_handle_locked(
                session_id,
                state,
                cand.capability_id,
                outcome_summary="Evicted from active set due to capability token budget.",
                reason="budget_lru_evicted",
                actor="system",
            )
            used = sum(h.token_cost for h in state.active.values())
        if used + needed > self._max_active_tokens:
            raise CapabilityBudgetError(
                f"Insufficient active capability budget ({used} used + {needed} needed > {self._max_active_tokens})."
            )

    def advance_turn(self, session_id: str) -> list[TaskRecord]:
        """Advance turn counter, keeping task-scoped handles alive and retiring expired idle handles."""
        expired: list[TaskRecord] = []
        with self._lock:
            state = self._session(session_id)
            state.current_turn += 1
            for cap_id, handle in list(state.active.items()):
                if handle.executing_tools:
                    continue
                if handle.task_id and handle.task_id in state.active_tasks:
                    continue
                if (state.current_turn - handle.last_used_turn) >= handle.ttl_turns:
                    rec = self._unload_handle_locked(
                        session_id,
                        state,
                        cap_id,
                        outcome_summary="Automatically deactivated after idle turn timeout.",
                        reason="idle_ttl_expired",
                        actor="system",
                    )
                    if rec is not None:
                        expired.append(rec)
        return expired

    def complete_task(
        self,
        session_id: str,
        task_id: str,
        *,
        outcome_summary: str = "",
        actor: str = "system",
    ) -> list[TaskRecord]:
        """Deactivate all capabilities scoped to a completed task and trigger reclamation."""
        clean_task_id = (task_id or "").strip()
        if not clean_task_id:
            return []
        unloaded: list[TaskRecord] = []
        with self._lock:
            state = self._session(session_id)
            state.active_tasks.discard(clean_task_id)
            for cap_id, handle in list(state.active.items()):
                if handle.task_id != clean_task_id or handle.executing_tools:
                    continue
                rec = self._unload_handle_locked(
                    session_id,
                    state,
                    cap_id,
                    outcome_summary=outcome_summary or f"Completed task {clean_task_id}.",
                    reason="task_completed",
                    actor=actor,
                )
                if rec is not None:
                    unloaded.append(rec)
        return unloaded

    def deactivate(
        self,
        session_id: str,
        capability_id: str,
        *,
        outcome_summary: str = "",
        reason: str = "explicit",
        actor: str = "lila",
    ) -> Optional[TaskRecord]:
        """Explicitly deactivate one capability and mark KV reclamation pending."""
        clean_id = (capability_id or "").strip()
        with self._lock:
            state = self._session(session_id)
            handle = state.active.get(clean_id)
            if handle is None:
                return None
            if handle.executing_tools:
                raise CapabilityEngineError(
                    f"Cannot deactivate '{clean_id}' while tools are executing: {sorted(handle.executing_tools)}"
                )
            return self._unload_handle_locked(
                session_id,
                state,
                clean_id,
                outcome_summary=outcome_summary,
                reason=reason,
                actor=actor,
            )

    def reset_session(self, session_id: str, *, actor: str = "owner") -> list[TaskRecord]:
        """Clear all transient capabilities and task bindings (for /new session reset)."""
        cleared: list[TaskRecord] = []
        with self._lock:
            state = self._session(session_id)
            state.active_tasks.clear()
            for cap_id in list(state.active.keys()):
                handle = state.active[cap_id]
                handle.executing_tools.clear()
                rec = self._unload_handle_locked(
                    session_id,
                    state,
                    cap_id,
                    outcome_summary="Session reset (/new); transient capabilities cleared.",
                    reason="session_reset",
                    actor=actor,
                )
                if rec is not None:
                    cleared.append(rec)
            state.reclamation_pending = True
            self._audit.record(
                actor,
                "session_reset",
                session_id,
                target_kind="session",
                details={"cleared_count": len(cleared)},
            )
        return cleared

    def _unload_handle_locked(
        self,
        session_id: str,
        state: _SessionState,
        cap_id: str,
        *,
        outcome_summary: str,
        reason: str,
        actor: str,
    ) -> Optional[TaskRecord]:
        handle = state.active.pop(cap_id, None)
        if handle is None:
            return None
        invoked = tuple(sorted(state.invoked_tools_by_cap.pop(cap_id, set())))
        summary = _sanitize_summary(
            outcome_summary or f"Deactivated {cap_id} ({reason}).",
            limit=200,
        )
        rec = TaskRecord(
            capability_id=cap_id,
            started_turn=handle.activated_turn,
            ended_turn=state.current_turn,
            summary=summary,
            task_id=handle.task_id,
            tools_invoked=invoked,
            succeeded=reason != "budget_lru_evicted",
        )
        state.task_records.append(rec)
        if len(state.task_records) > 20:
            state.task_records = state.task_records[-20:]
        state.reclamation_pending = True
        if cap_id not in state.pending_deactivated:
            state.pending_deactivated.append(cap_id)
        state.pending_reclaimed_tokens += handle.token_cost
        self._audit.record(
            actor,
            "capability_deactivated",
            cap_id,
            target_kind=CapabilityKind.CAPABILITY.value,
            details={
                "session_id": session_id,
                "reason": reason,
                "task_id": handle.task_id,
                "tools_invoked": list(invoked),
                "reclaimed_tokens_estimate": handle.token_cost,
            },
        )
        return rec

    def mark_executing(
        self,
        session_id: str,
        tool_name: str,
        executing: bool,
    ) -> None:
        """Track ACTIVE <-> EXECUTING transitions for tools inside active bundles."""
        with self._lock:
            state = self._session(session_id)
            for cap_id, handle in state.active.items():
                if tool_name not in handle.bundle.tool_names:
                    continue
                if executing:
                    handle.executing_tools.add(tool_name)
                    handle.state = CapabilityState.EXECUTING
                    handle.last_used_turn = state.current_turn
                    state.invoked_tools_by_cap.setdefault(cap_id, set()).add(tool_name)
                else:
                    handle.executing_tools.discard(tool_name)
                    if not handle.executing_tools:
                        handle.state = CapabilityState.ACTIVE

    def consume_reclamation_plan(self, session_id: str) -> ContextReclamationPlan:
        """Return pending KV/context reclamation requirements and clear pending flags."""
        with self._lock:
            state = self._session(session_id)
            if not state.reclamation_pending:
                return ContextReclamationPlan(
                    reclamation_required=False,
                    restore_static_prefix=False,
                    preserved_task_records=tuple(state.task_records[-5:]),
                )
            plan = ContextReclamationPlan(
                reclamation_required=True,
                restore_static_prefix=True,
                deactivated_ids=tuple(state.pending_deactivated),
                reclaimed_tokens_estimate=state.pending_reclaimed_tokens,
                preserved_task_records=tuple(state.task_records[-5:]),
            )
            state.reclamation_pending = False
            state.pending_deactivated.clear()
            state.pending_reclaimed_tokens = 0
            self._audit.record(
                "system",
                "context_reclaimed",
                session_id,
                target_kind="session",
                details={
                    "deactivated_ids": list(plan.deactivated_ids),
                    "reclaimed_tokens_estimate": plan.reclaimed_tokens_estimate,
                },
            )
            return plan
