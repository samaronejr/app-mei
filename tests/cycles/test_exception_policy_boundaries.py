"""Independent certificate boundary and wrong-company security-waiver cases."""

from datetime import timedelta
from uuid import uuid4

import pytest

from apps.core.tenancy import tenant_context
from apps.cycles.errors import IllegalTransition
from apps.cycles.models import Exception as CycleException
from apps.cycles.models import ExceptionState
from apps.cycles.openers import CERTIFICATE_EXPIRING, evaluate_tenant_exceptions
from apps.cycles.services.exceptions import transition_exception
from tests.cycles.cycle_fixtures import cycle_setup
from tests.cycles.test_exceptions import TODAY, _cycle, _open, _writes

pytestmark = pytest.mark.django_db(transaction=True)


def test_fresh_day_31_certificate_does_not_open_an_exception() -> None:
    setup = cycle_setup("day-31")
    with tenant_context(setup.tenant.pk):
        _cycle(setup)
        setup.client.certificate_expires_on = TODAY + timedelta(days=31)
        setup.client.save(update_fields=["certificate_expires_on"])
        assert not CycleException.objects.exists()
        evaluate_tenant_exceptions(setup.tenant, today=TODAY)
        assert not CycleException.objects.filter(
            reason_code=CERTIFICATE_EXPIRING
        ).exists()


def test_wrong_company_waiver_is_refused_without_writes() -> None:
    setup = cycle_setup("wrong-company-waiver")
    with tenant_context(setup.tenant.pk):
        cycle = _cycle(setup)
        problem = _open(setup, cycle, "documento_de_outra_empresa", uuid4())
        problem = transition_exception(
            setup.actor,
            problem.pk,
            ExceptionState.HANDLING,
            expected_version=problem.version,
        )
        before = _writes()
        with pytest.raises(IllegalTransition):
            transition_exception(
                setup.actor,
                problem.pk,
                ExceptionState.RISK_ACCEPTED,
                expected_version=problem.version,
                note="Synthetic attempted security waiver.",
            )
        assert _writes() == before
