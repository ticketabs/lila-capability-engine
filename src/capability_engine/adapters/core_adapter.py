"""CoreCapabilityAdapter bridging LilaCore turn assembly, ASI reclamation, and CapabilityEngine."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Sequence

from ..engine import CapabilityEngine
from ..models import TurnCapabilityContext


class CoreCapabilityAdapter:
    """Keeps canonical chat history separate from transient capabilities and handles KV reclamation."""

    def __init__(
        self,
        engine: CapabilityEngine,
        *,
        tool_registry: Optional[Any] = None,
    ) -> None:
        self._engine = engine
        self._tool_registry = tool_registry

    def build_static_meta_section(self) -> str:
        """Return the tiny immutable meta-control section safe to bake into `.asip` prefix."""
        return self._engine.context_bridge.render_meta_tools_prompt()

    def build_turn_messages(
        self,
        session_id: str,
        static_prefix: str,
        user_text: str,
        canonical_history: Sequence[Dict[str, Any]],
        *,
        dynamic_suffix: str = "",
        task_id: Optional[str] = None,
        enable_assisted: bool = True,
        reclaim_kv_callback: Optional[Callable[[str], Any]] = None,
    ) -> tuple[list[Dict[str, Any]], TurnCapabilityContext]:
        """Prepare one turn's messages without contaminating canonical conversation history.

        When a prior deactivation or budget eviction occurred, `reclaim_kv_callback(static_prefix)`
        is invoked so ASI restores the clean saved static `.asip` prefix and purges stale
        unloaded capability KV slots before evaluating this rebuilt turn context.
        """
        turn_ctx = self._engine.prepare_turn(
            session_id,
            user_text,
            task_id=task_id,
            enable_assisted=enable_assisted,
            tool_registry=self._tool_registry,
        )
        if turn_ctx.reclamation_plan.reclamation_required and callable(reclaim_kv_callback):
            reclaim_kv_callback(static_prefix)

        parts = [static_prefix]
        if turn_ctx.transient_context:
            parts.append(turn_ctx.transient_context)
        if turn_ctx.task_records_context:
            parts.append(turn_ctx.task_records_context)
        if dynamic_suffix and dynamic_suffix.strip():
            parts.append(dynamic_suffix.strip())
        system_content = "\n\n".join(p for p in parts if p)

        messages: list[Dict[str, Any]] = [
            {
                "role": "system",
                "content": system_content,
                "static_prefix": static_prefix,
            }
        ]
        for msg in canonical_history:
            messages.append(dict(msg))
        messages.append({"role": "user", "content": user_text})
        return messages, turn_ctx

    def on_task_completed(
        self,
        session_id: str,
        task_id: str,
        *,
        outcome_summary: str = "",
    ) -> None:
        """Unload task-scoped capabilities cleanly when a task ends."""
        self._engine.activation.complete_task(
            session_id,
            task_id,
            outcome_summary=outcome_summary,
            actor="core_adapter",
        )

    def on_new_session(self, session_id: str) -> None:
        """Handle `/new` session reset: wipe transient capabilities and flag reclamation."""
        self._engine.activation.reset_session(session_id, actor="owner")
