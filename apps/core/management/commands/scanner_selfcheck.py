"""Prove the integrated scanner end to end on a host (AXRM-005, operator gate OG-R1).

`manage.py scanner_selfcheck --tenant <slug>` takes a SYNTHETIC tenant carrying
`scan_required` (with `cycles_r1`) and sends two PDFs through the real vault intake
(`apps.cycles.intake.vault_upload`: sniff -> quarantine -> intake) attributed to one
of its client members, then scans each through the configured clamd
(`scan_intake`, phases A-B-C). It prints each verdict and passes only when the PDF
carrying the EICAR test string in a stream object is `rejeitado_malware` and the
clean PDF is `verificado`. Each run adds a fresh nonce, so a rerun never collides with
a previous upload.

It refuses before writing anything when the tenant lacks the flag, when
`CLAMAV_ENABLED` is off, or when clamd does not answer PING + VERSION. What it
leaves behind is the record of the check: the EICAR original retained in quarantine
(never served) and a clean synthetic PDF in that client's vault.
"""

import secrets
from argparse import ArgumentParser
from typing import Any, Final

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import DocumentIntake, IntakeState
from apps.cycles.intake import UploadRejected, vault_upload
from apps.cycles.scanning import (
    EICAR,
    ClamdScanner,
    ScannerUnavailable,
    clamd_from_settings,
    scan_intake,
    scan_required,
)
from apps.tenants.models import Membership, Tenant

EICAR_NAME: Final = "scanner-selfcheck-eicar.pdf"
CLEAN_NAME: Final = "scanner-selfcheck-limpo.pdf"


def _pdf(nonce: str, body: bytes) -> bytes:
    """Build a minimal PDF whose one stream object carries `body` (clamd scans it)."""
    return (
        b"%PDF-1.4\n%scanner-selfcheck " + nonce.encode() + b"\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
        b"3 0 obj\n<< /Length "
        + str(len(body)).encode()
        + b" >>\nstream\n"
        + body
        + b"\nendstream\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"
    )


class Command(BaseCommand):
    """Upload EICAR and a clean PDF through the real intake and print the verdicts."""

    help = "Scan EICAR and a clean PDF through the real intake of a synthetic tenant."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Name the synthetic tenant by slug."""
        parser.add_argument("--tenant", required=True)

    def handle(self, *_args: Any, **options: Any) -> None:  # noqa: ANN401
        """Refuse first, then upload, scan and compare each verdict."""
        slug = str(options["tenant"])
        # PLATFORM_QUERY_OK: the operator names the one synthetic tenant to check.
        tenant = Tenant.objects.filter(slug=slug).first()
        if tenant is None:
            msg = f"There is no tenant with slug {slug!r}."
            raise CommandError(msg)
        if not scan_required(tenant.pk):
            msg = (
                f"Refusing: tenant {slug!r} lacks scan_required (with cycles_r1); "
                "the selfcheck exercises the required-scanning intake only."
            )
            raise CommandError(msg)
        clamd = clamd_from_settings()
        if clamd is None:
            msg = "Refusing: CLAMAV_ENABLED is False, so no upload could be scanned."
            raise CommandError(msg)
        try:
            clamd.ping()
            engine, signatures = clamd.version()
        except ScannerUnavailable as error:
            msg = f"clamd at {settings.CLAMD_HOST}:{settings.CLAMD_PORT}: {error}"
            raise CommandError(msg) from error
        with tenant_context(tenant.pk):
            # PLATFORM_QUERY_OK: memberships carry no row policy; this reads only the
            # synthetic tenant the operator named, for a client-side member (a client
            # membership always holds a client role, by CHECK) to upload as.
            members = Membership.objects.filter(
                tenant=tenant, client__isnull=False, is_active=True
            )
            member = (
                members.select_related("user", "client")
                .order_by("created_at", "pk")
                .first()
            )
        if member is None or member.client is None:
            msg = f"Refusing: tenant {slug!r} has no active client member to upload as."
            raise CommandError(msg)
        self.stdout.write(
            f"scanner_selfcheck tenant={slug} client={member.client_id} "
            f"clamd={settings.CLAMD_HOST}:{settings.CLAMD_PORT} engine={engine} "
            f"signatures={signatures}"
        )
        nonce = secrets.token_hex(8)
        cases = (
            ("eicar", EICAR_NAME, _pdf(nonce, EICAR), IntakeState.REJECTED_MALWARE),
            (
                "limpo",
                CLEAN_NAME,
                _pdf(nonce, b"scanner selfcheck"),
                IntakeState.VERIFIED,
            ),
        )
        failed = False
        for label, name, payload, expected in cases:
            state, reason, document = self._check(
                tenant, member.user, member.client, (name, payload), clamd
            )
            verdict = "PASS" if state == expected else "FAIL"
            failed = failed or verdict == "FAIL"
            words = [f"{label}:", state, reason, verdict, f"document={document}"]
            self.stdout.write(" ".join(word for word in words if word))
        if failed:
            self.stdout.write("RESULT FAIL")
            msg = "RESULT FAIL: a verdict differs from the expected one (see above)."
            raise CommandError(msg)
        self.stdout.write("RESULT PASS")

    @staticmethod
    def _check(
        tenant: Tenant,
        user: User,
        client: ClientCompany,
        file: tuple[str, bytes],
        clamd: ClamdScanner,
    ) -> tuple[str, str, str]:
        """Upload through the vault intake, scan once, return (state, reason, doc)."""
        name, payload = file
        upload = SimpleUploadedFile(name, payload, content_type="application/pdf")
        with tenant_context(tenant.pk):
            try:
                document, _ = vault_upload(user, upload, tenant=tenant, client=client)
            except UploadRejected:
                return "recusado_na_entrada", "", ""
            intake_id = DocumentIntake.objects.get(document=document).pk
        scan_intake(tenant.pk, intake_id, scanner=clamd, now=timezone.now())
        with tenant_context(tenant.pk):
            intake = DocumentIntake.objects.get(pk=intake_id)
        return str(intake.state), intake.reason, str(document.pk)
