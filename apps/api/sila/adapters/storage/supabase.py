"""Supabase Storage adapter via the REST API.

Swap point (Rule 5): replace with S3Adapter or MinIOAdapter without changing callers.
"""

import logging

import httpx

logger = logging.getLogger(__name__)

_BUCKET = "sila-documents"

# Process-wide latch: we only need to confirm the bucket exists once per
# worker, not on every upload. Avoids an extra round-trip on the hot path
# while still self-healing a fresh environment (no manual bucket creation).
_bucket_ensured = False


class SupabaseStorageAdapter:
    def __init__(self, storage_url: str, service_key: str) -> None:
        self._base = storage_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {service_key}",
            "apikey": service_key,
        }

    async def _ensure_bucket(self, client: httpx.AsyncClient) -> None:
        """Create the documents bucket if it doesn't exist yet (idempotent).

        Supabase Storage returns 400 ("Bucket not found") when you POST an
        object into a missing bucket, so without this a fresh deploy can't
        accept uploads until someone creates the bucket by hand. We create it
        once per process; a 409 (already exists) is success.
        """
        global _bucket_ensured
        if _bucket_ensured:
            return
        resp = await client.post(
            f"{self._base}/bucket",
            json={"id": _BUCKET, "name": _BUCKET, "public": False},
            headers=self._headers,
        )
        # Supabase signals "already exists" as HTTP 400 with a 409/Duplicate
        # body, so check the body too — that case is success, not a warning.
        already_exists = resp.status_code == 409 or (
            resp.status_code == 400 and "lready exists" in resp.text
        )
        if resp.status_code not in (200, 201) and not already_exists:
            # Real problem, but don't block the upload — let put() surface it.
            logger.warning("Bucket ensure returned %s: %s", resp.status_code, resp.text[:200])
        _bucket_ensured = True

    async def put(self, path: str, content: bytes, content_type: str) -> str:
        url = f"{self._base}/object/{_BUCKET}/{path}"
        async with httpx.AsyncClient(timeout=120.0) as client:
            await self._ensure_bucket(client)
            resp = await client.post(
                url,
                content=content,
                headers={**self._headers, "Content-Type": content_type},
            )
            if resp.status_code == 409:
                # Object already exists — upsert via PUT
                resp = await client.put(
                    url,
                    content=content,
                    headers={**self._headers, "Content-Type": content_type},
                )
            resp.raise_for_status()
        return path

    async def get(self, path: str) -> bytes:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.get(
                f"{self._base}/object/{_BUCKET}/{path}",
                headers=self._headers,
            )
            resp.raise_for_status()
            return resp.content

    async def delete(self, path: str) -> None:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.delete(
                f"{self._base}/object/{_BUCKET}/{path}",
                headers=self._headers,
            )
            if resp.status_code not in (200, 404):
                resp.raise_for_status()

    async def get_signed_url(self, path: str, expires_in: int = 3600) -> str:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self._base}/object/sign/{_BUCKET}/{path}",
                json={"expiresIn": expires_in},
                headers=self._headers,
            )
            resp.raise_for_status()
            return str(resp.json()["signedURL"])
