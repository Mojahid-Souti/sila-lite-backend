-- Inject JWT secret and expiry into Postgres DB settings.
-- GoTrue and PostgREST read these via current_setting('app.settings.jwt_secret').
\set jwt_secret `echo "$JWT_SECRET"`
\set jwt_exp `echo "$JWT_EXP"`

ALTER DATABASE postgres SET "app.settings.jwt_secret" TO :'jwt_secret';
ALTER DATABASE postgres SET "app.settings.jwt_exp"    TO :'jwt_exp';
