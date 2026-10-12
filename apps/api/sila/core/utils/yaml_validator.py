"""DifySLValidator — safe YAML parsing and schema validation for Dify DSL files.

Validation rules (in order):
1. Size: reject if raw bytes > 1 MiB
2. Parse: yaml.safe_load only — never yaml.load
3. Required fields: app.name (str, 2–100 chars), app.mode in allowed set
4. Forbidden nodes: __python__ keys anywhere in the DSL
5. Tool allow-list: all tool_configurations entries must be in workspace_tool_ids
   (Phase 2: empty allow-list → all tool refs rejected until Phase 5)
"""

from __future__ import annotations

import yaml

_MAX_BYTES = 1_048_576  # 1 MiB
_ALLOWED_MODES = frozenset({"chat", "completion", "agent-chat", "workflow", "advanced-chat"})
_FORBIDDEN_KEYS = frozenset({"__python__"})


def _has_forbidden_node(obj: object) -> bool:
    """Recursively check DSL dict for forbidden keys. O(n) where n = total nodes."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _FORBIDDEN_KEYS:
                return True
            if _has_forbidden_node(v):
                return True
    elif isinstance(obj, list):
        for item in obj:
            if _has_forbidden_node(item):
                return True
    return False


def validate_yaml(
    raw: bytes | str,
    workspace_tool_ids: frozenset[str] | None = None,
) -> tuple[dict, list[str]]:  # type: ignore[type-arg]
    """Parse and validate a Dify DSL YAML payload.

    Args:
        raw: raw bytes or str of the YAML file content.
        workspace_tool_ids: IDs of tools registered for this workspace.
            None means skip tool validation (used in tests).
            Empty frozenset means all tool refs are rejected.

    Returns:
        (dsl_dict, errors) where errors is an empty list on success.

    Time: O(s) where s = YAML byte size ≤ 1 MiB.
    Space: O(s) for the parsed dict.
    """
    errors: list[str] = []

    # 1. Size check
    raw_bytes = raw if isinstance(raw, bytes) else raw.encode()
    if len(raw_bytes) > _MAX_BYTES:
        return {}, [f"YAML file exceeds 1 MiB limit ({len(raw_bytes):,} bytes)"]

    # 2. Parse — safe_load only
    try:
        dsl = yaml.safe_load(raw_bytes)
    except yaml.YAMLError as exc:
        return {}, [f"YAML parse error: {exc}"]

    if not isinstance(dsl, dict):
        return {}, ["YAML root must be a mapping"]

    # 3. Required fields
    app = dsl.get("app", {})
    if not isinstance(app, dict):
        errors.append("'app' must be a mapping")
        app = {}

    name = app.get("name", "")
    if not isinstance(name, str) or not (2 <= len(name) <= 100):
        errors.append("app.name must be a string between 2 and 100 characters")

    mode = app.get("mode", "")
    if mode not in _ALLOWED_MODES:
        errors.append(f"app.mode '{mode}' is not allowed; must be one of {sorted(_ALLOWED_MODES)}")

    # 4. Forbidden nodes
    if _has_forbidden_node(dsl):
        errors.append("DSL contains forbidden node '__python__'")

    # 5. Tool allow-list (Phase 2: empty = no tools registered yet)
    if workspace_tool_ids is not None:
        tool_configs = dsl.get("tool_configurations", [])
        if isinstance(tool_configs, list):
            for cfg in tool_configs:
                if isinstance(cfg, dict):
                    tool_id = cfg.get("tool_id", "")
                    if tool_id not in workspace_tool_ids:
                        errors.append(f"Tool '{tool_id}' is not registered for this workspace")

    return dsl, errors
