"""Generate a fresh set of Sila Lite secrets — never reuse another install's.

    python scripts/gen_secrets.py > .env                     # local docker compose
    python scripts/gen_secrets.py --openshift > sila-secrets.env
    oc create secret generic sila-secrets --from-env-file=sila-secrets.env

Standard library only. The two Supabase API keys are HS256 JWTs signed with
the new JWT secret (role "anon" and "service_role"), as Supabase expects.
Keep the output out of git: .env and *.env are in .gitignore.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import time

TEN_YEARS_S = 10 * 365 * 24 * 3600


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def supabase_key(role: str, jwt_secret: str) -> str:
    """An HS256 JWT for a Supabase API role, valid for ten years."""
    now = int(time.time())
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    claims = {"role": role, "iss": "supabase", "iat": now, "exp": now + TEN_YEARS_S}
    payload = _b64url(json.dumps(claims).encode())
    signing_input = f"{header}.{payload}".encode()
    signature = hmac.new(jwt_secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url(signature)}"


def generate(public_url: str, allowed_domains: str) -> dict[str, str]:
    postgres_password = secrets.token_hex(24)  # hex: safe inside a URL
    jwt_secret = secrets.token_hex(32)
    values = {
        "PUBLIC_URL": public_url,
        "SILA_SIGNUP_ALLOWED_DOMAINS": allowed_domains,
        "JWT_EXPIRY": "3600",
        "POSTGRES_PASSWORD": postgres_password,
        "SUPABASE_JWT_SECRET": jwt_secret,
        "SUPABASE_ANON_KEY": supabase_key("anon", jwt_secret),
        "SUPABASE_SERVICE_ROLE_KEY": supabase_key("service_role", jwt_secret),
        "SILA_SECRET_KEY": secrets.token_urlsafe(48),
        # A Fernet key: 32 random bytes, url-safe base64 (padding kept).
        "SILA_FIELD_ENCRYPTION_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
    }
    return values


def openshift_view(values: dict[str, str]) -> dict[str, str]:
    """The Secret's keys: the generated values plus the connection strings built
    from them, so no manifest ever spells out a password."""
    password = values["POSTGRES_PASSWORD"]
    config_only = ("PUBLIC_URL", "SILA_SIGNUP_ALLOWED_DOMAINS")  # live in sila-config
    return {
        **{k: v for k, v in values.items() if k not in config_only},
        "DATABASE_URL": f"postgresql+asyncpg://postgres:{password}@supabase-db:5432/postgres",
        "GOTRUE_DB_DATABASE_URL": (
            f"postgres://supabase_auth_admin:{password}@supabase-db:5432/postgres"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    # Where the website is opened. Locally the frontend runs on :3002; on
    # OpenShift pass the real address (e.g. https://sila.omantel.om).
    parser.add_argument("--public-url", default="http://localhost:3002")
    parser.add_argument("--allowed-domains", default="omantel.om")
    parser.add_argument(
        "--openshift",
        action="store_true",
        help="print the Secret's keys (oc --from-env-file)",
    )
    args = parser.parse_args()
    values = generate(args.public_url, args.allowed_domains)
    if args.openshift:
        values = openshift_view(values)
    print("\n".join(f"{k}={v}" for k, v in values.items()))


if __name__ == "__main__":
    main()
