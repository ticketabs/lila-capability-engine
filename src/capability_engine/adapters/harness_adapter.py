"""HarnessCapabilityAdapter enforcing strict active-only tool gating and control routing."""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

from ..engine import CapabilityEngine
from ..models import EXCLUDED_LEGACY_TOOLS, META_CONTROL_TOOLS


class HarnessCapabilityAdapter:
    """Connects HarnessExecutor to CapabilityEngine while preserving Harness/OIA authority."""

    def __init__(
        self,
        engine: CapabilityEngine,
        *,
        strict_enforcement: bool = True,
        allow_legacy_inactive_tools: bool = False,
        system_exceptions: Iterable[str] = (),
    ) -> None:
        self._engine = engine
        self._strict_enforcement = bool(strict_enforcement)
        self._allow_legacy_inactive_tools = bool(allow_legacy_inactive_tools)
        self._system_exceptions = frozenset(str(x).strip() for x in system_exceptions if str(x).strip())

    @staticmethod
    def is_meta_control_tool(tool_name: str) -> bool:
        """Return True iff `tool_name` is one of the 4 Capability Engine control ops."""
        return tool_name in META_CONTROL_TOOLS

    def execute_meta_tool(
        self,
        session_id: str,
        tool_name: str,
        args: Dict[str, Any],
        *,
        task_id: Optional[str] = None,
    ) -> Any:
        """Run a Capability Engine control operation without granting tool execution rights."""
        return self._engine.handle_meta_tool_call(
            session_id,
            tool_name,
            args,
            task_id=task_id,
            actor="lila",
        )

    def check_tool_active(self, session_id: str, tool_name: str) -> tuple[bool, str]:
        """Strictly require active capability membership unless an explicit exception applies."""
        if tool_name in EXCLUDED_LEGACY_TOOLS:
            self._engine.audit.record(
                "harness",
                "execution_blocked_excluded",
                tool_name,
                target_kind="tool",
                details={"session_id": session_id},
            )
            return False, f"Tool '{tool_name}' is excluded from capability execution."

        if tool_name in self._system_exceptions:
            return True, "system_exception"

        active_tools = self._engine.activation.active_tool_names(session_id)
        if tool_name in active_tools:
            return True, "active"

        if (not self._strict_enforcement) or self._allow_legacy_inactive_tools:
            self._engine.audit.record(
                "harness",
                "legacy_inactive_tool_allowed",
                tool_name,
                target_kind="tool",
                details={"session_id": session_id},
            )
            return True, "legacy_compatibility_fallback"

        bundles = self._engine.catalog.bundles_for_tool(tool_name)
        self._engine.audit.record(
            "harness",
            "execution_blocked_inactive",
            tool_name,
            target_kind="tool",
            details={"session_id": session_id, "required_bundles": list(bundles)},
        )
        hint = f" (bundle: {', '.join(bundles)})" if bundles else ""
        return (
            False,
            f"Tool '{tool_name}' is inactive{hint}. Discover and activate the required capability first.",
        )

    def mark_executing(self, session_id: str, tool_name: str, executing: bool) -> None:
        """Propagate ACTIVE <-> EXECUTING transitions to ActivationManager."""
        self._engine.activation.mark_executing(session_id, tool_name, executing)
