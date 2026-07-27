-- Database role split. Idempotent: safe to re-run against an initialized cluster.
--
-- Mounted into the db service's /docker-entrypoint-initdb.d/ so a cold `up -d --wait`
-- needs no manual step. Passwords come from the environment; the literals below are
-- development fallbacks only.
--
-- app_migrator  owns the schema and runs migrations.  BYPASSRLS.
-- app_runtime   is what the application connects as.  NOSUPERUSER, NOBYPASSRLS, no
--               table ownership. Every policy in this design is inert unless all
--               three of those hold.
-- app_test      owns the pytest-created test database.  CREATEDB + BYPASSRLS, and a
--               member of both roles above so tests can SET ROLE app_runtime.

\set app_migrator_password 'app_migrator_password'
\set app_runtime_password 'app_runtime_password'
\set app_test_password 'app_test_password'
\getenv app_migrator_password APP_MIGRATOR_PASSWORD
\getenv app_runtime_password APP_RUNTIME_PASSWORD
\getenv app_test_password APP_TEST_PASSWORD

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_migrator') THEN
        CREATE ROLE app_migrator;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_runtime') THEN
        CREATE ROLE app_runtime;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_test') THEN
        CREATE ROLE app_test;
    END IF;
END
$$;

-- Stated as ALTER rather than as CREATE options so a re-run re-asserts every flag.
-- BYPASSRLS on app_migrator is load-bearing: under FORCE ROW LEVEL SECURITY a
-- RunPython backfill would otherwise see zero rows and report success having done
-- nothing, which is the most dangerous silent failure in this whole design.
ALTER ROLE app_migrator WITH LOGIN NOSUPERUSER BYPASSRLS NOCREATEDB NOCREATEROLE
    PASSWORD :'app_migrator_password';
ALTER ROLE app_runtime WITH LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE
    PASSWORD :'app_runtime_password';
-- CREATEDB because pytest-django must CREATE DATABASE; BYPASSRLS because it runs the
-- test-suite migrations, and a data migration seeing zero rows in tests but all rows
-- in production is a divergence in the worst possible direction.
ALTER ROLE app_test WITH LOGIN NOSUPERUSER BYPASSRLS CREATEDB NOCREATEROLE
    PASSWORD :'app_test_password';

-- WITHOUT THESE TWO GRANTS THE ENTIRE ISOLATION SUITE DIES ON ITS FIRST STATEMENT.
-- PostgreSQL permits SET ROLE <r> only when the current role is a member of <r>.
-- app_test is neither role by default, so `SET ROLE app_runtime` would return
-- "permission denied to set role" and no cross-tenant assertion would ever execute.
GRANT app_runtime TO app_test;
GRANT app_migrator TO app_test;

-- Applied at LOGIN, so production sees '' while a test session that reaches
-- app_runtime through SET ROLE sees NULL. Both fail closed through
-- NULLIF(current_setting('app.tenant_id', true), ''), and T-014 asserts both shapes.
ALTER ROLE app_runtime SET app.tenant_id TO '';

GRANT CONNECT ON DATABASE :"DBNAME" TO app_migrator, app_runtime, app_test;

GRANT USAGE, CREATE ON SCHEMA public TO app_migrator;
GRANT USAGE ON SCHEMA public TO app_runtime, app_test;

ALTER DEFAULT PRIVILEGES FOR ROLE app_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE app_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO app_runtime;

-- The test database's tables are owned by app_test, so app_migrator's default
-- privileges do not reach them. Test databases are also created from template1, which
-- carries none of this database's ACLs, which is why tests/conftest.py additionally
-- issues an explicit GRANT ON ALL TABLES after migrations.
ALTER DEFAULT PRIVILEGES FOR ROLE app_test IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE app_test IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO app_runtime;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_runtime;
