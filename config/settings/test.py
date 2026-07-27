"""Test settings: fast hashers and the unprivileged application database role."""

from config.settings.base import *  # noqa: F403
from config.settings.base import DATABASES, INSTALLED_APPS, env

DEBUG = False

# Concrete tenant-scoped fixture models. Row-level security, the scoped manager and the
# isolation suite all need a real table to assert against, and the first business table
# does not arrive until the client registry several waves later.
INSTALLED_APPS = [*INSTALLED_APPS, "apps.core.tests"]

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Key mutation, never a wholesale reassignment — rebuilding DATABASES here would drop
# ATOMIC_REQUESTS in the one module the manage.py system checks never validate.
#
# Tests connect as app_test, NOT as app_runtime. app_runtime has no CREATEDB, so
# pointing this at it would kill pytest-django at CREATE DATABASE before a single test
# ran, and it cannot TRUNCATE app_test-owned tables either. The assertions still run
# under app_runtime: tests/conftest.py issues SET ROLE app_runtime per test, and
# PostgreSQL evaluates RLS against the *current* role, so policies still bite.
#
# `or` rather than a default: .env may ship these keys present-but-empty, so an empty
# string must fall back too, not just an absent variable.
DATABASES["default"]["USER"] = env.str("TEST_DB_USER", default="") or "app_test"
DATABASES["default"]["PASSWORD"] = (
    env.str("TEST_DB_PASSWORD", default="") or "app_test_password"
)
DATABASES["default"]["CONN_MAX_AGE"] = 0

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# WhiteNoise scans STATIC_ROOT eagerly and warns when it is absent. Outside
# production nobody has run collectstatic, and that warning is an error under
# pytest's filterwarnings=["error"]. Autorefresh is the documented setting for
# non-collected environments and keeps the middleware stack identical everywhere.
WHITENOISE_AUTOREFRESH = True
