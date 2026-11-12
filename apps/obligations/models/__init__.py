"""The obligation engine's tables, split by responsibility and re-exported here.

Django discovers models through this package, so every concrete model must be
imported below or its table is never created.
"""

from apps.obligations.models.documents import (
    AcquisitionOrigin,
    Document,
    new_storage_key,
)
from apps.obligations.models.federal import (
    FederalConsultation,
    FederalConsultationKind,
    FederalConsultationOutcome,
    FederalEnvironment,
    FederalPayment,
    FederalPaymentType,
    FiscalReportState,
    FiscalSituationReport,
)
from apps.obligations.models.observations import (
    ObservationKind,
    ObservationSourceKind,
    ObservationSubjectKind,
    ObservationVerificationStatus,
    SourceObservation,
)
from apps.obligations.models.operations import (
    Obligation,
    ObligationStatus,
    SchedulerHeartbeat,
)
from apps.obligations.models.reference import (
    FiscalParameter,
    Holiday,
    ObligationDueRule,
    ObligationType,
    Periodicity,
)
from apps.obligations.models.revenue import MonthlyRevenue

__all__ = [
    "AcquisitionOrigin",
    "Document",
    "FederalConsultation",
    "FederalConsultationKind",
    "FederalConsultationOutcome",
    "FederalEnvironment",
    "FederalPayment",
    "FederalPaymentType",
    "FiscalParameter",
    "FiscalReportState",
    "FiscalSituationReport",
    "Holiday",
    "MonthlyRevenue",
    "Obligation",
    "ObligationDueRule",
    "ObligationStatus",
    "ObligationType",
    "ObservationKind",
    "ObservationSourceKind",
    "ObservationSubjectKind",
    "ObservationVerificationStatus",
    "Periodicity",
    "SchedulerHeartbeat",
    "SourceObservation",
    "new_storage_key",
]
