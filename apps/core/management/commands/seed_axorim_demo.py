"""Create reproducible local portfolios without touching a production database."""

import os
import secrets
from argparse import ArgumentParser
from typing import Final

from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.conf import settings
from django.core.files.storage import FileSystemStorage, default_storage
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.demo_cycles import seed_cycles
from apps.core.demo_data import (
    ScaleSpec,
    add_portfolio_variations,
    build_portfolio,
    demo_id,
)
from apps.core.demo_evidence import seed_evidence, write_document_files
from apps.core.management.commands.seed_demo import Credential, seed_owner
from apps.core.tenancy import tenant_context
from apps.cycles.models import ServiceCycle
from apps.tenants.models import Membership, Tenant, TenantRole

FIRM_SIZES: Final = {"small": 100, "growth": 1_000, "mixed": 1_000}


def _portal(tenant: Tenant, client: ClientCompany) -> list[Credential]:
    email = f"portal@{tenant.slug}.example.com"
    password = secrets.token_urlsafe(18)
    account = User.objects.create_user(email=email, password=password)
    secret = generate_totp_secret()
    TOTP.activate(account, secret)
    # PLATFORM_QUERY_OK: the local seed creates this firm's first client-side seat.
    Membership.objects.create(
        tenant=tenant, user=account, client=client, role=TenantRole.CLIENT_OWNER
    )
    return [
        Credential(email, "password", password),
        Credential(email, "totp-secret", secret),
    ]


def _firm(kind: str, seed: int) -> tuple[Tenant, list[Credential]]:
    slug = f"axorim-{kind}-{seed}"
    discriminator = tuple(FIRM_SIZES).index(kind)
    spec = ScaleSpec(FIRM_SIZES[kind], seed * len(FIRM_SIZES) + discriminator)
    identity = demo_id(spec.seed, "tenant")
    credentials: list[Credential] = []
    with transaction.atomic():
        # PLATFORM_QUERY_OK: host operator creates a new, explicitly synthetic firm.
        tenant, created = Tenant.objects.get_or_create(
            slug=slug,
            defaults={
                "id": identity,
                "name": f"Axorim sintético {kind} ({seed})",
                "features": ["cycles_r1", "briefing_r1"],
            },
        )
        if tenant.pk != identity:
            message = f"Refusing existing non-synthetic tenant {slug}."
            raise CommandError(message)
        if created:
            credentials = seed_owner(tenant, f"owner@{slug}.example.com")
            owner = User.objects.get(email=f"owner@{slug}.example.com")
            clients = build_portfolio(tenant, owner, spec)
            with tenant_context(tenant.pk):
                add_portfolio_variations(spec, mixed=kind == "mixed")
                requests = seed_cycles(tenant, owner, spec)
                seed_evidence(requests, owner, spec)
            credentials.extend(_portal(tenant, clients[0]))
    return tenant, credentials


class Command(BaseCommand):
    """Seed 100/1000/1000 clients and twelve months, converging on rerun."""

    help = "Seed local synthetic Axorim portfolios; never use production data."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept explicit firm selection and a deterministic data seed."""
        parser.add_argument("--firms", default="small,growth,mixed")
        parser.add_argument("--seed", type=int, default=42)

    def handle(self, *_args: str, **options: str | int) -> None:
        """Check safety before any query, then report actual stored counts."""
        production_name = os.environ.get("POSTGRES_DB") or "app_mei"
        if (
            not settings.DEBUG
            and str(settings.DATABASES["default"]["NAME"]) == production_name
        ):
            message = (
                "Refusing synthetic data on the production database with DEBUG=False."
            )
            raise CommandError(message)
        firms = [value.strip() for value in str(options["firms"]).split(",")]
        if len(set(firms)) != len(firms) or any(
            kind not in FIRM_SIZES for kind in firms
        ):
            message = "--firms must contain distinct names: small,growth,mixed."
            raise CommandError(message)
        seed = int(options["seed"])
        if seed < 0:
            message = "--seed must be nonnegative."
            raise CommandError(message)
        if not isinstance(default_storage, FileSystemStorage):
            message = "Synthetic documents require local FileSystemStorage."
            raise CommandError(message)
        for kind in firms:
            tenant, credentials = _firm(kind, seed)
            # Accounts are committed. Issue their one-time secrets before a local
            # file failure, so a repair run never needs to rotate a lost password.
            for credential in credentials:
                self.stdout.write(str(credential))
            self.stdout.flush()
            write_document_files(tenant)
            with tenant_context(tenant.pk):
                clients = ClientCompany.objects.count()
                cycles = ServiceCycle.objects.count()
            self.stdout.write(f"{tenant.slug}: clients={clients} cycles={cycles}")
