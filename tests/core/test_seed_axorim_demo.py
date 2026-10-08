"""Synthetic portfolios converge; production and malformed selectors write nothing."""

import re
from datetime import date
from hashlib import sha256
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import CommandError, call_command
from django.db import connection
from pytest_django.fixtures import DjangoAssertNumQueries, SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientCompany, TaxProfile
from apps.core.tenancy import tenant_context
from apps.cycles.models import DocumentIntake, EvidenceRequest, ServiceCycle
from apps.delivery.models import OutboundMessage
from apps.fiscal.validators import validate_cnpj
from apps.obligations.models import Document
from apps.tenants.models import Tenant

pytestmark = pytest.mark.django_db(transaction=True)


def test_production_database_is_refused_before_writes(
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    django_assert_num_queries: DjangoAssertNumQueries,
) -> None:
    settings.DEBUG = False
    monkeypatch.setenv("POSTGRES_DB", str(connection.settings_dict["NAME"]))
    with django_assert_num_queries(0), pytest.raises(CommandError, match="production"):
        call_command("seed_axorim_demo", "--firms=small", "--seed=42")
    assert Tenant.objects.count() == 0
    assert User.objects.count() == 0


def test_empty_production_name_uses_the_compose_default(
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    django_assert_num_queries: DjangoAssertNumQueries,
) -> None:
    settings.DEBUG = False
    with monkeypatch.context() as local:
        local.setenv("POSTGRES_DB", "")
        local.setitem(settings.DATABASES["default"], "NAME", "app_mei")
        with (
            django_assert_num_queries(0),
            pytest.raises(CommandError, match="production"),
        ):
            call_command("seed_axorim_demo", "--firms=small", "--seed=42")
    assert Tenant.objects.count() == 0


def test_all_three_firms_have_real_messy_history(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    settings.DEBUG = True
    settings.MEDIA_ROOT = str(tmp_path)
    call_command("seed_axorim_demo", "--firms=small,growth,mixed", "--seed=42")
    for kind, size in (("small", 100), ("growth", 1_000), ("mixed", 1_000)):
        tenant = Tenant.objects.get(slug=f"axorim-{kind}-42")
        with tenant_context(tenant.pk):
            clients = list(ClientCompany.objects.all())
            assert len(clients) == size
            assert all(len(client.legal_name) > 100 for client in clients)
            for client in clients:
                validate_cnpj(client.cnpj)
                assert any(char.isalpha() for char in client.cnpj)
            assert (
                ClientCompany.objects.filter(
                    certificate_expires_on__lt=date(2026, 9, 20)
                ).count()
                == size // 4
            )
            assert ServiceCycle.objects.count() == size * 12
            assert ServiceCycle.objects.values("period_start").distinct().count() == 12
            assert EvidenceRequest.objects.filter(links__isnull=True).count() > size
            assert (
                DocumentIntake.objects.filter(
                    state="divergente", reason="cnpj_divergente"
                ).count()
                == size // 4
            )
            assert (
                OutboundMessage.objects.filter(
                    state="recusada_pelo_provedor", attempts=1
                ).count()
                == size // 10
            )
            regimes = set(TaxProfile.objects.values_list("regime", flat=True))
            assert regimes == (
                {"mei", "simples", "presumido", "real"} if kind == "mixed" else {"mei"}
            )
            document = Document.objects.first()
            assert document is not None
            payload = (tmp_path / document.storage_key).read_bytes()
            assert sha256(payload).hexdigest() == document.sha256
            assert b"12ABC34501DE35" in payload


def test_rerun_preserves_data_and_credentials_and_repairs_missing_files(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    settings.DEBUG = True
    settings.MEDIA_ROOT = str(tmp_path)
    first = StringIO()
    call_command("seed_axorim_demo", "--firms=small", "--seed=7", stdout=first)
    tenant = Tenant.objects.get(slug="axorim-small-7")
    with tenant_context(tenant.pk):
        before = list(ClientCompany.objects.order_by("id").values())
        cycle_ids = set(ServiceCycle.objects.values_list("pk", flat=True))
        document = Document.objects.first()
        assert document is not None
        path = tmp_path / document.storage_key
        original = path.read_bytes()
        path.unlink()
    password_hashes = list(
        User.objects.order_by("id").values_list("password", flat=True)
    )
    second = StringIO()
    call_command("seed_axorim_demo", "--firms=small", "--seed=7", stdout=second)
    with tenant_context(tenant.pk):
        assert list(ClientCompany.objects.order_by("id").values()) == before
        assert set(ServiceCycle.objects.values_list("pk", flat=True)) == cycle_ids
    assert path.read_bytes() == original
    assert list(User.objects.order_by("id").values_list("password", flat=True)) == (
        password_hashes
    )
    assert "password=" in first.getvalue()
    assert "password=" not in second.getvalue()


def test_a_different_seed_produces_a_different_portfolio(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    settings.DEBUG = True
    settings.MEDIA_ROOT = str(tmp_path)
    portfolios: list[set[str]] = []
    for seed in (7, 8):
        call_command("seed_axorim_demo", "--firms=small", f"--seed={seed}")
        tenant = Tenant.objects.get(slug=f"axorim-small-{seed}")
        with tenant_context(tenant.pk):
            portfolios.append(set(ClientCompany.objects.values_list("cnpj", flat=True)))
    assert portfolios[0].isdisjoint(portfolios[1])


@pytest.mark.parametrize("firms", ["", "unknown", "small,small", "small,unknown"])
def test_invalid_firms_are_refused_before_writes(
    firms: str,
    settings: SettingsWrapper,
    django_assert_num_queries: DjangoAssertNumQueries,
) -> None:
    settings.DEBUG = True
    with django_assert_num_queries(0), pytest.raises(CommandError, match="--firms"):
        call_command("seed_axorim_demo", f"--firms={firms}", stdout=StringIO())
    assert Tenant.objects.count() == 0


@pytest.mark.parametrize("debug", [False, True])
def test_guard_allows_debug_or_a_distinct_local_database(
    debug: bool,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings.DEBUG = debug
    settings.MEDIA_ROOT = str(tmp_path)
    monkeypatch.setenv(
        "POSTGRES_DB",
        str(connection.settings_dict["NAME"]) if debug else "production-only",
    )
    call_command("seed_axorim_demo", "--firms=small", "--seed=3")
    tenant = Tenant.objects.get(slug="axorim-small-3")
    with tenant_context(tenant.pk):
        assert ClientCompany.objects.count() == 100


def test_seed_refuses_an_existing_unrelated_firm(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    settings.DEBUG = True
    settings.MEDIA_ROOT = str(tmp_path)
    original = Tenant.objects.create(slug="axorim-small-42", name="Existing firm")
    with pytest.raises(CommandError, match="non-synthetic"):
        call_command("seed_axorim_demo", "--firms=small", "--seed=42")
    assert Tenant.objects.get(pk=original.pk).name == "Existing firm"
    assert User.objects.count() == 0


def test_file_failure_does_not_lose_committed_one_time_credentials(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    settings.DEBUG = True
    occupied = tmp_path / "occupied"
    occupied.write_text("This file cannot be a storage directory.")
    settings.MEDIA_ROOT = str(occupied)
    output = StringIO()
    with pytest.raises(NotADirectoryError):
        call_command("seed_axorim_demo", "--firms=small", "--seed=9", stdout=output)
    credentials = re.search(
        r"owner@axorim-small-9\.example\.com\s+password=(\S+)", output.getvalue()
    )
    assert credentials is not None, "Committed account credentials were never issued."
    owner = User.objects.get(email="owner@axorim-small-9.example.com")
    assert owner.check_password(credentials.group(1))
    settings.MEDIA_ROOT = str(tmp_path / "recovered")
    rerun = StringIO()
    call_command("seed_axorim_demo", "--firms=small", "--seed=9", stdout=rerun)
    assert "password=" not in rerun.getvalue()
    owner.refresh_from_db()
    assert owner.check_password(credentials.group(1))
    tenant = Tenant.objects.get(slug="axorim-small-9")
    with tenant_context(tenant.pk):
        document = Document.objects.first()
        assert document is not None
        assert (Path(settings.MEDIA_ROOT) / document.storage_key).is_file()
