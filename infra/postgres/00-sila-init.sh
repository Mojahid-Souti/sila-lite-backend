#!/bin/sh
# First-start setup for a plain PostgreSQL (the pgvector image) so Sila Lite
# runs without Supabase's own Postgres image — which must start as root and so
# needs OpenShift's anyuid permission. This image runs as any user ID.
#
# Creates what GoTrue (sign-in) and the API expect: the API roles, the role
# GoTrue logs in as and the `auth` schema it owns, and the two extensions.
# Runs once, as the postgres superuser, when the data directory is empty.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v pw="$POSTGRES_PASSWORD" <<'SQL'
-- Roles the API's row-level security and GoTrue's grants refer to.
CREATE ROLE anon NOLOGIN NOINHERIT;
CREATE ROLE authenticated NOLOGIN NOINHERIT;
CREATE ROLE service_role NOLOGIN NOINHERIT BYPASSRLS;
CREATE ROLE dashboard_user NOLOGIN;
CREATE ROLE authenticator LOGIN NOINHERIT PASSWORD :'pw';
GRANT anon, authenticated, service_role TO authenticator;

-- GoTrue logs in as supabase_auth_admin and owns the auth schema; its own
-- migrations create the tables and auth.uid()/auth.role() there.
CREATE ROLE supabase_auth_admin LOGIN CREATEROLE NOINHERIT PASSWORD :'pw';
CREATE SCHEMA auth AUTHORIZATION supabase_auth_admin;
ALTER ROLE supabase_auth_admin SET search_path = auth;
GRANT USAGE ON SCHEMA auth TO anon, authenticated, service_role, postgres;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;
SQL
