-- Create the _realtime schema required by supabase/realtime.
-- Owner is set to postgres, which migrate.sh creates before running migrations/*.sql.
CREATE SCHEMA IF NOT EXISTS _realtime;
ALTER SCHEMA _realtime OWNER TO postgres;
