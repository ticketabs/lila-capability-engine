"""Standalone Lila Capability Engine (Phases 1–3: Awareness, Creation & Evolution)."""

from .activation import ActivationManager
from .adapters import CoreCapabilityAdapter, HarnessCapabilityAdapter
from .audit import CapabilityAuditLedger
from .builtin_bundles import register_builtin_capabilities
from .context_bridge import ContextBridge
from .creation import (
    SkillAuthoringResult,
    SkillAuthoringService,
    SkillScenario,
    ToolDevelopmentPipeline,
    ToolDevelopmentResult,
)
from .discovery import DiscoveryEngine
from .engine import CapabilityEngine, build_default_capability_engine
from .evolution import (
    CapabilityEvolutionEngine,
    CapabilityGapReport,
    EvolutionComparison,
    SkillEvolutionResult,
)
from .models import (
    ActiveCapabilityHandle,
    AuditEntry,
    CapabilityActionResult,
    CapabilityBudgetError,
    CapabilityBundle,
    CapabilityCreationRequest,
    CapabilityEngineAPI,
    CapabilityEngineError,
    CapabilityKind,
    CapabilityNotFoundError,
    CapabilityState,
    CapabilityValidationError,
    ContextReclamationPlan,
    DiscoveryMatch,
    EXCLUDED_LEGACY_TOOLS,
    META_CONTROL_TOOLS,
    SkillSpec,
    TaskRecord,
    ToolDescriptor,
    TurnCapabilityContext,
)
from .providers import CapabilityProvider, ExternalProviderRegistry, ProviderRegistration
from .registry import CapabilityCatalogRegistry
from .skills import SkillRegistry, compute_skill_digest, validate_skill_content

__all__ = [
    "ActivationManager",
    "ActiveCapabilityHandle",
    "AuditEntry",
    "CapabilityActionResult",
    "CapabilityAuditLedger",
    "CapabilityBudgetError",
    "CapabilityBundle",
    "CapabilityCatalogRegistry",
    "CapabilityCreationRequest",
    "CapabilityEngine",
    "CapabilityEngineAPI",
    "CapabilityEngineError",
    "CapabilityEvolutionEngine",
    "CapabilityGapReport",
    "CapabilityKind",
    "CapabilityNotFoundError",
    "CapabilityProvider",
    "CapabilityState",
    "CapabilityValidationError",
    "ContextBridge",
    "ContextReclamationPlan",
    "CoreCapabilityAdapter",
    "DiscoveryEngine",
    "DiscoveryMatch",
    "EvolutionComparison",
    "EXCLUDED_LEGACY_TOOLS",
    "ExternalProviderRegistry",
    "HarnessCapabilityAdapter",
    "META_CONTROL_TOOLS",
    "ProviderRegistration",
    "SkillAuthoringResult",
    "SkillAuthoringService",
    "SkillEvolutionResult",
    "SkillRegistry",
    "SkillScenario",
    "SkillSpec",
    "TaskRecord",
    "ToolDescriptor",
    "ToolDevelopmentPipeline",
    "ToolDevelopmentResult",
    "TurnCapabilityContext",
    "build_default_capability_engine",
    "compute_skill_digest",
    "register_builtin_capabilities",
    "validate_skill_content",
]
