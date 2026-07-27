"""Local development settings."""

from config.settings.base import *  # noqa: F403
from config.settings.base import DATABASES, env

DEBUG = True

ALLOWED_HOSTS = env.list(
    "ALLOWED_HOSTS",
    default=["localhost", "127.0.0.1", ".localhost", "web"],
)

# Mutate keys instead of rebuilding the dict: a wholesale reassignment silently drops
# ATOMIC_REQUESTS, which connections.settings reads from settings.DATABASES.
DATABASES["default"]["CONN_MAX_AGE"] = env.int("CONN_MAX_AGE", default=0)

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# WhiteNoise scans STATIC_ROOT eagerly and warns when it is absent. Outside
# production nobody has run collectstatic, and that warning is an error under
# pytest's filterwarnings=["error"]. Autorefresh is the documented setting for
# non-collected environments and keeps the middleware stack identical everywhere.
WHITENOISE_AUTOREFRESH = True
