"""TCode research facts and deterministic Attribution Shadow primitives.

Shadow decisions are durable and correctable but never change prompts or answers.
"""

from .config import ResearchSettings, load_research_settings
from .activities import (
    ResearchActivity,
    ResearchActivityApplyReport,
    ResearchActivityBuilder,
    ResearchActivityStore,
)
from .continuation import (
    ResearchContinuationContext,
    ResearchContinuationContextBuilder,
    ResearchContextAuditStore,
)
from .attribution import (
    AttributionRunReport,
    AttributionShadowDispatcher,
    ResearchAttributionEngine,
)
from .attribution_models import (
    AttributionCorrectionInput,
    ResearchEventLink,
    ResearchEventLinkInput,
)
from .journal import ResearchDeadLetterReplayer, ResearchEventSink
from .episode_models import (
    DerivationRunReport,
    ResearchCheckpoint,
    ResearchConflict,
    ResearchEpisode,
    ResearchFinding,
    ResearchResolution,
)
from .episode_store import ResearchDerivationStore
from .experiment_store import ResearchExperiment, ResearchExperimentStore, ResearchHypothesis
from .continuation_store import ResearchContinuation, ResearchContinuationStore
from .episodes import ResearchDerivationEngine, ResearchEvolutionManager
from .maintenance import ResearchMaintenanceReport, ResearchMaintenanceRunner
from .knowledge import ResearchKnowledgeManager
from .knowledge_models import ResearchKnowledgeLink, ResearchKnowledgeLintReport
from .knowledge_store import ResearchKnowledgeStore
from .pack import ResearchPackBuilder, ResearchPackRedactor, ResearchPackValidator
from .pack_import_store import ResearchPackImportStore
from .pack_importer import (
    ResearchPackArchiveInspector,
    ResearchPackImporter,
    ResearchPackRecoveryManager,
    ResearchPackRecoveryReport,
)
from .pack_models import (
    RESEARCH_PACK_ATTRIBUTION_MODES,
    RESEARCH_PACK_FORMAT,
    RESEARCH_PACK_GENERATOR_VERSION,
    RESEARCH_PACK_PROFILES,
    RESEARCH_PACK_SCHEMA_VERSION,
    RESEARCH_PACK_DUPLICATE_ACTIONS,
    RESEARCH_PACK_IMPORT_STATUSES,
    ResearchPackExport,
    ResearchPackImport,
    ResearchPackPreview,
)
from .pack_store import ResearchPackStore
from .pack_asset_models import (
    ResearchPackAsset,
    ResearchPackObjectMapping,
    ResearchPackObjectSnapshot,
    ResearchPackOverlay,
    ResearchPackOverlayObject,
    ResearchPackSourceScope,
)
from .pack_asset_store import ResearchPackAssetStore
from .pack_overlay import (
    ResearchPackOverlayProjectionReport,
    ResearchPackOverlayProjector,
)
from .pack_v2_models import (
    RESEARCH_PACK_V2_SCHEMA_VERSION,
    RESEARCH_PACK_V2_SCOPE_MODES,
    ResearchPackV2Activity,
    ResearchPackV2Manifest,
    ResearchPackV2Scope,
)
from .pack_v2 import (
    RESEARCH_PACK_V2_GENERATOR_VERSION,
    ResearchPackV2Builder,
)
from .experience_models import ResearchExperienceItem
from .experience import (
    ResearchExperienceProjectionReport,
    ResearchExperienceProjector,
)
from .experience_store import ResearchExperienceStore
from .models import ResearchEvent, ResearchEventInput
from .reconcile import ResearchJournalReconciler, ResearchReconcileReport
from .read_service import ResearchReadService
from .workspace_service import ResearchWorkspaceService
from .action_models import ResearchPendingAction
from .action_store import ResearchActionStore
from .action_service import (
    ResearchActionCommitService,
    interpret_research_action_decision,
)
from .chat_context import (
    RESEARCH_CHAT_CONTEXT_SCHEMA,
    RESEARCH_GROUNDING_SCHEMA,
    ResearchChatContextBuilder,
    ResearchPackIndexScheduler,
    normalize_research_chat_context,
    selected_asset_ids,
)
from .store import ResearchEventStore
from .routing import (
    ResearchRouteEnvelopeParser,
    ResearchRoutingContextBuilder,
    ResearchTurnRouter,
)
from .routing_models import (
    ResearchObjectScope,
    ResearchRouteExtraction,
    ResearchRouteProposal,
    ResearchRoutingContext,
    ResearchTurnRoute,
)
from .routing_store import ResearchRoutingStore
from .topic_models import (
    ResearchCandidateMembership,
    ResearchProject,
    ResearchTopicCandidate,
    TopicRunReport,
    research_candidate_digest,
)
from .topic_store import ResearchTopicStore
from .topics import ResearchTopicEngine, ResearchTopicManager
from .project_deletion import (
    ResearchProjectDeletionManager,
    ResearchProjectDeletionReport,
)
from .trace import ResearchTraceContext, trace_id_for_turn

__all__ = [
    "ResearchEvent",
    "ResearchEventInput",
    "ResearchActivity",
    "ResearchActivityApplyReport",
    "ResearchActivityBuilder",
    "ResearchActivityStore",
    "ResearchEventSink",
    "ResearchDeadLetterReplayer",
    "ResearchEpisode",
    "ResearchCheckpoint",
    "ResearchFinding",
    "ResearchConflict",
    "ResearchResolution",
    "DerivationRunReport",
    "ResearchDerivationStore",
    "ResearchHypothesis",
    "ResearchContinuation",
    "ResearchContinuationStore",
    "ResearchExperiment",
    "ResearchExperimentStore",
    "ResearchDerivationEngine",
    "ResearchEvolutionManager",
    "ResearchMaintenanceReport",
    "ResearchMaintenanceRunner",
    "ResearchKnowledgeLink",
    "ResearchKnowledgeLintReport",
    "ResearchKnowledgeStore",
    "ResearchPackBuilder",
    "ResearchPackRedactor",
    "ResearchPackValidator",
    "ResearchPackStore",
    "ResearchPackAsset",
    "ResearchPackSourceScope",
    "ResearchPackOverlay",
    "ResearchPackOverlayObject",
    "ResearchPackObjectMapping",
    "ResearchPackObjectSnapshot",
    "ResearchPackAssetStore",
    "ResearchPackOverlayProjectionReport",
    "ResearchPackOverlayProjector",
    "ResearchPackV2Scope",
    "ResearchPackV2Manifest",
    "ResearchPackV2Activity",
    "RESEARCH_PACK_V2_GENERATOR_VERSION",
    "ResearchPackV2Builder",
    "RESEARCH_PACK_V2_SCHEMA_VERSION",
    "RESEARCH_PACK_V2_SCOPE_MODES",
    "ResearchPackImportStore",
    "ResearchPackImporter",
    "ResearchPackArchiveInspector",
    "ResearchPackRecoveryManager",
    "ResearchPackRecoveryReport",
    "ResearchPackExport",
    "ResearchPackImport",
    "ResearchPackPreview",
    "RESEARCH_PACK_FORMAT",
    "RESEARCH_PACK_SCHEMA_VERSION",
    "RESEARCH_PACK_GENERATOR_VERSION",
    "RESEARCH_PACK_PROFILES",
    "RESEARCH_PACK_IMPORT_STATUSES",
    "RESEARCH_PACK_DUPLICATE_ACTIONS",
    "RESEARCH_PACK_ATTRIBUTION_MODES",
    "ResearchKnowledgeManager",
    "ResearchEventStore",
    "ResearchTurnRoute",
    "ResearchObjectScope",
    "ResearchRouteExtraction",
    "ResearchRouteProposal",
    "ResearchRoutingContext",
    "ResearchRouteEnvelopeParser",
    "ResearchRoutingContextBuilder",
    "ResearchTurnRouter",
    "ResearchRoutingStore",
    "ResearchExperienceItem",
    "ResearchExperienceProjectionReport",
    "ResearchExperienceProjector",
    "ResearchExperienceStore",
    "ResearchCandidateMembership",
    "ResearchProject",
    "ResearchTopicCandidate",
    "research_candidate_digest",
    "TopicRunReport",
    "ResearchTopicStore",
    "ResearchTopicEngine",
    "ResearchTopicManager",
    "ResearchProjectDeletionManager",
    "ResearchProjectDeletionReport",
    "ResearchJournalReconciler",
    "ResearchReconcileReport",
    "ResearchReadService",
    "ResearchWorkspaceService",
    "ResearchPendingAction",
    "ResearchActionStore",
    "ResearchActionCommitService",
    "interpret_research_action_decision",
    "RESEARCH_CHAT_CONTEXT_SCHEMA",
    "RESEARCH_GROUNDING_SCHEMA",
    "ResearchChatContextBuilder",
    "ResearchPackIndexScheduler",
    "normalize_research_chat_context",
    "selected_asset_ids",
    "ResearchSettings",
    "ResearchContinuationContext",
    "ResearchContinuationContextBuilder",
    "ResearchContextAuditStore",
    "ResearchEventLink",
    "ResearchEventLinkInput",
    "AttributionCorrectionInput",
    "AttributionRunReport",
    "AttributionShadowDispatcher",
    "ResearchAttributionEngine",
    "ResearchTraceContext",
    "load_research_settings",
    "trace_id_for_turn",
]
