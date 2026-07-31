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
-- app_portal    the client portal's role, entered with SET LOCAL ROLE inside the
--               request transaction. NOLOGIN, and holds SELECT on an ALLOW-LIST of six
--               tables only, so any table added later is closed to the portal by
--               default and a missing grant fails loudly instead of returning nothing.

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
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_portal') THEN
        CREATE ROLE app_portal;
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
-- NOLOGIN: reached only through SET LOCAL ROLE from an app_runtime connection, so it
-- needs no password and no GRANT CONNECT — SET ROLE does not re-check either.
ALTER ROLE app_portal WITH NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE
    NOINHERIT;

-- WITHOUT THESE TWO GRANTS THE ENTIRE ISOLATION SUITE DIES ON ITS FIRST STATEMENT.
-- PostgreSQL permits SET ROLE <r> only when the current role is a member of <r>.
-- app_test is neither role by default, so `SET ROLE app_runtime` would return
-- "permission denied to set role" and no cross-tenant assertion would ever execute.
GRANT app_runtime TO app_test;
GRANT app_migrator TO app_test;

-- INHERIT FALSE IS LOAD-BEARING. DO NOT DROP IT, AND DO NOT "SIMPLIFY" THIS TO A PLAIN
-- GRANT. PostgreSQL matches a policy's TO clause by PRIVILEGE INHERITANCE, not identity.
-- Under the default (INHERIT TRUE) app_runtime acquires app_portal's privileges, so the
-- RESTRICTIVE `TO app_portal` policies bind app_runtime as well and the accounting firm
-- reads ZERO ROWS from every client table. Reproduced on PostgreSQL 16.14.
--   pg_has_role('app_runtime','app_portal','USAGE') MUST be false  (inheritance off)
--   pg_has_role('app_runtime','app_portal','SET')   MUST be true   (SET LOCAL ROLE works)
-- 'MEMBER' is true under both settings and must never be used as the assertion.
-- A plain re-GRANT of an existing membership is a NO-OP: changing this later requires
-- an explicit WITH INHERIT TRUE/FALSE, or a REVOKE followed by a GRANT.
GRANT app_portal TO app_runtime WITH INHERIT FALSE, SET TRUE;

-- Applied at LOGIN, so production sees '' while a test session that reaches
-- app_runtime through SET ROLE sees NULL. Both fail closed through
-- NULLIF(current_setting('app.tenant_id', true), ''), and T-014 asserts both shapes.
ALTER ROLE app_runtime SET app.tenant_id TO '';

GRANT CONNECT ON DATABASE :"DBNAME" TO app_migrator, app_runtime, app_test;

GRANT USAGE, CREATE ON SCHEMA public TO app_migrator;
GRANT USAGE ON SCHEMA public TO app_runtime, app_test, app_portal;

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

-- app_portal's allow-list. Deliberately NOT `ON ALL TABLES`, and deliberately with no
-- ALTER DEFAULT PRIVILEGES: a blanket grant would also expose accounts_user (password
-- hashes), mfa_authenticator (TOTP secrets) and django_session, none of which the
-- portal coverage meta-test inspects. Naming the six tables closes every future table
-- by default.
--
-- to_regclass-guarded because this file runs from the init hook against an EMPTY data
-- directory under ON_ERROR_STOP=1: an unguarded GRANT on a table that does not exist
-- yet aborts cluster bootstrap. On a fresh cluster this therefore grants nothing, so
-- the same statements must be re-run after `migrate` (ops/README.md) — and are, for the
-- test database, by tests/conftest.py.
DO $$
DECLARE
    portal_table text;
BEGIN
    FOREACH portal_table IN ARRAY ARRAY[
        'clients_clientcompany',
        'clients_clientassignment',
        'clients_clienttag',
        'clients_onboardingitem',
        'obligations_obligation',
        'obligations_monthlyrevenue',
        'obligations_document'
    ]
    LOOP
        IF to_regclass('public.' || portal_table) IS NOT NULL THEN
            EXECUTE format('GRANT SELECT ON public.%I TO app_portal', portal_table);
        END IF;
    END LOOP;
END
$$;

-- app_portal's WRITE allow-list. A SEPARATE block from the read one above, and a
-- separate loop variable, because the two lists are different sets and always will be:
-- the portal reads its client's obligations and revenue, and writes only into the vault.
--
-- INSERT and nothing else. Not UPDATE, because `UPDATE documents SET status='deleted'`
-- is a delete in every sense this product cares about; not DELETE or TRUNCATE, because a
-- client removing fiscal evidence is a compliance problem, and erasure routes through the
-- existing DataSubjectRequest flow instead; not REFERENCES, because a role holding it can
-- create a foreign key to a table it cannot read and use the violations as an existence
-- oracle. If metadata editing is ever needed it must be a COLUMN-level grant.
--
-- No sequence privilege is granted anywhere: every primary key here is a UUIDv7, and a
-- granted sequence's last_value is a global cross-client row counter readable by a role
-- that can see zero rows.
DO $$
DECLARE
    portal_write_table text;
BEGIN
    FOREACH portal_write_table IN ARRAY ARRAY[
        'obligations_document'
    ]
    LOOP
        IF to_regclass('public.' || portal_write_table) IS NOT NULL THEN
            EXECUTE format(
                'GRANT INSERT ON public.%I TO app_portal', portal_write_table
            );
        END IF;
    END LOOP;
END
$$;
