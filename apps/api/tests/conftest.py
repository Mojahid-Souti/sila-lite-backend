"""
Pytest configuration — set required env vars before any module imports so that
Pydantic Settings can initialise without a real .env file in CI / unit tests.
"""

import os

# Must be set before sila.* modules are imported (Settings() runs at module level).
os.environ.setdefault("SILA_SECRET_KEY", "test-secret-key-for-unit-tests-only")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:test@localhost:5432/postgres",
)
os.environ.setdefault("SILA_SUPABASE_URL", "http://localhost:8000")
os.environ.setdefault("SILA_SUPABASE_SERVICE_KEY", "test-service-key")
os.environ.setdefault(
    "SUPABASE_JWT_SECRET",
    "test-jwt-secret-at-least-32-characters-long",  # gitleaks:allow
)
os.environ.setdefault("OPA_URL", "http://localhost:8181")
