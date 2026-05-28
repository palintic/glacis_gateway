#!/bin/bash
set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Load .env if present
if [ -f "$ROOT/.env" ]; then
  export $(grep -v '^#' "$ROOT/.env" | xargs)
fi

echo "Waiting for PostgreSQL to be ready..."
until pg_isready -h localhost > /dev/null 2>&1; do
  sleep 1
done

echo "Creating role and database if they don't exist..."
psql -h localhost -U "$(whoami)" -d postgres <<-SQL
  DO \$\$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'glacis') THEN
      CREATE ROLE glacis WITH LOGIN PASSWORD 'glacispassword';
    END IF;
  END \$\$;

  SELECT 'CREATE DATABASE glacis_gateway OWNER glacis'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'glacis_gateway')\gexec
SQL

echo "Running migrations..."
PYTHONPATH="$ROOT" alembic upgrade head

echo "Done. Database is ready."
