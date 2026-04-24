-- Postgres init script — runs once on first container start.
-- Creates extensions and applies security hardening.

-- Enable UUID generation (used for primary keys)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Enable pg_stat_statements for query performance monitoring
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Revoke public schema access from public role (security hardening)
-- Only the app user can create objects
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON DATABASE reconciliation FROM PUBLIC;

-- Grant necessary permissions to the app user
GRANT ALL PRIVILEGES ON DATABASE reconciliation TO recon_user;
GRANT ALL ON SCHEMA public TO recon_user;

-- Set default search path
ALTER USER recon_user SET search_path TO public;
