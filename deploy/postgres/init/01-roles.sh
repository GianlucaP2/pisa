#!/bin/bash
# Runs once, on first start of an empty data volume (superuser). Creates the database, the pgvector
# extension and one role per job (least privilege):
#   pisa_migrate  owns the schema, runs DDL (pisa migrate only)
#   pisa_app      reads/writes data in `pisa`, no DDL; owns `pisa_dbos` (DBOS system tables) where it may run DDL
#   pisa_readonly SELECT only (for your own checks; made only when PISA_READONLY_PASSWORD is set)
# Each password comes from its variable (PISA_APP_PASSWORD) or from a file it names (PISA_APP_PASSWORD_FILE,
# for Docker secrets).
set -euo pipefail

password() {
  local name=$1 file_var="${1}_FILE"
  if [[ -n "${!file_var:-}" ]]; then
    tr -d '\n' < "${!file_var}"
  else
    printf '%s' "${!name:-}"
  fi
}
MIGRATE_PW=$(password PISA_MIGRATE_PASSWORD)
APP_PW=$(password PISA_APP_PASSWORD)
RO_PW=$(password PISA_READONLY_PASSWORD)
if [[ -z "$MIGRATE_PW" || -z "$APP_PW" ]]; then
  echo "PISA_MIGRATE_PASSWORD and PISA_APP_PASSWORD are required" >&2
  exit 1
fi

psql -v ON_ERROR_STOP=1 --username postgres -v migrate_pw="$MIGRATE_PW" -v app_pw="$APP_PW" <<'SQL'
CREATE ROLE pisa_migrate LOGIN PASSWORD :'migrate_pw';
CREATE ROLE pisa_app LOGIN PASSWORD :'app_pw';
CREATE ROLE pisa_readonly NOLOGIN;
CREATE DATABASE pisa OWNER pisa_migrate;
CREATE DATABASE pisa_dbos OWNER pisa_app;
SQL
if [[ -n "$RO_PW" ]]; then
  psql -v ON_ERROR_STOP=1 --username postgres -v ro_pw="$RO_PW" <<'SQL'
ALTER ROLE pisa_readonly LOGIN PASSWORD :'ro_pw';
SQL
fi

psql -v ON_ERROR_STOP=1 --username postgres --dbname pisa <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO pisa_app, pisa_readonly;
GRANT CREATE ON SCHEMA public TO pisa_migrate;
ALTER DEFAULT PRIVILEGES FOR ROLE pisa_migrate IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO pisa_app;
ALTER DEFAULT PRIVILEGES FOR ROLE pisa_migrate IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO pisa_app;
ALTER DEFAULT PRIVILEGES FOR ROLE pisa_migrate IN SCHEMA public
  GRANT SELECT ON TABLES TO pisa_readonly;
ALTER ROLE pisa_readonly SET default_transaction_read_only = on;
SQL
