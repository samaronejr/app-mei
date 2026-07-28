"""Looking up what a municipality requires, including when the answer is "unknown".

The whole point of this module is the third state. A boolean pair can say "the national
emitter works here" and "a certificate is needed here", but it cannot say **nobody has
checked** — and Brazil has 5,570 municipalities, so for most of them nobody has. A
lookup that collapsed the unknown case into the seeded defaults would report an
unverified guess in the same shape as a verified fact, and an accountant reading the
screen would have no way to tell them apart.

So `capability_for` never raises and never invents a permissive answer. It returns a
record whose `status` is either `known` or `unknown`, and for `unknown` it fills the
flags with the **conservative** value in each direction:

* `nfse_national_emitter=False` — never claim a capability that has not been verified.
  Claiming it wrongly sends a firm down an integration path that does not exist for
  that city, and it is the "silent True" the plan forbids.
* `requires_certificate=True` — assume the stricter requirement. Being wrong this way
  costs a firm an unnecessary certificate purchase; being wrong the other way tells a
  client they can issue invoices with a gov.br login, and they discover otherwise on
  the day a deadline falls.

Note the two conservative values point in opposite directions. "Conservative" here
means *assume the least convenient truth*, not *assume False*.
"""

from dataclasses import dataclass
from datetime import date

from django.db.models import TextChoices
from django.utils.translation import gettext_lazy as _

from apps.fiscal.models import Municipality


class CapabilityStatus(TextChoices):
    """Whether this answer was verified or is a conservative placeholder."""

    KNOWN = "known", _("known")
    UNKNOWN = "unknown", _("unknown")


# The values returned for a municipality nobody has assessed. Named constants rather
# than literals inside the function, so the registry pack can assert the policy itself
# rather than re-stating it.
UNKNOWN_NFSE_NATIONAL_EMITTER = False
UNKNOWN_REQUIRES_CERTIFICATE = True


@dataclass(frozen=True)
class MunicipalityCapabilityView:
    """One answer about one municipality, carrying whether it is actually known."""

    ibge_code: str
    status: str
    name: str = ""
    uf: str = ""
    nfse_national_emitter: bool = UNKNOWN_NFSE_NATIONAL_EMITTER
    requires_certificate: bool = UNKNOWN_REQUIRES_CERTIFICATE
    notes: str = ""
    verified_on: date | None = None

    @property
    def is_known(self) -> bool:
        """Report whether this answer came from a seeded, verified record."""
        return self.status == CapabilityStatus.KNOWN


def unknown_capability(ibge_code: str) -> MunicipalityCapabilityView:
    """Return the conservative placeholder for a municipality nobody has assessed."""
    return MunicipalityCapabilityView(
        ibge_code=ibge_code,
        status=CapabilityStatus.UNKNOWN,
        notes=str(
            _(
                "This municipality is not in the capability registry. The values "
                "shown are conservative defaults, not verified facts.",
            ),
        ),
    )


def capability_for(ibge_code: str) -> MunicipalityCapabilityView:
    """Return what is known about a municipality, or an explicit `unknown`.

    Never raises. An unseeded code, a blank string and a malformed code all produce the
    same `unknown` answer, because from the caller's point of view they are the same
    situation: there is nothing verified to report. Raising would push a `try` block
    into every screen that displays a client, and the natural way to write that block
    is to swallow the error and carry on with a permissive default.
    """
    code = (ibge_code or "").strip()
    if not code:
        return unknown_capability(code)

    municipality = (
        Municipality.objects.filter(ibge_code=code).select_related("capability").first()
    )
    if municipality is None:
        return unknown_capability(code)

    capability = getattr(municipality, "capability", None)
    if capability is None:
        # The city is in the registry but nobody has recorded its capabilities. That
        # is still "unknown", and reporting it as known-with-defaults would be the
        # exact conflation this module exists to prevent.
        return MunicipalityCapabilityView(
            ibge_code=municipality.ibge_code,
            status=CapabilityStatus.UNKNOWN,
            name=municipality.name,
            uf=municipality.uf,
            notes=str(
                _(
                    "This municipality is registered but its capabilities have not "
                    "been assessed. The values shown are conservative defaults.",
                ),
            ),
        )

    return MunicipalityCapabilityView(
        ibge_code=municipality.ibge_code,
        status=CapabilityStatus.KNOWN,
        name=municipality.name,
        uf=municipality.uf,
        nfse_national_emitter=capability.nfse_national_emitter,
        requires_certificate=capability.requires_certificate,
        notes=capability.notes,
        verified_on=capability.verified_on,
    )
