from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    BUILDER = "builder"
    REVIEWER = "reviewer"
    HIRER = "hirer"
    VIEWER = "viewer"


# Ordered by privilege level (index = rank)
ROLE_RANK: dict[Role, int] = {
    Role.VIEWER: 0,
    Role.HIRER: 1,
    Role.REVIEWER: 2,
    Role.BUILDER: 3,
    Role.ADMIN: 4,
}


class Action(StrEnum):
    WORKSPACE_VIEW = "workspace.view"
    WORKSPACE_UPDATE = "workspace.update"
    ORGANIZATION_CREATE = "organization.create"
    WORKSPACE_DELETE = "workspace.delete"
    WORKSPACE_INVITE = "workspace.invite"
    MEMBERSHIP_UPDATE = "membership.update"
    API_KEY_MANAGE = "api_key.manage"
    AGENT_CREATE = "agent.create"
    AGENT_VIEW = "agent.view"
    AGENT_UPDATE = "agent.update"
    AGENT_PUBLISH = "agent.publish"
    AGENT_ARCHIVE = "agent.archive"
    AGENT_CLONE = "agent.clone"
    AGENT_TEST = "agent.test"
    AGENT_TRANSFER = "agent.transfer"
    AGENT_REVIEW = "agent.review"
    AGENT_HIRE = "agent.hire"
    # Bazaar / hire / review (Phase 3)
    HIRE_CREATE = "hire.create"
    HIRE_VIEW = "hire.view"
    REVIEW_CREATE = "review.create"
    REVIEW_VIEW = "review.view"
    BAZAAR_SEARCH = "bazaar.search"
    AGENT_REACT = "agent.react"
    AGENT_REPORT = "agent.report"
    # SILA concierge (acts on behalf of the user; every tool call audited)
    SILA_SESSION_START = "sila.session_start"
    SILA_SESSION_DELETE = "sila.session_delete"
    SILA_TOOL_CALL = "sila.tool_call"
    # Per-user memory writes/deletes (content length only in metadata, not text).
    SILA_MEMORY_ADD = "sila.memory_add"
    SILA_MEMORY_DELETE = "sila.memory_delete"
    SILA_MEMORY_CLEAR = "sila.memory_clear"
    # Analysis Engine dataset uploads (filename + size only, never content).
    SILA_DATASET_UPLOAD = "sila.dataset_upload"
    # Knowledge Base & RAG (Phase 4)
    KB_CREATE = "kb.create"
    KB_VIEW = "kb.view"
    KB_DELETE = "kb.delete"
    DOCUMENT_UPLOAD = "document.upload"
    DOCUMENT_VIEW = "document.view"
    DOCUMENT_DELETE = "document.delete"
    KB_SEARCH = "kb.search"
    # Tools, MCP & integrations (Phase 5)
    MCP_SERVER_REGISTER = "mcp_server.register"
    MCP_SERVER_DELETE = "mcp_server.delete"
    MCP_SERVER_DISCOVER = "mcp_server.discover"
    TOOL_VIEW = "tool.view"
    TOOL_GRANT = "tool.grant"
    TOOL_REVOKE = "tool.revoke"
    TOOL_INVOKE = "tool.invoke"
    # Security Gatekeeper & PDPL (Phase 6)
    AGENT_SUBMIT_REVIEW = "agent.submit_review"
    AGENT_VET_START = "agent.vet_start"
    AGENT_VET_COMPLETE = "agent.vet_complete"
    AGENT_APPROVE = "agent.approve"
    AGENT_REJECT = "agent.reject"
    # Developer SDK & API (Phase 7)
    PUBLIC_INVOKE = "public.invoke"
    PUBLIC_STREAM = "public.stream"
    WEBHOOK_CREATE = "webhook.create"
    WEBHOOK_DELETE = "webhook.delete"
    WEBHOOK_VIEW = "webhook.view"
    # Command Center & analytics (Phase 8)
    ANALYTICS_VIEW = "analytics.view"
    ANALYTICS_EXPORT = "analytics.export"
    ANOMALY_ACK = "anomaly.ack"

    # Phase 17 — onboarding & UX mode
    ONBOARDING_COMPLETE = "onboarding.complete"
    UX_MODE_CHANGE = "ux_mode.change"
    PROFILE_UPDATE = "profile.update"

    # WS-9 — connector authorization (Nango)
    CONNECTOR_VIEW = "connector.view"
    CONNECTOR_CONNECT = "connector.connect"
    CONNECTOR_REVOKE = "connector.revoke"
    # Each proxied third-party call (the interim replay-detection control until
    # capability tokens become mint-on-execute — see ADR).
    CONNECTOR_PROXY = "connector.proxy"
    # A read through the user's own binding (Sheets values, Outlook mail, Teams
    # messages) — path + row count only, never content.
    CONNECTOR_READ = "connector.read"
    # Platform SMTP "email me the summary" send (metadata only — no body/recipient).
    EMAIL_SEND = "email.send"
    # Phase 18 — open-source skills in the Bazaar
    SKILL_IMPORT = "skill.import"
    SKILL_APPROVE = "skill.approve"
    SKILL_REJECT = "skill.reject"
    SKILL_RESCAN = "skill.rescan"
    SKILL_PUBLISH = "skill.publish"
    SKILL_UNPUBLISH = "skill.unpublish"
    SKILL_REMOVE = "skill.remove"
    SKILL_ATTACH = "skill.attach"


class ResourceType(StrEnum):
    WORKSPACE = "workspace"
    SKILL = "skill"
    ORGANIZATION = "organization"
    MEMBERSHIP = "membership"
    API_KEY = "api_key"
    AGENT = "agent"
    AGENT_VERSION = "agent_version"
    HIRE = "hire"
    REVIEW = "review"
    CATEGORY = "category"
    # Phase 4
    KB = "knowledge_base"
    DOCUMENT = "document"
    # Phase 5
    MCP_SERVER = "mcp_server"
    TOOL = "tool"
    TOOL_INVOCATION = "tool_invocation"
    # Phase 6
    VETTING_RUN = "vetting_run"
    VETTING_FINDING = "vetting_finding"
    # Phase 7
    WEBHOOK_SUBSCRIPTION = "webhook_subscription"
    WEBHOOK_DELIVERY = "webhook_delivery"
    API_REQUEST = "api_request"
    # Phase 8
    ANOMALY = "anomaly"
    # WS-9
    CONNECTOR_BINDING = "connector_binding"
    METRIC = "metric"
    # Phase 17
    USER = "user"


class Outcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"


class AgentStatus(StrEnum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class AgentKind(StrEnum):
    """What executes the agent: a chat app (Dify) or a workflow webhook (n8n)."""

    CHAT = "chat"
    WORKFLOW = "workflow"


class Classification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"
    CONFIDENTIAL = "confidential"


class AgentSource(StrEnum):
    YAML_UPLOAD = "yaml_upload"
    TEMPLATE = "template"


class ReactionType(StrEnum):
    LIKE = "like"
    DISLIKE = "dislike"


class ReportStatus(StrEnum):
    OPEN = "open"
    REVIEWING = "reviewing"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


# Anti-harassment: a user may file at most this many reports on one agent per day.
MAX_REPORTS_PER_AGENT_PER_DAY = 5

# SILA concierge limits (security review): sessions per user per day, and the
MAX_SILA_TOOL_ITERATIONS = 4

# Largest batch one bulk session-delete request may carry. Bounds the work a
# single call can queue (Rule 6: background/bulk work is only O(n) if n is
# provably bounded) and keeps the id list well inside statement limits.
MAX_SILA_BULK_DELETE = 100


# ---------------------------------------------------------------------------
# Phase 17 — onboarding answers + UX mode
# ---------------------------------------------------------------------------
class UxMode(StrEnum):
    """Which shell a user sees. A UX preference, never a security boundary —
    RLS/OPA gate every action regardless of mode."""

    SIMPLE = "simple"  # the SILA concierge as the whole app
    ADVANCED = "advanced"  # the builder dashboard


class ReferralSource(StrEnum):
    """Where the user heard about the platform. Analytics only — does not route."""

    COLLEAGUE = "colleague"
    LINKEDIN = "linkedin"
    CONFERENCE = "conference"
    SEARCH = "search"
    OMANTEL = "omantel"
    OTHER = "other"


class JobRole(StrEnum):
    """The user's self-described role. Profile only — distinct from the RBAC
    workspace Role, and does not route."""

    EXECUTIVE = "executive"
    MANAGER = "manager"
    DEVELOPER = "developer"
    ANALYST = "analyst"
    OPERATIONS = "operations"
    OTHER = "other"


class AgentExperience(StrEnum):
    """Self-reported agent-building experience — the ONLY answer that routes."""

    NEVER = "never"
    SOME = "some"
    EXPERIENCED = "experienced"


def ux_mode_for_experience(experience: AgentExperience) -> UxMode:
    """Map the experience answer to the initial UX mode. Only the experienced
    pick the builder dashboard; everyone else starts in the guided concierge.
    Switchable later via the settings toggle (WS-7), so this is just the default."""
    return UxMode.ADVANCED if experience is AgentExperience.EXPERIENCED else UxMode.SIMPLE
