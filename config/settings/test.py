"""Test settings: fast hashers and the unprivileged application database role."""

from config.settings.base import *  # noqa: F403
from config.settings.base import DATABASES, env

DEBUG = False

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Key mutation, never a wholesale reassignment — rebuilding DATABASES here would drop
# ATOMIC_REQUESTS in the one module the manage.py system checks never validate.
# The dedicated non-superuser test role arrives with the role split in T-009; until
# then these fall back to the credentials already parsed from DATABASE_URL.
# `or` rather than a default: .env ships these keys present-but-empty, so an empty
# string must fall back too, not just an absent variable.
DATABASES["default"]["USER"] = (
    env.str("TEST_DB_USER", default="") or DATABASES["default"]["USER"]
)
DATABASES["default"]["PASSWORD"] = (
    env.str("TEST_DB_PASSWORD", default="") or DATABASES["default"]["PASSWORD"]
)
DATABASES["default"]["CONN_MAX_AGE"] = 0

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# WhiteNoise scans STATIC_ROOT eagerly and warns when it is absent. Outside
# production nobody has run collectstatic, and that warning is an error under
# pytest's filterwarnings=["error"]. Autorefresh is the documented setting for
# non-collected environments and keeps the middleware stack identical everywhere.
WHITENOISE_AUTOREFRESH = True
