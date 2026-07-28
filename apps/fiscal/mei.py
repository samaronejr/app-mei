"""The MEI categories, which decide which annual ceiling a client is measured against.

This lives in `apps.fiscal` rather than in `apps.obligations` because both the client
registry and the obligation engine need it, and the registry must not depend on the
engine — `Obligation` and `MonthlyRevenue` point the other way, at `ClientCompany`.

**The ceiling is not a single scalar.** MEI-Caminhoneiro carries a substantially
higher annual limit than common MEI (LC 188/2021), so a product that models
`mei.annual_ceiling` as one global number measures every trucker against the wrong
figure. The wrongness is invisible: the result is a plausible percentage, and the
first visible symptom is a client desenquadrado by Receita Federal while the firm's
dashboard still showed green.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _


class MEICategory(models.TextChoices):
    """The two MEI regimes this product distinguishes.

    Adding a third requires seeding its own `mei.annual_ceiling` and
    `mei.monthly_proportional` rows in the same commit: the resolver raises
    `NoEffectiveParameter` for an unseeded category rather than falling back to a
    default, so an unseeded category fails loudly at the first threshold check.
    """

    COMMON = "common", _("MEI comum")
    CAMINHONEIRO = "caminhoneiro", _("MEI caminhoneiro")
