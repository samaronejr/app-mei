"""Brazilian presentation of money, dates and competence months.

Formatting is done here rather than through `django.utils.formats` because the two
differ in a way that matters for a fiscal document. Django's localization reads the
active locale, which a request can influence through `Accept-Language`; a real
accounting figure must render as `R$ 1.234,56` for every reader, in every locale, or
two people reading the same screen disagree about what is owed.

Money never passes through `float`. `Decimal(str(value))` keeps the digits the caller
wrote, where `Decimal(1234.5)` would keep the binary approximation of them.
"""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

from django import template
from django.utils.translation import gettext_lazy as _

from apps.clients.models import ClientStatus, GovBrTrustLevel, OnboardingStatus
from apps.obligations.models import ObligationStatus
from apps.tenants.models import TenantRole

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise

register = template.Library()

BRT = ZoneInfo("America/Sao_Paulo")
CENTS = Decimal("0.01")

# Lower case and unpunctuated, which is how a Brazilian calendar strip reads.
MONTH_ABBREVIATIONS = (
    "jan",
    "fev",
    "mar",
    "abr",
    "mai",
    "jun",
    "jul",
    "ago",
    "set",
    "out",
    "nov",
    "dez",
)

EMPTY = "—"
UNKNOWN_LABEL: Final["StrOrPromise"] = _("Desconhecido")

TENANT_ROLE_LABELS: Final[dict[str, "StrOrPromise"]] = {
    TenantRole.OWNER.value: _("Proprietário"),
    TenantRole.STAFF_ACCOUNTANT.value: _("Contador"),
    TenantRole.OPERATIONS_ADMIN.value: _("Administrador de operações"),
    TenantRole.CLIENT_OWNER.value: _("Titular do MEI"),
    TenantRole.CLIENT_COLLABORATOR.value: _("Colaborador do MEI"),
}

CLIENT_STATUS_LABELS: Final[dict[str, "StrOrPromise"]] = {
    ClientStatus.ONBOARDING.value: _("Em onboarding"),
    ClientStatus.ACTIVE.value: _("Ativo"),
    ClientStatus.SUSPENDED.value: _("Suspenso"),
    ClientStatus.CLOSED.value: _("Encerrado"),
}

OBLIGATION_STATUS_LABELS: Final[dict[str, "StrOrPromise"]] = {
    ObligationStatus.SCHEDULED.value: _("Programada"),
    ObligationStatus.DUE.value: _("A pagar"),
    ObligationStatus.PAID.value: _("Paga"),
    ObligationStatus.OVERDUE.value: _("Em atraso"),
    ObligationStatus.WAIVED.value: _("Dispensada"),
}

ONBOARDING_STATUS_LABELS: Final[dict[str, "StrOrPromise"]] = {
    OnboardingStatus.PENDING.value: _("Pendente"),
    OnboardingStatus.BLOCKED.value: _("Travado"),
    OnboardingStatus.DONE.value: _("Concluído"),
    OnboardingStatus.NOT_APPLICABLE.value: _("Não se aplica"),
}

# The gov.br tier is a RECORDED claim rather than a verified one, and only one of its
# four values reads as English -- `bronze`, `prata` and `ouro` are already the words a
# Brazilian reader expects. It is mapped here anyway, in full, for two reasons: a map
# covering three of four values is one an exhaustive-key guard cannot hold, and the tier
# renders beside the client status on the same card, so a second mechanism installed for
# one word would reinstate exactly the split this family exists to end.
#
# `Não informado` and NOT `Desconhecido`: the latter is this module's fallback for a
# value no map knows, so reusing it would make "nobody has asked this client" and "the
# enum grew a member nobody mapped" render identically -- and the second is a defect the
# first would then hide. The distinction is what `GovBrTrustLevel`'s own docstring calls
# the difference between "nobody has asked" and an answer that disqualifies them.
GOVBR_TRUST_LEVEL_LABELS: Final[dict[str, "StrOrPromise"]] = {
    GovBrTrustLevel.UNKNOWN.value: _("Não informado"),
    GovBrTrustLevel.BRONZE.value: _("Bronze"),
    GovBrTrustLevel.PRATA.value: _("Prata"),
    GovBrTrustLevel.OURO.value: _("Ouro"),
}


def _as_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


@register.filter(name="brl")
def brl(value: object) -> str:
    """Render an amount as Brazilian currency: `1234.5` becomes `R$ 1.234,50`."""
    amount = _as_decimal(value)
    if amount is None:
        return EMPTY
    quantized = amount.quantize(CENTS)
    sign = "-" if quantized < 0 else ""
    units, _sep, cents = f"{abs(quantized):.2f}".partition(".")
    grouped = f"{int(units):,}".replace(",", ".")
    return f"{sign}R$ {grouped},{cents}"


@register.filter(name="data")
def data(value: date | None) -> str:
    """Render a date as `dd/mm/aaaa`."""
    if value is None:
        return EMPTY
    return value.strftime("%d/%m/%Y")


@register.filter(name="data_hora")
def data_hora(value: datetime | None) -> str:
    """Render a stored UTC timestamp in Brasília time as `dd/mm/aaaa HH:MM`.

    Timestamps are stored in UTC, so rendering one without converting shows an
    accountant a deadline up to three hours away from the one they are working to.
    A naive value is returned unconverted rather than assumed to be UTC — guessing
    would silently shift a timestamp that was already local.
    """
    if value is None:
        return EMPTY
    if value.tzinfo is not None:
        value = value.astimezone(BRT)
    return value.strftime("%d/%m/%Y %H:%M")


@register.filter(name="competencia")
def competencia(value: date | None) -> str:
    """Render a competence month as `jan/2026`."""
    if value is None:
        return EMPTY
    return f"{MONTH_ABBREVIATIONS[value.month - 1]}/{value.year}"


@register.filter(name="cnpj_mask")
def cnpj_mask(value: str | None) -> str:
    """Punctuate a stored CNPJ as `12.345.678/0001-90`.

    Applied to the alphanumeric format too: the 2026 registrations keep the same
    fourteen positions and the same punctuation, only the character set widens.
    """
    digits = (value or "").strip()
    expected_length = 14
    if len(digits) != expected_length:
        return digits or EMPTY
    return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"


@register.filter(name="percentual")
def percentual(value: object) -> str:
    """Render a ratio already expressed in percent as `82,4%`."""
    amount = _as_decimal(value)
    if amount is None:
        return EMPTY
    return f"{amount.quantize(Decimal('0.1'))}".replace(".", ",") + "%"


STATUS_LABELS = {
    "ok": _("dentro do limite"),
    "warning": _("perto do limite"),
    "exceeded_within_tolerance": _("excedido (até 20%)"),
    "exceeded_over_tolerance": _("excedido (acima de 20%)"),
}


@register.filter(name="banda_limite")
def banda_limite(value: str) -> str:
    """Name a revenue threshold band in the words an accountant uses."""
    return str(STATUS_LABELS.get(value, value))


@register.filter(name="papel")
def papel(value: str) -> str:
    """Name a tenant role in pt-BR."""
    return str(TENANT_ROLE_LABELS.get(value, UNKNOWN_LABEL))


@register.filter(name="situacao_cliente")
def situacao_cliente(value: str) -> str:
    """Name a client status in pt-BR."""
    return str(CLIENT_STATUS_LABELS.get(value, UNKNOWN_LABEL))


@register.filter(name="situacao_obrigacao")
def situacao_obrigacao(value: str) -> str:
    """Name an obligation status in pt-BR."""
    return str(OBLIGATION_STATUS_LABELS.get(value, UNKNOWN_LABEL))


@register.filter(name="situacao_onboarding")
def situacao_onboarding(value: str) -> str:
    """Name an onboarding status in pt-BR."""
    return str(ONBOARDING_STATUS_LABELS.get(value, UNKNOWN_LABEL))


@register.filter(name="nivel_govbr")
def nivel_govbr(value: str) -> str:
    """Name a gov.br trust level in pt-BR."""
    return str(GOVBR_TRUST_LEVEL_LABELS.get(value, UNKNOWN_LABEL))
