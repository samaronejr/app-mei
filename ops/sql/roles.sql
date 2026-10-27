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
--               request transaction. NOLOGIN, and holds SELECT on an ALLOW-LIST of
--               tables only, so any table added later is closed to the portal by
--               default and a missing grant fails loudly instead of returning nothing.

\set app_migrator_password 'app_migrator_password'
\set app_runtime_password 'app_runtime_password'
\set app_test_password 'app_test_password'
\getenv app_migrator_password APP_MIGRATOR_PASSWORD
\getenv app_runtime_password APP_RUNTIME_PASSWORD
\getenv app_test_password APP_TEST_PASSWORD

-- Server-level prerequisites, owned by the bootstrap superuser on purpose.
-- CREATE EXTENSION needs database-level CREATE, which app_migrator deliberately
-- lacks, so the migration needing btree_gist (clients 0012, the TaxProfile
-- exclusion constraint) ASSERTS its presence and fails with a readable message
-- rather than installing it. On a database roles.sql did not initialise — the
-- pilot DB predating this file — the superuser re-runs just these three lines
-- (ops/README.md's runbook step). Test databases are cloned from template1, not
-- from this one, so they need the same three extensions created there; that is
-- 02-template1-extensions.sh in this directory and the bootstrap step in ci.yml.
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

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

-- PORTAL_NONE: authz_usercapabilitygrant
-- PORTAL_NONE: integrations_providerbudget
-- PORTAL_NONE: integrations_providerreservation
-- Firm-only reviewer authority: no portal grant. DenyPortalAccess supplies the
-- restrictive false policy as well. Mirrored by PORTAL_DENIED_TABLES in tests/conftest.py.

-- PORTAL_NONE: clients_clientcontact
-- Contacts remain ungranted; the existing client predicate is unchanged.
-- PORTAL_NONE: clients_importbatch
-- PORTAL_NONE: clients_importrow
-- PORTAL_NONE: cycles_playbook
-- PORTAL_NONE: cycles_playbookversion
-- PORTAL_NONE: cycles_playbookstep
-- Firm-internal playbook definitions (todo 8): no portal grant. Each table
-- carries a DenyPortalAccess restrictive policy from cycles.0001_initial, so a
-- grant added by mistake would still read nothing.
-- Firm-internal import history (todo 7): no portal grant. The batch has no client
-- and carries DenyPortalAccess; the row carries the client-anchored portal policy,
-- so a grant added by mistake would still be confined to one client.

-- Re-applying bootstrap must not undo the new append-only table's migration.
DO $$
BEGIN
    IF to_regclass('public.cycles_cycleevent') IS NOT NULL THEN
        REVOKE ALL ON cycles_cycleevent FROM app_runtime;
        GRANT SELECT, INSERT ON cycles_cycleevent TO app_runtime;
    END IF;
END
$$;

-- PORTAL_NONE: cycles_cycletask
-- PORTAL_NONE: cycles_cycleamendment
-- PORTAL_NONE: cycles_internalnote
-- PORTAL_NONE: cycles_exceptioninternal
-- PORTAL_NONE: cycles_requestinternal
-- PORTAL_NONE: cycles_extractedfield
-- PORTAL_NONE: cycles_review
-- PORTAL_NONE: cycles_actionproposal
-- PORTAL_NONE: cycles_approval
-- PORTAL_NONE: cycles_actionexecution
-- PORTAL_NONE: cycles_evidenceindex
-- PORTAL_NONE: cycles_evidenceexport
-- PORTAL_NONE: cycles_exportattempt
-- PORTAL_NONE: cycles_completiongeneration
-- PORTAL_NONE: cycles_completionjustification
-- PORTAL_NONE: cycles_legalhold
-- PORTAL_NONE: cycles_contentpurgeclaim
-- PORTAL_NONE: cycles_savedview
-- PORTAL_NONE: cycles_briefingfeedback
-- Personal briefing filters and dismissal reasons are firm-private (T19).
-- PORTAL_NONE: cycles_bulkoperation
-- PORTAL_NONE: cycles_bulkitem
-- Bulk snapshots, recipient lists and item results are firm-private.
-- Exact recipients, frozen payloads and approval authority are firm-private.
-- T9 exposes no internal work. T14 grants SELECT on cycles_exception (allow-list
-- below) under a policy that also requires client_visible; instructions and evidence
-- references live in cycles_exceptioninternal, and resolution prose stays
-- in cycles_internalnote. T11 keeps each request's firm-only
-- instructions and waiver reason in cycles_requestinternal, which the portal never
-- reads. cycles_cycleevent is INSERT-only for the portal (see the write list below):
-- the upload appends its submission event, and the portal reads no event back,
-- because the timeline also carries firm-side event types. T13 keeps extracted and
-- reviewed field values (cycles_extractedfield) and the firm's review decisions
-- (cycles_review, S8.2) firm-only; the client reads a decision and its pt-BR reason
-- from the columns the review service writes on cycles_evidencelink.

-- PORTAL_NONE: delivery_outboundmessage
-- PORTAL_NONE: delivery_deliveryattempt
-- PORTAL_NONE: delivery_deliveryeffect
-- PORTAL_NONE: delivery_automationpause
-- PORTAL_NONE: delivery_tenantnoticepolicy
-- PORTAL_NONE: automation_agentrun
-- PORTAL_NONE: automation_evalrun
-- T24: hidden AI observations and synthetic evaluation metrics have no portal grant.
-- Message contents, SMTP receipts and operational controls are firm-private.
-- PORTAL_NONE: fiscal_municipalityseedrow
-- PORTAL_NONE: authz_capabilityseedrow
-- AXRM-007: the regional capability seed's ledger (what authz.0009 created or
-- changed); platform migration bookkeeping, no tenant content, no portal grant.
-- AXRM-006: migration bookkeeping about the shared municipality registry (which
-- rows fiscal.0003 inserted); no tenant content and no portal grant.
-- PORTAL_NONE: integrations_providerconnection
-- PORTAL_NONE: integrations_clientproviderlink
-- PORTAL_NONE: integrations_providercall
-- PORTAL_NONE: integrations_providercallattempt
-- PORTAL_NONE: integrations_providereffectbarrier
-- PORTAL_NONE: integrations_providerexecutionepoch
-- PORTAL_NONE: integrations_firmprovidercall
-- PORTAL_NONE: integrations_firmprovidercallattempt
-- PORTAL_NONE: integrations_firmintegrationissue
-- PORTAL_NONE: integrations_firmproviderreservation
-- AXRM-007: provider connections and client provider links are firm-side provider
-- configuration (ADR-0008); no portal grant, and each table carries DenyPortalAccess
-- from integrations.0001, so a grant added by mistake would still read nothing.
-- PORTAL_NONE: integrations_authorityrecord
-- PORTAL_NONE: integrations_firmauthorization
-- PORTAL_NONE: integrations_integrationissue
-- AXRM-008: client authority records, the firm's termo record and owned provider
-- problems are firm-side (INV-R10); each carries DenyPortalAccess from
-- integrations.0002 (the two client-scoped tables also the client-anchored policy).
-- Re-applying bootstrap must not hand UPDATE/DELETE back on the two append-only ones.
DO $$
BEGIN
    IF to_regclass('public.integrations_authorityrecord') IS NOT NULL THEN
        REVOKE ALL ON integrations_authorityrecord FROM app_runtime;
        GRANT SELECT, INSERT ON integrations_authorityrecord TO app_runtime;
    END IF;
    IF to_regclass('public.integrations_firmauthorization') IS NOT NULL THEN
        REVOKE ALL ON integrations_firmauthorization FROM app_runtime;
        GRANT SELECT, INSERT ON integrations_firmauthorization TO app_runtime;
    END IF;
END
$$;
-- PORTAL_NONE: obligations_federalconsultation
-- PORTAL_NONE: obligations_federalpayment
-- PORTAL_NONE: obligations_fiscalsituationreport
-- AXRM-014: federal consultation receipts, the payment documents they returned and
-- fiscal-situation reports are firm-side (INV-R10); each carries the client-anchored
-- policy and DenyPortalAccess from obligations.0024. obligations_federalpayment is
-- append-only: the receipt loop below keeps it SELECT/INSERT on re-applied bootstrap.
-- PORTAL_NONE: integrations_paymentquery
-- AXRM-015: a PAGTOWEB consultation (who asked, which window, pages fetched, outcome)
-- is firm-side (INV-R10); integrations.0007 adds the client-anchored policy and
-- DenyPortalAccess, so a grant added by mistake would still read nothing.
-- PORTAL_NONE: tenants_billingaccount
-- PORTAL_NONE: tenants_usagerecord
-- PORTAL_NONE: tenants_notificationpreference
-- PORTAL_NONE: tenants_firmoutboundmessage
-- PORTAL_NONE: tenants_accountexport
-- PORTAL_NONE: tenants_accountexportattempt
-- PORTAL_NONE: tenants_supportgrant
-- AXR1-023: no DELETE for the runtime role. Revocation writes revoked_at, so
-- the row IS the audit trail; the blanket GRANT above is narrowed here just
-- like the append-only tables below.
DO $$
BEGIN
    IF to_regclass('public.tenants_supportgrant') IS NOT NULL THEN
        REVOKE ALL ON tenants_supportgrant FROM app_runtime;
        GRANT SELECT, INSERT, UPDATE ON tenants_supportgrant TO app_runtime;
    END IF;
END
$$;
-- Firm account data is private even when a client belongs to the firm.
DO $$
DECLARE
    receipt_table text;
BEGIN
    FOREACH receipt_table IN ARRAY ARRAY[
        'delivery_deliveryattempt', 'delivery_deliveryeffect', 'cycles_approval',
        'cycles_actionexecution', 'cycles_clientacknowledgement',
        'cycles_evidenceindex', 'cycles_completiongeneration', 'tenants_usagerecord',
        'integrations_providercallattempt', 'integrations_providereffectbarrier',
        'integrations_firmprovidercallattempt', 'obligations_federalpayment'
    ]
    LOOP
        IF to_regclass('public.' || receipt_table) IS NOT NULL THEN
            EXECUTE format('REVOKE ALL ON %I FROM app_runtime', receipt_table);
            EXECUTE format('GRANT SELECT, INSERT ON %I TO app_runtime', receipt_table);
        END IF;
    END LOOP;
END
$$;
-- AXRM-015: a payment recorded before its DAS obligation existed is bound once.
-- The REVOKE ALL above also drops column grants, so the one column UPDATE that
-- obligations.0026 grants is re-applied here; that migration's trigger admits only
-- matched_obligation_id going from NULL to an obligation.
DO $$
BEGIN
    IF to_regclass('public.obligations_federalpayment') IS NOT NULL THEN
        GRANT UPDATE (matched_obligation_id) ON obligations_federalpayment
            TO app_runtime;
    END IF;
END
$$;

-- The epoch has no tenant content and no portal grant. Runtime code may only read
-- it; the ownership-pinned restore procedure is its only increment path.
DO $$
BEGIN
    IF to_regclass('public.integrations_providerexecutionepoch') IS NOT NULL THEN
        REVOKE ALL ON integrations_providerexecutionepoch FROM app_runtime, app_portal;
        GRANT SELECT ON integrations_providerexecutionepoch TO app_runtime;
    END IF;
    IF to_regprocedure('public.integrations_current_provider_epoch()') IS NOT NULL THEN
        REVOKE ALL ON FUNCTION integrations_current_provider_epoch()
            FROM PUBLIC, app_portal;
        GRANT EXECUTE ON FUNCTION integrations_current_provider_epoch() TO app_runtime;
        REVOKE ALL ON FUNCTION integrations_restore_provider_epoch(bigint, uuid)
            FROM PUBLIC, app_portal;
        GRANT EXECUTE ON FUNCTION integrations_restore_provider_epoch(bigint, uuid)
            TO app_runtime;
    END IF;
END
$$;

-- The identifier-free readiness aggregate is runtime-only, including after an
-- ownership-pinned logical restore that deliberately omits dump ACLs.
DO $$
BEGIN
    IF to_regprocedure('public.delivery_outbox_health()') IS NOT NULL THEN
        REVOKE ALL ON FUNCTION public.delivery_outbox_health() FROM PUBLIC, app_portal;
        GRANT EXECUTE ON FUNCTION public.delivery_outbox_health() TO app_runtime;
    END IF;
END
$$;

-- app_portal's allow-list. Deliberately NOT `ON ALL TABLES`, and deliberately with no
-- ALTER DEFAULT PRIVILEGES: a blanket grant would also expose accounts_user (password
-- hashes), mfa_authenticator (TOTP secrets) and django_session, none of which the
-- portal coverage meta-test inspects. Naming the tables closes every future table
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
        'clients_engagement',
        'clients_taxprofile',
        'cycles_servicecycle',
        'cycles_clientpublication',
        -- S8.2 / T11: the client's own requests (client-visible fields only), the
        -- links it has submitted and their intake rows.
        -- AXRM-012: a request is READABLE only while its obligation (if any) is
        -- visible to app_portal, a link only while its document AND request are
        -- (USING folds, obligations 0023). WITH CHECK stays client-only, so the
        -- composite FKs still refuse a foreign parent.
        'cycles_evidencerequest',
        'cycles_evidencelink',
        -- AXRM-012: an intake row is READABLE only while its parent
        -- obligations_document row is visible to app_portal (EXISTS folded into the
        -- USING client predicate, obligations 0023); it has no visibility flag of its
        -- own. WITH CHECK stays client-only, so the composite FK still refuses an
        -- intake naming another client's document.
        'cycles_documentintake',
        -- S8.2 / T14: the portal predicate adds `client_visible`; only the fixed
        -- client_message is rendered. Internal instructions/evidence are in the
        -- portal-denied cycles_exceptioninternal companion, not this table.
        'cycles_exception',
        -- AXRM-012: the predicate adds `portal_visible` (folded into the client
        -- comparison, obligations 0023), so municipal and other firm-only rows stay
        -- hidden. SELECT only, as before; no new grant.
        'obligations_obligation',
        'obligations_monthlyrevenue',
        -- AXRM-012: the same `portal_visible` predicate hides staff-private and
        -- provider originals even after a clean scan, and a document bound to an
        -- obligation is READABLE only while that obligation is visible to app_portal
        -- (USING fold, obligations 0023). portal_visible,
        -- acquisition_origin and acquisition_receipt_ref are readable only on visible
        -- rows. The INSERT grant below is unchanged; trigger
        -- document_portal_client_upload (obligations 0023) pins a portal INSERT to a
        -- human upload_portal row with portal_visible=true and no receipt.
        'obligations_document',
        -- S8.2: the payments page labels paid rows from the newest payment
        -- observation. SELECT only; the append-only trigger and the missing
        -- INSERT grant together keep the portal from writing or rewriting any.
        -- AXRM-012: READABLE only while every member of its supersedes chain has a
        -- subject and an evidence link (if any) visible to app_portal (USING fold,
        -- obligations 0023). The chain is walked by the SECURITY DEFINER function
        -- obligations_observation_chain(uuid); 0023 creates it after this script,
        -- so 0023 itself revokes PUBLIC and grants EXECUTE to app_portal only.
        'obligations_sourceobservation'
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
        'obligations_document',
        -- T11: an upload against a request inserts its link, the intake row of a
        -- new document and one submission event. Each carries the RESTRICTIVE
        -- client-anchored WITH CHECK from its migration. cycles_cycleevent is the
        -- one INSERT without SELECT: the portal appends and never reads events.
        'cycles_evidencelink',
        'cycles_documentintake',
        'cycles_cycleevent',
        'cycles_clientacknowledgement'
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

-- T60: exact-version receipt matching only. No table-wide SELECT, receipt timestamps
-- or receipt row IDs; existing tenant/client RLS also governs these column reads.
DO $$
BEGIN
    IF to_regclass('public.cycles_clientacknowledgement') IS NOT NULL THEN
        GRANT SELECT (tenant_id, publication_id, publication_digest, user_id)
        ON cycles_clientacknowledgement TO app_portal;
    END IF;
END
$$;
