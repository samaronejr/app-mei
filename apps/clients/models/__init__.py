"""The client registry's tables, split by responsibility and re-exported here.

Django discovers models through this package, so every concrete model must be
imported below or its table is never created.
"""

from apps.clients.models.assignment import AssignmentRole, ClientAssignment
from apps.clients.models.company import (
    CNPJ_LENGTH,
    CNPJ_SHAPE,
    CPF_LENGTH,
    CPF_SHAPE,
    ClientCompany,
    ClientStatus,
    GovBrTrustLevel,
)
from apps.clients.models.contact import ClientContact
from apps.clients.models.engagement import Engagement, ServiceKind
from apps.clients.models.onboarding import (
    OnboardingItem,
    OnboardingItemTemplate,
    OnboardingStatus,
)
from apps.clients.models.tagging import ClientTag, Tag
from apps.clients.models.tax_profile import (
    ProfileConfidence,
    TaxProfile,
    TaxRegime,
)

__all__ = [
    "CNPJ_LENGTH",
    "CNPJ_SHAPE",
    "CPF_LENGTH",
    "CPF_SHAPE",
    "AssignmentRole",
    "ClientAssignment",
    "ClientCompany",
    "ClientContact",
    "ClientStatus",
    "ClientTag",
    "Engagement",
    "GovBrTrustLevel",
    "OnboardingItem",
    "OnboardingItemTemplate",
    "OnboardingStatus",
    "ProfileConfidence",
    "ServiceKind",
    "Tag",
    "TaxProfile",
    "TaxRegime",
]
