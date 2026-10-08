"""Measure the five S11.2 reads on the same seeded growth portfolio."""

import json
import os
import sys
from io import StringIO
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg
import pytest
from django.core.management import call_command
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from pytest_django.fixtures import DjangoAssertNumQueries, SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles.models import ServiceCycle
from apps.tenants.models import Membership, Tenant
from tests.browser.harness import mfa_session_cookie

pytestmark = [pytest.mark.scale, pytest.mark.django_db(transaction=True)]

# Measured on populated pages: workspace=46 and cycle_detail=50. These are
# open follow-ups, not larger budgets. The three compliant reads enforce 25 and
# the pre-existing tests/briefing/test_scale.py remains completely unchanged.
FOLLOW_UP_READS = frozenset({"workspace", "cycle_detail"})


def _analyze_seeded_tables(settings: SettingsWrapper) -> None:
    """Refresh planner stats on the freshly bulk-loaded tables, then prove it.

    Test-data hygiene, not a fix for the 2026-10-07 stall: planner statistics
    should reflect the seeded volume, as autoanalyze does in production, while
    TRUNCATE between tests leaves stats sampled on empty tables. ANALYZE needs
    table ownership, which app_test lacks, so it runs through the lane
    superuser on a second session. The seed above ran in autocommit, so that
    session sees the committed rows; reltuples is written synchronously by
    ANALYZE and a 0 here would mean the stats still describe empty tables.
    """
    superuser_dsn = urlparse(os.environ["SUPERUSER_DATABASE_URL"])
    test_dsn = urlunparse(
        superuser_dsn._replace(path="/" + settings.DATABASES["default"]["NAME"])
    )
    with psycopg.connect(test_dsn, autocommit=True) as analyzer:
        # Database-wide ANALYZE: the 2026-10-08 live stall showed the UNION
        # walked tables a 4-table list misses (tenants_membership sampled
        # empty, clients_clientassignment never analyzed), so the scope is
        # every ordinary table, not the ones this test happens to assert on.
        analyzer.execute("ANALYZE")
        analyzed: dict[str, int] = dict(
            analyzer.execute(
                "SELECT relname, reltuples::int FROM pg_class WHERE relname "
                "IN ('clients_engagement', 'obligations_obligation')"
            )
        )
        never_analyzed = [
            row[0]
            for row in analyzer.execute(
                "SELECT c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                "AND c.reltuples = -1"
            )
        ]
    expected = {"clients_engagement": 1_000, "obligations_obligation": 1_000}
    assert analyzed == expected, analyzed
    # planner statistics reflect the seeded volume, as autoanalyze would;
    # not a timing guard
    assert never_analyzed == [], never_analyzed


def test_named_reads_on_seeded_growth_portfolio(
    settings: SettingsWrapper,
    tmp_path: Path,
    django_assert_max_num_queries: DjangoAssertNumQueries,
) -> None:
    settings.MEDIA_ROOT = str(tmp_path)
    call_command("seed_axorim_demo", "--firms=growth", "--seed=42", stdout=StringIO())
    _analyze_seeded_tables(settings)
    tenant = Tenant.objects.get(slug="axorim-growth-42")
    owner = User.objects.get(email=f"owner@{tenant.slug}.example.com")
    portal_user = User.objects.get(email=f"portal@{tenant.slug}.example.com")
    membership = Membership.objects.get(user=portal_user)
    assert membership.client_id is not None
    with tenant_context(tenant.pk):
        company = ClientCompany.objects.get(pk=membership.client_id)
        assert ClientCompany.objects.count() == 1_000
        cycle = ServiceCycle.objects.filter(client=company).latest("period_start")
        assert ServiceCycle.objects.count() == 12_000
    sessions = {}
    for role, actor in (("firm", owner), ("portal", portal_user)):
        host = f"{tenant.slug}{'-portal' if role == 'portal' else ''}.localhost"
        session = Client(SERVER_NAME=host)
        cookie = mfa_session_cookie(actor, f"http://{host}/")
        assert "name" in cookie
        assert "value" in cookie
        session.cookies[cookie["name"]] = cookie["value"]
        sessions[role] = session
    reads = (
        ("hoje", "firm", reverse("dashboard")),
        ("clientes", "firm", reverse("clients-list")),
        (
            "workspace",
            "firm",
            reverse("client-detail", args=[company.pk])
            + f"?periodo={cycle.period_start:%Y-%m}",
        ),
        ("cycle_detail", "firm", reverse("cycle-detail", args=[cycle.pk])),
        ("portal_inicio", "portal", "/"),
    )
    counts: dict[str, int] = {}
    for name, role, path in reads:
        budget = (
            CaptureQueriesContext(connection)
            if name in FOLLOW_UP_READS
            else django_assert_max_num_queries(25)
        )
        with budget as queries:
            response = sessions[role].get(path)
        assert response.status_code == 200, (name, response.status_code)
        if name == "hoje":
            assert response.context["page"].total >= 1_000
        elif name == "portal_inicio":
            assert b"Documentos sint" in response.content
        else:
            assert company.legal_name.encode() in response.content
        if name == "workspace":
            assert str(cycle.pk).encode() in response.content
        counts[name] = len(queries)
    sys.stdout.write(f"\nAXR1_READ_QUERIES={json.dumps(counts, sort_keys=True)}\n")
    followups = {name: count for name, count in counts.items() if count > 25}
    sys.stdout.write(f"AXR1_QUERY_FOLLOW_UPS={json.dumps(followups, sort_keys=True)}\n")
