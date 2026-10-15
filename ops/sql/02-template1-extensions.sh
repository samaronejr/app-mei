# Create the extensions test databases need, inside the template they clone.

# PostgreSQL builds every new database by copying template1 — including the test
# databases pytest-django creates — so an extension existing only in app_mei never
# reaches them, and clients 0012's btree_gist assertion would fail on every fresh
# test run. The init hook runs this once, on an empty data directory, alongside
# roles.sql which creates the same extensions inside the app database itself.

# Deliberately NOT inside roles.sql: that file is also re-run by hand against a
# specific database URL, where a \connect template1 in the middle would detour the
# rest of the script onto the wrong database.
#
# shellcheck shell=sh
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname template1 <<'SQL'
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
SQL
