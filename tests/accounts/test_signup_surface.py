from http import HTTPStatus
from typing import TYPE_CHECKING, Final

import pytest
from django.test import Client
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

if TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse

from apps.accounts.models import User
from apps.tenants.models import Tenant

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD: Final = "correct-horse-battery-staple"  # noqa: S105
CLOSED_TEMPLATE: Final = "account/signup_closed.html"
SIGNUP_FORM: Final = "<form"

SURFACES: Final = (
    ("firm", "alpha.localhost", "config.urls"),
    ("portal", "alpha-portal.localhost", "apps.portal.urls"),
)


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]
    Tenant.objects.create(name="Alpha", slug="alpha")


def _request_signup(
    *,
    method: str,
    host: str,
    urlconf: str,
) -> "_MonkeyPatchedWSGIResponse":
    client = Client()
    path = reverse("account_signup", urlconf=urlconf)
    if method == "GET":
        return client.get(path, headers={"host": host})
    return client.post(
        path,
        {
            "email": f"public-signup@{host}",
            "password1": PASSWORD,
            "password2": PASSWORD,
        },
        headers={"host": host},
        REMOTE_ADDR="203.0.113.20",
    )


@pytest.mark.parametrize(("surface", "host", "urlconf"), SURFACES)
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_public_signup_is_closed_on_both_hosts_for_both_methods(
    surface: str,
    host: str,
    urlconf: str,
    method: str,
) -> None:
    before = User.objects.count()

    response = _request_signup(method=method, host=host, urlconf=urlconf)

    assert response.status_code == HTTPStatus.OK, f"{method} on {surface}"
    assert CLOSED_TEMPLATE in [str(template.name) for template in response.templates]
    assert SIGNUP_FORM not in response.content.decode().lower()
    assert User.objects.count() == before
