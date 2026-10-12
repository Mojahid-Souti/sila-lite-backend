"""HTTP client for n8n's internal REST API.

Complexity: each method is O(1) — a single HTTP call (login/invite/accept).
Owner bootstrap is amortized O(1) over the lifetime of the deployment.
"""

import contextlib
import re
import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import structlog

from sila.adapters.n8n.base import (
    N8nService,
    N8nSession,
    N8nUser,
    N8nWorkflowInfo,
    OwnerAlreadyExistsError,
)

log = structlog.get_logger(__name__)

AUTH_COOKIE_NAME = "n8n-auth"
DEFAULT_TIMEOUT_S = 10.0


class N8nRestAdapter(N8nService):
    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    # ------------------------------------------------------------------
    # Owner bootstrap (called once at API startup)
    # ------------------------------------------------------------------

    async def is_owner_setup(self) -> bool:
        """POST /rest/owner/setup with empty body returns 400 either way; check the message."""
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as client:
            try:
                r = await client.post(f"{self._base_url}/rest/owner/setup", json={})
            except httpx.RequestError:
                return False
            text = (r.text or "").lower()
            return "already" in text or "setup is already" in text

    async def setup_owner(
        self, email: str, password: str, first_name: str, last_name: str
    ) -> N8nUser:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as client:
            r = await client.post(
                f"{self._base_url}/rest/owner/setup",
                json={
                    "email": email,
                    "password": password,
                    "firstName": first_name,
                    "lastName": last_name,
                },
            )
            if r.status_code == 400 and "already" in (r.text or "").lower():
                raise OwnerAlreadyExistsError
            r.raise_for_status()
            body = r.json()
            data = body.get("data", body)
            return N8nUser(id=uuid.UUID(data["id"]), email=data["email"])

    # ------------------------------------------------------------------
    # Per-request: owner login + invite + accept + user login
    # ------------------------------------------------------------------

    async def login_as_owner(self, email: str, password: str) -> tuple[str, N8nUser]:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as client:
            r = await client.post(
                f"{self._base_url}/rest/login",
                json={"emailOrLdapLoginId": email, "password": password},
            )
            r.raise_for_status()
            cookie = r.cookies.get(AUTH_COOKIE_NAME)
            if not cookie:
                raise RuntimeError("n8n login returned no n8n-auth cookie")
            body = r.json()
            data = body.get("data", body)
            user = N8nUser(id=uuid.UUID(data["id"]), email=data["email"])
            return cookie, user

    async def invite_user(self, owner_cookie: str, email: str) -> tuple[uuid.UUID, str]:
        async with httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT_S, cookies={AUTH_COOKIE_NAME: owner_cookie}
        ) as client:
            r = await client.post(
                f"{self._base_url}/rest/invitations/",
                json=[{"email": email, "role": "global:member"}],
            )
            r.raise_for_status()
            body = r.json()
            users = body.get("data", body)
            if not users:
                raise RuntimeError(f"n8n invitation returned no users for {email}")
            entry = users[0].get("user", users[0])
            n8n_user_id = uuid.UUID(entry["id"])
            invite_url = entry.get("inviteAcceptUrl")
            if not invite_url:
                # Without an accept URL we still return the id; caller must
                # accept via the JWT bridge (out of scope here).
                raise RuntimeError(f"n8n did not return inviteAcceptUrl for {email}")
            parsed = urlparse(invite_url)
            token = parse_qs(parsed.query).get("token", [""])[0]
            return n8n_user_id, token

    async def accept_invitation(
        self,
        invite_token: str,
        first_name: str,
        last_name: str,
        password: str,
    ) -> None:
        # n8n's handler is acceptInvitationWithToken — it derives both
        # inviterId and inviteeId from the JWT in `token`. Pass that, NOT
        # the IDs.
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as client:
            r = await client.post(
                f"{self._base_url}/rest/invitations/accept",
                json={
                    "token": invite_token,
                    "firstName": first_name,
                    "lastName": last_name,
                    "password": password,
                },
            )
            r.raise_for_status()

    async def login_user(self, email: str, password: str) -> N8nSession:
        # n8n's /rest/login is rate-limited (~5/min) to defend against brute
        # force. React 19's StrictMode double-invokes effects in dev, so the
        # frontend often fires two /api/n8n-session POSTs in <100ms. Retry
        # once with a small delay if we hit the limit — this covers the
        # double-fire without weakening the protection.
        import asyncio

        for attempt in (1, 2):
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as client:
                r = await client.post(
                    f"{self._base_url}/rest/login",
                    json={"emailOrLdapLoginId": email, "password": password},
                )
            if r.status_code == 429 and attempt == 1:
                await asyncio.sleep(1.5)
                continue
            r.raise_for_status()
            cookie = r.cookies.get(AUTH_COOKIE_NAME)
            if not cookie:
                raise RuntimeError("n8n login returned no n8n-auth cookie")
            max_age_s = 8 * 60 * 60
            body = r.json()
            data = body.get("data", body)
            n8n_user_id = uuid.UUID(data["id"])
            return N8nSession(
                cookie_name=AUTH_COOKIE_NAME,
                cookie_value=cookie,
                max_age_s=max_age_s,
                n8n_user_id=n8n_user_id,
            )
        raise RuntimeError("n8n login rate limit not cleared after retry")

    def _session_client(self, cookie: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S, cookies={AUTH_COOKIE_NAME: cookie})

    async def list_workflows(self, cookie: str) -> list[N8nWorkflowInfo]:
        async with self._session_client(cookie) as client:
            r = await client.get(f"{self._base_url}/rest/workflows")
            r.raise_for_status()
            rows = r.json().get("data", [])
        return [
            N8nWorkflowInfo(
                id=str(w["id"]),
                name=w.get("name", ""),
                active=bool(w.get("active", False)),
                updated_at=w.get("updatedAt"),
            )
            for w in rows
            if w.get("id") is not None
        ]

    async def get_workflow(self, cookie: str, workflow_id: str) -> dict:  # type: ignore[type-arg]
        async with self._session_client(cookie) as client:
            r = await client.get(f"{self._base_url}/rest/workflows/{workflow_id}")
            r.raise_for_status()
            body = r.json()
            data = body.get("data", body)
            if not isinstance(data, dict) or "nodes" not in data:
                raise RuntimeError("n8n returned an unexpected workflow shape")
            return data

    async def create_workflow(self, cookie: str, definition: dict) -> str:  # type: ignore[type-arg]
        """POST /rest/workflows — create an inactive workflow; return its id."""
        async with self._session_client(cookie) as client:
            r = await client.post(f"{self._base_url}/rest/workflows", json=definition)
            r.raise_for_status()
            body = r.json()
            data = body.get("data", body)
            workflow_id = data.get("id") if isinstance(data, dict) else None
            if not workflow_id:
                raise RuntimeError("n8n create_workflow returned no id")
            return str(workflow_id)

    async def create_n8n_credential(
        self,
        cookie: str,
        *,
        name: str,
        type_: str,
        data: dict,  # type: ignore[type-arg]
    ) -> str:
        """POST /rest/credentials — create a credential in the user's own n8n account
        (n8n encrypts it at rest with N8N_ENCRYPTION_KEY); return its id. The secret
        lives only here — never in Sila's DB, transcript, or logs."""
        async with self._session_client(cookie) as client:
            r = await client.post(
                f"{self._base_url}/rest/credentials",
                json={"name": name, "type": type_, "data": data},
            )
            r.raise_for_status()
            body = r.json()
            out = body.get("data", body)
            cred_id = out.get("id") if isinstance(out, dict) else None
            if not cred_id:
                raise RuntimeError("n8n create_credential returned no id")
            return str(cred_id)

    # Trigger node types we can fire a manual "run now" from.
    _TRIGGER_TYPES = frozenset(
        {
            "n8n-nodes-base.scheduleTrigger",
            "n8n-nodes-base.webhook",
            "n8n-nodes-base.manualTrigger",
        }
    )

    async def run_workflow(self, cookie: str, workflow_id: str) -> str:
        """Fire a workflow once on demand — the editor's 'Execute Workflow'. Lets a
        SCHEDULED agent (no webhook to POST) be test-run before its cron fires.
        Returns the execution id. n8n 2.x wants the full workflowData + which trigger
        to start from."""
        wf = await self.get_workflow(cookie, workflow_id)
        trigger = next(
            (n.get("name") for n in wf.get("nodes", []) if n.get("type") in self._TRIGGER_TYPES),
            None,
        )
        if not trigger:
            raise RuntimeError("workflow has no trigger node to run from")
        async with self._session_client(cookie) as client:
            r = await client.post(
                f"{self._base_url}/rest/workflows/{workflow_id}/run",
                json={"workflowData": wf, "triggerToStartFrom": {"name": trigger}},
            )
            r.raise_for_status()
            body = r.json()
            data = body.get("data", body)
            return str(data.get("executionId", "")) if isinstance(data, dict) else ""

    async def deactivate_workflow(self, cookie: str, workflow_id: str) -> None:
        """POST /rest/workflows/{id}/deactivate — take a workflow off its schedule (n8n
        2.x rejects editing an active workflow, so a safe-fields edit deactivates first)."""
        async with self._session_client(cookie) as client:
            r = await client.post(f"{self._base_url}/rest/workflows/{workflow_id}/deactivate")
            r.raise_for_status()

    async def update_workflow(self, cookie: str, workflow_id: str, definition: dict) -> None:  # type: ignore[type-arg]
        """PATCH /rest/workflows/{id} — replace the workflow's nodes/connections (e.g. a
        new schedule cron / destination on a safe-fields edit). Caller deactivates first
        if the workflow is active (n8n 2.x rejects editing an active workflow)."""
        async with self._session_client(cookie) as client:
            r = await client.patch(
                f"{self._base_url}/rest/workflows/{workflow_id}",
                json={
                    "name": definition.get("name"),
                    "nodes": definition.get("nodes", []),
                    "connections": definition.get("connections", {}),
                    "settings": definition.get("settings", {}),
                },
            )
            r.raise_for_status()

    async def delete_workflow(self, cookie: str, workflow_id: str) -> None:
        """DELETE /rest/workflows/{id} — remove a workflow (e.g. test cleanup). n8n
        refuses to delete an ACTIVE workflow (400), so deactivate first (best-effort).
        404 is treated as already-gone."""
        async with self._session_client(cookie) as client:
            # n8n 2.x: a workflow must be inactive AND archived before it can be deleted.
            # Both are best-effort (already in that state / route absent → delete still applies).
            for step in ("deactivate", "archive"):
                with contextlib.suppress(httpx.HTTPError):
                    await client.post(f"{self._base_url}/rest/workflows/{workflow_id}/{step}")
            r = await client.delete(f"{self._base_url}/rest/workflows/{workflow_id}")
            if r.status_code != 404:
                r.raise_for_status()

    async def activate_workflow(self, cookie: str, workflow_id: str) -> None:
        # n8n 2.x uses versioned publishing: activation is a dedicated route
        # that takes the version to publish (PATCH {active: true} is silently
        # ignored). Mirrors the editor's "Publish" button.
        wf = await self.get_workflow(cookie, workflow_id)
        async with self._session_client(cookie) as client:
            r = await client.post(
                f"{self._base_url}/rest/workflows/{workflow_id}/activate",
                json={"versionId": wf.get("versionId")},
            )
            r.raise_for_status()


# Safety: redact tokens from log messages where this adapter is invoked.
_redact_re = re.compile(r"(token|password|cookie)[^&\"]*", re.IGNORECASE)


def _redact(s: str) -> str:
    return _redact_re.sub("[REDACTED]", s)
