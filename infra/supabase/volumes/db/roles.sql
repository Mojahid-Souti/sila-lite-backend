-- Set passwords for Supabase service roles.
-- $POSTGRES_PASSWORD is injected by the supabase/postgres image at init time.
-- Without this file the roles are created without passwords and scram-sha-256
-- auth (required by pg_hba.conf) will reject every service connection.
\set pgpass `echo "$POSTGRES_PASSWORD"`

ALTER USER authenticator            WITH PASSWORD :'pgpass';
ALTER USER pgbouncer                WITH PASSWORD :'pgpass';
ALTER USER supabase_auth_admin      WITH PASSWORD :'pgpass';
ALTER USER supabase_functions_admin WITH PASSWORD :'pgpass';
ALTER USER supabase_storage_admin   WITH PASSWORD :'pgpass';
