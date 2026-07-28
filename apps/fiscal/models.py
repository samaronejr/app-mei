"""The municipality capability registry.

**These tables are platform-level, not tenant-scoped, and that is deliberate.** Which
NFS-e system Curitiba runs is a fact about Curitiba, not about any one accounting firm.
A per-tenant copy would let two firms hold contradictory answers about the same city
and would make every firm re-discover the same municipal variance independently. Both
tables are therefore listed in `apps.core.rls.NON_TENANT_TABLES`; T-014's coverage
meta-test fails the build if a table is neither tenant-scoped nor listed there.

**Scope limit: data shape and lookup only.** There are no NFS-e API calls here and no
connector logic. Municipal NFS-e integration is Phase 3. What this registry buys today
is the ability to answer "what will this client run into?" from stored data, and to
tell the difference between *verified as not required* and *nobody has checked*.
"""

from typing import ClassVar

from django.db import models
from django.utils.translation import gettext_lazy as _

# IBGE municipality codes are seven digits, and the first two are the state's own IBGE
# code. That relationship is asserted in the registry pack, which is what would catch a
# transcription error in the seed rather than letting a wrong code become a lookup miss.
IBGE_CODE_LENGTH = 7
IBGE_UF_PREFIX_LENGTH = 2


class UF(models.TextChoices):
    """The 26 states and the Federal District."""

    AC = "AC", _("Acre")
    AL = "AL", _("Alagoas")
    AP = "AP", _("Amapá")
    AM = "AM", _("Amazonas")
    BA = "BA", _("Bahia")
    CE = "CE", _("Ceará")
    DF = "DF", _("Distrito Federal")
    ES = "ES", _("Espírito Santo")
    GO = "GO", _("Goiás")
    MA = "MA", _("Maranhão")
    MT = "MT", _("Mato Grosso")
    MS = "MS", _("Mato Grosso do Sul")
    MG = "MG", _("Minas Gerais")
    PA = "PA", _("Pará")
    PB = "PB", _("Paraíba")
    PR = "PR", _("Paraná")
    PE = "PE", _("Pernambuco")
    PI = "PI", _("Piauí")
    RJ = "RJ", _("Rio de Janeiro")
    RN = "RN", _("Rio Grande do Norte")
    RS = "RS", _("Rio Grande do Sul")
    RO = "RO", _("Rondônia")
    RR = "RR", _("Roraima")
    SC = "SC", _("Santa Catarina")
    SP = "SP", _("São Paulo")
    SE = "SE", _("Sergipe")
    TO = "TO", _("Tocantins")


class Municipality(models.Model):
    """One Brazilian municipality, keyed by its IBGE code.

    The IBGE code is the primary key rather than a surrogate. It is the identifier
    every fiscal document already carries, it is stable, and it is what
    `ClientCompany.municipality_ibge_code` holds — so a surrogate key would add a join
    and a second way to name the same city.
    """

    ibge_code = models.CharField(
        _("IBGE code"),
        primary_key=True,
        max_length=IBGE_CODE_LENGTH,
    )
    name = models.CharField(_("name"), max_length=120)
    uf = models.CharField(_("UF"), max_length=2, choices=UF.choices)

    class Meta:
        """Model metadata."""

        verbose_name = _("municipality")
        verbose_name_plural = _("municipalities")
        ordering: ClassVar[list[str]] = ["uf", "name"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(ibge_code__regex=r"^[0-9]{7}$"),
                name="municipality_ibge_code_is_seven_digits",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["uf"], name="municipality_uf_idx"),
        ]

    def __str__(self) -> str:
        """Identify the municipality the way an accountant would write it."""
        return f"{self.name}/{self.uf}"


class MunicipalityCapability(models.Model):
    """What a firm will run into when issuing invoices in this municipality.

    A one-to-one rather than a plain foreign key. Two capability rows for one city
    would make `capability_for` non-deterministic, and "which of these two
    contradictory answers is current?" is precisely the question this table exists to
    remove. `verified_on` records when a human last checked, which is what separates
    stale data from absent data.
    """

    municipality = models.OneToOneField(
        Municipality,
        on_delete=models.CASCADE,
        related_name="capability",
        verbose_name=_("municipality"),
    )
    nfse_national_emitter = models.BooleanField(
        _("uses the national NFS-e emitter"),
        default=False,
    )
    requires_certificate = models.BooleanField(
        _("requires a digital certificate"),
        default=True,
    )
    notes = models.TextField(_("notes"), blank=True)
    verified_on = models.DateField(_("verified on"), null=True, blank=True)

    class Meta:
        """Model metadata."""

        verbose_name = _("municipality capability")
        verbose_name_plural = _("municipality capabilities")
        ordering: ClassVar[list[str]] = ["municipality"]

    def __str__(self) -> str:
        """Identify the capability by the municipality it describes."""
        return f"capability for {self.municipality}"
