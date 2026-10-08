"""Local read identities keep the real 120/minute rate-limit policy in the run."""

import time
from dataclasses import dataclass
from typing import Final

from allauth.account.internal.flows.login import AUTHENTICATION_METHODS_SESSION_KEY
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.conf import settings
from django.contrib.sessions.models import Session
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles.models import ServiceCycle
from apps.tenants.models import Membership, Tenant, TenantRole

ACTORS_PER_ROLE: Final = 10
GROWTH_CLIENTS: Final = 1_000


@dataclass(frozen=True, slots=True)
class ReadTarget:
    """A named read with a host, path and nonempty business-content witness."""

    name: str
    host: str
    path: str
    cookies: tuple[str, ...]
    witness: bytes


def session_cookie(user: User) -> str:
    """Use the existing browser harness's allauth MFA session shape, outside timing."""
    authenticator = Authenticator.objects.get(user=user, type=Authenticator.Type.TOTP)
    session_client = Client()
    session_client.force_login(user)
    session = session_client.session
    now = time.time()
    session[AUTHENTICATION_METHODS_SESSION_KEY] = [
        {"method": "password", "at": now, "email": user.email},
        {"method": "mfa", "at": now, "id": authenticator.pk, "type": "totp"},
    ]
    session.save()
    return f"{settings.SESSION_COOKIE_NAME}={session.session_key}"


def _actors(tenant: Tenant, company: ClientCompany | None) -> tuple[str, ...]:
    role = TenantRole.CLIENT_OWNER if company else TenantRole.OWNER
    cookies = []
    for index in range(ACTORS_PER_ROLE):
        email = f"perf-{role}-{index}@{tenant.slug}.example.com"
        user, created = User.objects.get_or_create(email=email)
        if created:
            user.set_unusable_password()
            user.save(update_fields=["password"])
            TOTP.activate(user, generate_totp_secret())
            EmailAddress.objects.create(user=user, email=email, verified=True)
        # PLATFORM_QUERY_OK: local measurement identities inside the synthetic firm.
        Membership.objects.get_or_create(
            tenant=tenant, user=user, client=company, defaults={"role": role}
        )
        cookies.append(session_cookie(user))
    return tuple(cookies)


def read_targets(slug: str) -> tuple[ReadTarget, ...]:
    """Resolve only the seeded growth firm and its own portal client's cycle."""
    # PLATFORM_QUERY_OK: the local-only harness requires this named synthetic firm.
    tenant = Tenant.objects.get(slug=slug)
    portal = User.objects.get(email=f"portal@{slug}.example.com")
    membership = Membership.objects.get(user=portal, tenant=tenant)
    client_id = membership.client_id
    if client_id is None:
        message = "The seeded portal user must have a client-scoped membership."
        raise ValueError(message)
    with tenant_context(tenant.pk):
        company = ClientCompany.objects.get(pk=client_id)
        if ClientCompany.objects.count() != GROWTH_CLIENTS:
            message = "The measurement requires the seeded 1,000-client growth firm."
            raise ValueError(message)
        cycle = ServiceCycle.objects.filter(client=company).latest("period_start")
        owner_cookies = _actors(tenant, None)
        portal_cookies = _actors(tenant, company)
    host = f"{slug}.localhost"
    return (
        ReadTarget("hoje", host, "/", owner_cookies, b"Hoje"),
        ReadTarget("clientes", host, "/clientes/", owner_cookies, b"Empresa sint"),
        ReadTarget(
            "workspace",
            host,
            (
                reverse("client-detail", args=[company.pk])
                + f"?periodo={cycle.period_start:%Y-%m}"
            ),
            owner_cookies,
            company.legal_name.encode(),
        ),
        ReadTarget(
            "cycle_detail",
            host,
            reverse("cycle-detail", args=[cycle.pk]),
            owner_cookies,
            b"Documentos sint",
        ),
        ReadTarget(
            "portal_inicio",
            f"{slug}-portal.localhost",
            "/",
            portal_cookies,
            b"Documentos sint",
        ),
    )


def remove_sessions(targets: tuple[ReadTarget, ...]) -> int:
    """Delete only session keys issued by this measurement, including on failure."""
    keys = {cookie.split("=", 1)[1] for target in targets for cookie in target.cookies}
    deleted, _counts = Session.objects.filter(session_key__in=keys).delete()
    return deleted
