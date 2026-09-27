"""The project URLconf plus the browser suite's test-only pages.

Installed as `ROOT_URLCONF` by `tests/browser/conftest.py` for browser tests only.
The shipped `config.urls` never includes these routes.
"""

from django.urls import path

from config.urls import urlpatterns as project_urlpatterns
from tests.browser import views

urlpatterns = [
    path("__ui__/kit/", views.component_kit, name="browser-kit"),
    path("__ui__/kit/swap/", views.swap_fragment, name="browser-kit-swap"),
    path("__ui__/unnamed-button/", views.unnamed_button, name="browser-unnamed"),
    *project_urlpatterns,
]
