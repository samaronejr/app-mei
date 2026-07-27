"""Settings shared by every environment.

Subdomain tenancy (``<slug>.example.com``) drives the host and cookie policy here.
Cookies are deliberately HOST-ONLY: one subdomain per mutually-untrusted accounting
firm is exactly the deployment Django documents as unsafe for a parent-domain session
cookie, so ``SESSION_COOKIE_DOMAIN`` stays ``None`` and ``CSRF_TRUSTED_ORIGINS`` is an
explicit per-origin list rather than a wildcard.
"""

from pathlib import Path
from typing import Any

import django_stubs_ext
import environ

# Makes Django's generic classes subscriptable at runtime, so shipped code can write
# ModelAdmin[Tenant] and Manager[User] rather than losing the parameter to a bare name.
django_stubs_ext.monkeypatch()

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()

_env_file = Path(env.str("DJANGO_ENV_FILE", default=str(BASE_DIR / ".env")))
if _env_file.is_file():
    environ.Env.read_env(str(_env_file))

# Read with no default so a missing value raises ImproperlyConfigured instead of
# silently falling back to something guessable.
SECRET_KEY = env.str("SECRET_KEY")

DEBUG = env.bool("DEBUG", default=False)

# A leading dot matches every subdomain, which subdomain tenant resolution needs.
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

# Explicit per-origin entries only. A wildcard here is rejected by core.E003.
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_celery_beat",
    "apps.core",
    "apps.accounts",
    "apps.tenants",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # After AuthenticationMiddleware: tenant resolution reads request.user to check
    # membership, and running earlier would make that check pass for everyone.
    "apps.tenants.middleware.TenantMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES: list[dict[str, Any]] = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ATOMIC_REQUESTS is a PER-DATABASE key, never a module-level setting. Writing it at
# module level makes settings.ATOMIC_REQUESTS report True while the connection keeps
# False, so views are never wrapped and every 4xx path commits partial writes. Assert
# it via connections["default"].settings_dict, which core.E001 does.
DATABASES: dict[str, dict[str, Any]] = {"default": env.db("DATABASE_URL")}
DATABASES["default"]["CONN_MAX_AGE"] = env.int("CONN_MAX_AGE", default=60)
DATABASES["default"]["ATOMIC_REQUESTS"] = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Declared before the first migration exists. Swapping it later is a known trap, and
# T-016 configures allauth for email-only login, which auth.User cannot do without
# auto-generating throwaway usernames.
AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": (
            "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
        ),
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "pt-br"
TIME_ZONE = "America/Sao_Paulo"
USE_TZ = True
USE_I18N = True
DECIMAL_SEPARATOR = ","
THOUSAND_SEPARATOR = "."
LOCALE_PATHS = [BASE_DIR / "locale"]

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# HOST-ONLY cookies. `None` is load-bearing: a parent-domain cookie would let any
# attacker-controlled *.example.com origin set a sessionid for the whole domain, and
# RFC 6265 gives Django no way to tell which host set it.
SESSION_COOKIE_DOMAIN = None
CSRF_COOKIE_DOMAIN = None
SESSION_COOKIE_HTTPONLY = True
# The CSRF token is read from {% csrf_token %}, never from JavaScript, so the cookie
# can be HttpOnly. This blocks token theft from a sibling-subdomain XSS.
CSRF_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_PATH = "/"
CSRF_COOKIE_PATH = "/"

X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

REDIS_URL = env.str("REDIS_URL", default="redis://localhost:6379/0")

CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
# Sweeps in this product are idempotent fiscal jobs. Losing one silently is worse than
# running it twice, so a task is acknowledged only after it completes and is requeued
# when a worker dies mid-flight.
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_TASK_TIME_LIMIT = env.int("CELERY_TASK_TIME_LIMIT", default=600)
CELERY_TASK_SOFT_TIME_LIMIT = env.int("CELERY_TASK_SOFT_TIME_LIMIT", default=540)
# Store UTC, schedule in Brazilian local time.
CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = True

LOGGING: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "root": {"handlers": ["console"], "level": env.str("LOG_LEVEL", default="INFO")},
}
