"""Core data models for the standalone Lila Capability Engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Literal, Optional, Protocol


class CapabilityKind(str, Enum):
    """Distinct entity types managed by the Capability Engine."""

    TOOL = "tool"
    SKILL = "skill"
    CAPABILITY = "capability"


class CapabilityState(str, Enum):
    """Four explicit lifecycle states separating availability, activation, and execution."""

    AVAILABLE = "available"
    ACTIVE = "active"
    EXECUTING = "executing"
    DEACTIVATED = "deactivated"


META_CONTROL_TOOLS = frozenset({
    "discover_capabilities",
    "activate_capability",
    "deactivate_capability",
    "request_capability_creation",
})

EXCLUDED_LEGACY_TOOLS = frozenset({
    "screen_capture",
})


def estimate_token_count(text: str) -> int:
    """Estimate compact BPE token footprint (~4 chars per token)."""
    clean = str(text or "")
    if not clean:
        return 0
    return max(1, (len(clean) + 3) // 4)


@dataclass(frozen=True)
class ToolDescriptor:
    """Non-authoritative metadata mirror of an executable Tool."""

    name: str
    capability_id: str
    category: str
    summary: str
    risk: str
    permission_hint: str
    estimated_schema_tokens: int
    trigger_keywords: tuple[str, ...] = ()
    schema_text: str = ""


@dataclass
class CapabilityActionResult:
    """Standalone result envelope compatible with Harness ToolResult."""

    success: bool
    output: str
    error: Optional[str] = None
    data: Optional[Any] = None


@dataclass(frozen=True)
class SkillSpec:
    """Versioned procedural knowledge unit (never directly executable)."""

    skill_id: str
    version: str
    title: str
    summary: str
    instructions_markdown: str
    content_digest: str
    tags: tuple[str, ...] = ()
    recommended_tools: tuple[str, ...] = ()
    resource_paths: tuple[str, ...] = ()
    estimated_tokens: int = 0
    origin: str = "builtin"


@dataclass(frozen=True)
class CapabilityBundle:
    """Discoverable, activatable bundle combining skills and tools."""

    capability_id: str
    version: str
    title: str
    summary: str
    skill_ids: tuple[str, ...] = ()
    tool_names: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    trigger_phrases: tuple[str, ...] = ()
    estimated_total_tokens: int = 0
    origin: str = "builtin"
    auto_activate_allowed: bool = False
    risk_tier: str = "moderate"


@dataclass(frozen=True)
class DiscoveryMatch:
    """Ranked search or assisted-discovery match returned to callers."""

    item_id: str
    kind: CapabilityKind
    title: str
    summary: str
    score: float
    source: Literal["model_query", "assisted_matcher"]
    estimated_tokens: int
    already_active: bool
    auto_activate_allowed: bool = False
    requires_approval_tools: tuple[str, ...] = ()


@dataclass
class ActiveCapabilityHandle:
    """Runtime handle for one currently active capability bundle in a session."""

    capability_id: str
    state: CapabilityState
    activated_turn: int
    last_used_turn: int
    ttl_turns: int
    token_cost: int
    bundle: CapabilityBundle
    task_id: Optional[str] = None
    activation_source: str = "model"
    executing_tools: set[str] = field(default_factory=set)
    loaded_skills: tuple[SkillSpec, ...] = ()
    loaded_tools: tuple[ToolDescriptor, ...] = ()


@dataclass(frozen=True)
class TaskRecord:
    """Compact summary retained after a capability deactivates or completes a task."""

    capability_id: str
    started_turn: int
    ended_turn: int
    summary: str
    task_id: Optional[str] = None
    tools_invoked: tuple[str, ...] = ()
    succeeded: bool = True


@dataclass(frozen=True)
class ContextReclamationPlan:
    """Explicit signal instructing Core/ASI to rebuild without deactivated skill tokens."""

    reclamation_required: bool
    restore_static_prefix: bool
    deactivated_ids: tuple[str, ...] = ()
    reclaimed_tokens_estimate: int = 0
    preserved_task_records: tuple[TaskRecord, ...] = ()


@dataclass(frozen=True)
class CapabilityCreationRequest:
    """Structured record for a requested new skill, tool, or capability bundle."""

    request_id: str
    kind: CapabilityKind
    title: str
    problem_statement: str
    proposed_inputs: str = ""
    suggested_tests: tuple[str, ...] = ()
    status: Literal["logged", "drafting", "quarantined", "verified", "promoted", "rejected"] = "logged"
    created_at_utc: str = ""


@dataclass(frozen=True)
class AuditEntry:
    """Immutable SHA-256 hash-chained capability audit ledger record."""

    seq: int
    timestamp_utc: str
    actor: str
    action: str
    target_id: str
    target_kind: str
    digest: str
    details: Dict[str, Any]
    prev_hash: str
    entry_hash: str


@dataclass(frozen=True)
class TurnCapabilityContext:
    """Turn-level snapshot combining active state, transient overlay, and reclamation plan."""

    session_id: str
    turn: int
    auto_activated: tuple[str, ...] = ()
    suggestions: tuple[DiscoveryMatch, ...] = ()
    active_capability_ids: tuple[str, ...] = ()
    active_tool_names: tuple[str, ...] = ()
    reclamation_plan: ContextReclamationPlan = field(
        default_factory=lambda: ContextReclamationPlan(False, False)
    )
    transient_context: str = ""
    task_records_context: str = ""


class CapabilityEngineError(ValueError):
    """Base error raised for invalid capability, skill, or lifecycle operations."""


class CapabilityNotFoundError(CapabilityEngineError):
    """Raised when a referenced capability or skill does not exist."""


class CapabilityValidationError(CapabilityEngineError):
    """Raised when a skill or capability fails structural or safety validation."""


class CapabilityBudgetError(CapabilityEngineError):
    """Raised when a bundle exceeds the total active capability token budget."""


class CapabilityEngineAPI(Protocol):
    """Minimal protocol implemented by the standalone CapabilityEngine facade."""

    def handle_meta_tool_call(
        self,
        session_id: str,
        name: str,
        args: Dict[str, Any],
        *,
        task_id: Optional[str] = None,
        actor: str = "lila",
    ) -> Any: ...
