package sila.authz

# Role hierarchy: higher rank = more permissions.
# n = number of roles (5); rank lookup is O(1) map access.
role_rank := {
  "viewer":   0,
  "hirer":    1,
  "reviewer": 2,
  "builder":  3,
  "admin":    4,
}

# Minimum role required for each action.
# Source of truth for all Phase 1+2 authorization decisions.
min_role := {
  "workspace.view":    "viewer",
  "workspace.update":  "admin",
  "workspace.invite":  "admin",
  "membership.view":   "viewer",
  "membership.update": "admin",
  "membership.remove": "admin",
  "api_key.view":      "admin",
  "api_key.manage":    "admin",
  # Agent lifecycle (Phase 2)
  "agent.create":      "builder",
  "agent.view":        "viewer",
  "agent.update":      "builder",
  "agent.publish":     "builder",
  "agent.archive":     "builder",
  "agent.clone":       "builder",
  "agent.test":        "builder",
  # Team sharing: any member (Hirer+) may turn an APPROVED shared agent on/off;
  # it runs on its OWNER's identity (bindings + n8n session), actor audited.
  "agent.activate":    "hirer",
  "agent.transfer":    "admin",
  # Bazaar / review / hire (Phase 3)
  "agent.review":      "reviewer",
  "agent.hire":        "hirer",
  "bazaar.search":     "viewer",
  "hire.create":       "hirer",
  "hire.view":         "viewer",
  "review.create":     "hirer",
  "review.view":       "viewer",
  # Knowledge Base & RAG (Phase 4)
  "kb.create":         "builder",
  "kb.view":           "viewer",
  "kb.delete":         "builder",
  "kb.search":         "viewer",
  "document.upload":   "builder",
  "document.view":     "viewer",
  "document.delete":   "builder",
  # Tools, MCP & integrations (Phase 5)
  "mcp_server.register": "admin",
  "mcp_server.delete":   "admin",
  "mcp_server.discover": "admin",
  "tool.view":           "viewer",
  "tool.grant":          "builder",
  "tool.revoke":         "builder",
  "tool.invoke":         "builder",
  # Security Gatekeeper & PDPL (Phase 6)
  "agent.submit_review": "builder",
  "agent.approve":       "reviewer",
  "agent.reject":        "reviewer",
  # Developer SDK & API (Phase 7)
  "webhook.create":      "admin",
  "webhook.delete":      "admin",
  "webhook.view":        "viewer",
  "public.invoke":       "viewer",
  "public.stream":       "viewer",
  # Command Center & analytics (Phase 8)
  "analytics.view":      "viewer",
  "analytics.export":    "builder",
  "anomaly.ack":         "admin",
  # Connector authorization — Nango (WS-9)
  "connector.view":      "viewer",
  "connector.connect":   "builder",
  "connector.revoke":    "builder",
  # Open-source skills (Phase 18): an Admin imports, a Reviewer+ decides,
  # any member attaches a published skill to a chat.
  "skill.import":        "admin",
  "skill.approve":       "reviewer",
  "skill.reject":        "reviewer",
  "skill.rescan":        "reviewer",
  "skill.publish":       "reviewer",
  "skill.unpublish":     "reviewer",
  "skill.remove":        "admin",
  "skill.attach":        "viewer",
}

# Actions where rank-based gating is wrong because the roles are intentionally
# *parallel* (separation of duties), not hierarchical. A BUILDER has rank > REVIEWER
# but must NOT be allowed to approve their own agent's vetting run.
# These actions are gated on explicit role membership instead.
explicit_role_set := {
  "agent.approve": {"reviewer", "admin"},
  "agent.reject":  {"reviewer", "admin"},
}

# default deny
default allow := false

# allow when the caller's role rank >= the required role rank for the action,
# AND the action is not in the explicit-role list (those use the rule below).
allow {
  required := min_role[input.action]
  not explicit_role_set[input.action]
  role_rank[input.role] >= role_rank[required]
}

# allow when the caller's role is explicitly in the allowed set for the action
allow {
  allowed := explicit_role_set[input.action]
  input.role == allowed[_]
}
