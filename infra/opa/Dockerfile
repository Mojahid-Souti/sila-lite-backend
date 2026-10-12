# OPA with the policies BAKED IN, not bind-mounted.
#
# The bind mount was the bug. Three times now (BUG-020, BUG-057, and again on
# 2026-09-03) the WSL mount for ./infra/opa/policies came up EMPTY after a Docker
# restart, OPA loaded zero policies, and every authz query returned undefined —
# which the API correctly fail-closes to 403, emptying the Connectors page and
# every other workspace list. A plain `restart` does not even fix it; the
# container has to be recreated so the mount is re-resolved.
#
# Copying the policies into the image removes the failure mode outright: there is
# no host mount left to corrupt, and the container cannot start without them.
#
# TRADE-OFF: editing a .rego now needs a rebuild —
#     docker compose up -d --build opa
# That is the cost of the outage not recurring. Policies change rarely; the stack
# restarts constantly.
FROM openpolicyagent/opa:0.70.0

COPY policies /policies
