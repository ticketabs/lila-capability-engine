"""4-layer prompt/context builder separating immutable prefix from transient capabilities."""

from __future__ import annotations

from typing import Any, Iterable, Optional

from .activation import ActivationManager
from .models import DiscoveryMatch, EXCLUDED_LEGACY_TOOLS
from .registry import CapabilityCatalogRegistry, _optional_default_tool_registry
from .skills import SkillRegistry


class ContextBridge:
    """Renders static meta-controls, ephemeral Layer 3 capabilities, and Layer 4 task records."""

    def __init__(
        self,
        catalog: CapabilityCatalogRegistry,
        skill_registry: SkillRegistry,
        activation_manager: ActivationManager,
    ) -> None:
        self._catalog = catalog
        self._skills = skill_registry
        self._activation = activation_manager

    @staticmethod
    def render_meta_tools_prompt() -> str:
        """Render the small immutable Capability Engine meta-interface (~110 tokens)."""
        return "\n".join([
            "CAPABILITY ENGINE CONTROLS (always available via <tool_call> JSON)",
            "Control operations adjust active capabilities only and never grant tool execution permission.",
            "- discover_capabilities(query, kind?, limit?): search tools, skills, and capabilities.",
            "- activate_capability(capability_id, reason?, task_id?, ttl_turns?): load capability skill + schemas.",
            "- deactivate_capability(capability_id, outcome_summary?): unload capability when finished.",
            "- request_capability_creation(kind, title, problem, proposed_inputs?): propose a missing skill/tool.",
            'Example: <tool_call>{"name": "discover_capabilities", "arguments": {"query": "memory cpu health"}}</tool_call>',
        ])

    def render_transient_context(
        self,
        session_id: str,
        *,
        suggestions: Iterable[DiscoveryMatch] = (),
        tool_registry: Optional[Any] = None,
    ) -> str:
        """Render ephemeral Layer 3 context (NEVER saved into canonical conversation history)."""
        sections: list[str] = []
        sug_list = list(suggestions)
        if sug_list:
            hints = ", ".join(
                f"{m.item_id} (~{m.estimated_tokens} tok)" for m in sug_list[:3]
            )
            sections.append(
                f"[Suggested capabilities (not active — call activate_capability if needed): {hints}]"
            )

        handles = self._activation.active_handles(session_id)
        if not handles:
            return "\n\n".join(sections)

        registry = tool_registry if tool_registry is not None else _optional_default_tool_registry()
        cap_names = ", ".join(h.capability_id for h in handles)
        lines = [
            f"ACTIVE CAPABILITY CONTEXT (TRANSIENT FOR CURRENT TASK ONLY: {cap_names})",
            "Active skills provide workflow guidance only; runtime policy and owner authorization control all execution.",
        ]

        seen_skills: set[str] = set()
        for handle in handles:
            for skill in handle.loaded_skills:
                if skill.skill_id in seen_skills:
                    continue
                seen_skills.add(skill.skill_id)
                lines.extend([
                    f"\n[SKILL {skill.skill_id}@{skill.version} — non-authoritative procedural reference]",
                    skill.instructions_markdown,
                ])

        seen_tools: dict[str, str] = {}
        for handle in handles:
            for tool_name in handle.bundle.tool_names:
                if tool_name in seen_tools or tool_name in EXCLUDED_LEGACY_TOOLS:
                    continue
                tool = registry.get(tool_name) if (registry is not None and hasattr(registry, "get")) else None
                if tool is not None and getattr(tool, "permission", "auto") != "disabled":
                    schema_fn = getattr(tool, "as_prompt_description", None)
                    seen_tools[tool_name] = (
                        schema_fn()
                        if callable(schema_fn)
                        else f"{tool_name}(): {getattr(tool, 'description', '')}"
                    )
                else:
                    desc = self._catalog.get_tool(tool_name)
                    if desc is not None and desc.permission_hint != "disabled":
                        seen_tools[tool_name] = (
                            desc.schema_text
                            or f"{desc.name}() [risk={desc.risk}]: {desc.summary}"
                        )

        if seen_tools:
            lines.append("\nACTIVE TOOLS (call with <tool_call> JSON):")
            for tool_name in sorted(seen_tools.keys()):
                lines.append(f"- {seen_tools[tool_name]}")

        sections.append("\n".join(lines))
        return "\n\n".join(sections)

    def render_task_records(self, session_id: str, *, limit: int = 5) -> str:
        """Render Layer 4 compact TaskRecord summaries kept after unloading."""
        records = self._activation.task_records(session_id, limit=limit)
        if not records:
            return ""
        lines = ["RECENT CAPABILITY TASK RECORDS (compact continuity):"]
        for rec in records:
            task_note = f" task={rec.task_id}" if rec.task_id else ""
            lines.append(f"- [{rec.capability_id}{task_note}] {rec.summary}")
        return "\n".join(lines)
