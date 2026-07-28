"""The audit trail, split in two because a tenant-scoped table cannot record login.

`Event` is tenant-scoped and therefore carries the row-level-security policy every
business table carries — including its `WITH CHECK` half. At **login** no tenant has
been resolved yet, so `app.tenant_id` is the empty string, the predicate evaluates to
NULL, and the INSERT is **rejected**. Verified empirically on PostgreSQL 16. The same
kills anonymous invite acceptance and data-subject-request intake.

A single audit table would therefore have made the product's own login flow impossible
while looking correct in review. So:

* **`Event`** — tenant-attributable actions, written inside a resolved tenant context,
  policed by RLS like everything else the firm owns.
* **`PlatformEvent`** — identity events that happen *before or without* a tenant:
  login success, login failure, MFA changes, invite issue and acceptance, DSR intake.
  Platform level, listed in `NON_TENANT_TABLES`, nullable tenant FK for correlation.

Both are append-only, and that is enforced by a **trigger**, not merely by grants —
`app_migrator` and `app_test` own these tables and hold `BYPASSRLS`, so a revoke aimed
at `app_runtime` leaves the owners free to rewrite history. See the migration.
"""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.access import PlatformScopedManager
from apps.core.models import TenantScopedModel, UUIDv7PrimaryKeyModel


class AuditAction(models.TextChoices):
    """Every action the product will ever audit, including ones not yet built.

    Later phases add behaviour, not migrations: an incomplete enum would mean an
    `ALTER TYPE` (or a `CharField` choices migration) each time a feature lands, and
    the pressure at that moment is always to log nothing rather than migrate.
    """

    LOGIN_SUCCEEDED = "login_succeeded", _("login succeeded")
    LOGIN_FAILED = "login_failed", _("login failed")
    LOGOUT = "logout", _("logout")
    MFA_ENROLLED = "mfa_enrolled", _("MFA enrolled")
    MFA_REMOVED = "mfa_removed", _("MFA removed")
    MFA_RESET = "mfa_reset", _("MFA reset")
    PASSWORD_CHANGED = "password_changed", _("password changed")
    INVITE_ISSUED = "invite_issued", _("invite issued")
    INVITE_ACCEPTED = "invite_accepted", _("invite accepted")
    ROLE_CHANGED = "role_changed", _("role changed")
    ASSIGNMENT_CHANGED = "assignment_changed", _("assignment changed")
    CONNECTOR_CONNECTED = "connector_connected", _("connector connected")
    CONNECTOR_DISCONNECTED = "connector_disconnected", _("connector disconnected")
    INVOICE_ATTEMPTED = "invoice_attempted", _("invoice attempted")
    DAS_CONFIRMED = "das_confirmed", _("DAS confirmed")
    DECLARATION_SUBMITTED = "declaration_submitted", _("declaration submitted")
    EXPORT = "export", _("export")
    IMPERSONATION = "impersonation", _("impersonation")
    TENANT_SELECTED = "tenant_selected", _("tenant selected")
    ALL_TENANTS_ACCESS = "all_tenants_access", _("cross-tenant access")
    DSR_SUBMITTED = "dsr_submitted", _("data subject request submitted")
    DENIED = "denied", _("action denied")


class Event(TenantScopedModel):
    """A tenant-attributable action, written inside a resolved tenant context.

    `metadata` holds **references** — `object_type` and `object_id` — never document
    numbers or names. The table is append-only and LGPD grants erasure rights, so a
    CPF stored here would be an unerasable personal-data store. A guard test asserts
    no value in this column is CPF- or CNPJ-shaped.
    """

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="audit_events",
        verbose_name=_("actor"),
    )
    action = models.CharField(_("action"), max_length=48, choices=AuditAction.choices)
    object_type = models.CharField(_("object type"), max_length=64, blank=True)
    object_id = models.CharField(_("object id"), max_length=64, blank=True)
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        """Model metadata."""

        verbose_name = _("audit event")
        verbose_name_plural = _("audit events")
        ordering: ClassVar[list[str]] = ["-created_at"]
        indexes: ClassVar[list[models.Index]] = [
            # tenant_id leads: the RLS predicate filters on it, and an index that does
            # not lead with it stops serving that predicate.
            models.Index(
                fields=["tenant", "-created_at"], name="audit_event_tenant_ts"
            ),
            models.Index(
                fields=["tenant", "action"],
                name="audit_event_tenant_action",
            ),
        ]

    def __str__(self) -> str:
        """Identify the record by what happened and when."""
        return f"{self.action} @ {self.created_at:%Y-%m-%d %H:%M:%S}"


class PlatformEvent(UUIDv7PrimaryKeyModel):
    """An identity event that happens before, or entirely without, a tenant.

    Deliberately NOT tenant-scoped. Its whole reason to exist is that the fail-closed
    policy on `Event` would reject exactly the records an incident investigation needs
    most — a failed login, by definition, has no resolved tenant behind it.
    """

    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="platform_events",
        verbose_name=_("tenant"),
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="platform_events",
        verbose_name=_("actor"),
    )
    action = models.CharField(_("action"), max_length=48, choices=AuditAction.choices)
    # Recorded as text rather than a FK to accounts_user: a failed login names an
    # address that may not have an account, which is precisely the interesting case.
    subject = models.CharField(_("subject"), max_length=254, blank=True)
    ip = models.GenericIPAddressField(_("IP address"), null=True, blank=True)
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    objects = PlatformScopedManager["PlatformEvent"]()

    class Meta:
        """Model metadata."""

        verbose_name = _("platform event")
        verbose_name_plural = _("platform events")
        ordering: ClassVar[list[str]] = ["-created_at"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["-created_at"], name="audit_platform_ts"),
            models.Index(
                fields=["action", "-created_at"], name="audit_platform_action"
            ),
        ]

    def __str__(self) -> str:
        """Identify the record by what happened and when."""
        return f"{self.action} @ {self.created_at:%Y-%m-%d %H:%M:%S}"


class AccessLog(UUIDv7PrimaryKeyModel):
    """The Marco Civil access record: who reached what, from where, and when.

    Deliberately a different table from `Event`, with a different purpose, a different
    retention period (six months, purged) and a different access policy. Folding it
    into the business audit trail would force one retention rule onto two obligations
    that genuinely differ.

    `tenant` is present but **nullable**, and the table carries no policy. Both follow
    from the same requirement: a firm must be able to ask "who reached my data?", and
    the log must also capture pre-authentication and anonymous requests — which have
    no tenant, and which a fail-closed policy would refuse to record at all. Scoping
    is applied in the application layer through `objects.for_user()`.
    """

    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="access_logs",
        verbose_name=_("tenant"),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="access_logs",
        verbose_name=_("user"),
    )
    ip = models.GenericIPAddressField(_("IP address"), null=True, blank=True)
    user_agent = models.TextField(_("user agent"), blank=True)
    method = models.CharField(_("method"), max_length=10)
    path = models.CharField(_("path"), max_length=2048)
    status_code = models.PositiveSmallIntegerField(_("status code"))
    created_at = models.DateTimeField(_("created at"), auto_now_add=True, db_index=True)

    objects = PlatformScopedManager["AccessLog"]()

    class Meta:
        """Model metadata."""

        verbose_name = _("access log")
        verbose_name_plural = _("access logs")
        ordering: ClassVar[list[str]] = ["-created_at"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["tenant", "-created_at"], name="audit_access_tenant_ts"
            ),
        ]

    def __str__(self) -> str:
        """Identify the record by the request it describes."""
        return f"{self.method} {self.path} -> {self.status_code}"
