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
from celery.schedules import crontab

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
    "allauth",
    "allauth.account",
    "allauth.mfa",
    "apps.core",
    "apps.accounts",
    "apps.clients",
    "apps.fiscal",
    "apps.lgpd",
    "apps.obligations",
    "apps.audit",
    "apps.portal",
    "apps.authz",
    "apps.security",
    "apps.tenants",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Registered this far out so its RESPONSE phase runs near the end of the chain and
    # therefore covers responses produced by inner middleware too — the 429 from the
    # rate limiter and the 403 from require_can included.
    "apps.security.csp.ContentSecurityPolicyMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # allauth refuses to start without this one, and it must follow authentication.
    "allauth.account.middleware.AccountMiddleware",
    # Before TenantMiddleware, so buffered platform events are written after that
    # transaction resolves. A login failure is recorded by a request whose own
    # writes are rolled back, and the audit row has to outlive them.
    "apps.audit.middleware.PlatformEventMiddleware",
    # Also before TenantMiddleware, and for the same reason turned the other way:
    # inside that transaction, the rollback on a failed request would erase the
    # Marco Civil access record for exactly the request an investigation wants.
    "apps.audit.middleware.AccessLogMiddleware",
    # Before TenantMiddleware: whether an account must carry a second factor is a
    # property of the account, not of the firm it is visiting, so it is settled
    # without resolving a tenant or opening the tenant transaction.
    "apps.accounts.middleware.MFAEnforcementMiddleware",
    # Immediately after MFAEnforcementMiddleware, and the three that follow are one
    # ordered group enforced at startup by core.E005/E006/E007.
    #
    # The dispatcher precedes PortalMiddleware because _is_non_atomic in both portal
    # and tenant middleware resolves against request.urlconf; a dispatcher running
    # later would have them decide transaction behaviour from ROOT_URLCONF.
    "apps.portal.middleware.HostDispatchMiddleware",
    # INSIDE MFAEnforcementMiddleware, never outside it. Outside, _must_enrol runs
    # within the portal transaction, where requires_mfa() reads tenants_membership and
    # is_mfa_enabled() reads mfa_authenticator — app_portal holds SELECT on neither, so
    # every authenticated portal request would die with permission denied. Being inside
    # SessionMiddleware matters for the same reason: django_session is written in its
    # response phase, after COMMIT, in autocommit as app_runtime.
    "apps.portal.middleware.PortalMiddleware",
    # After AuthenticationMiddleware: tenant resolution reads request.user to check
    # membership, and running earlier would make that check pass for everyone.
    # After PortalMiddleware, which no-ops it on portal hosts.
    "apps.tenants.middleware.TenantMiddleware",
    # After TenantMiddleware: the authenticated limits are keyed on
    # (tenant_id, user_id), and the tenant is not resolved before then. The
    # credential limits deliberately ignore the tenant — see apps.security.
    "apps.security.middleware.RateLimitMiddleware",
    # Inner, so its tenant_context nests inside the one TenantMiddleware opened
    # with an empty GUC on the platform host. It spans get_response, and therefore
    # the template rendering Django performs inside it — wrapping only
    # ModelAdmin.get_queryset would evaluate the lazy changelist after the block
    # exited and render an empty page instead of raising.
    "apps.tenants.admin_middleware.AdminTenantMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# The console lives here and nowhere else: AdminTenantMiddleware 404s it on any
# firm's subdomain, where it would inherit that firm's cookie scope.
ADMIN_PATH_PREFIX = "/admin/"

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

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

# The origin invitation and data-subject links point at. Those flows are platform
# level, not firm level: an invitee has no membership yet, so a link on the firm's
# own subdomain would be refused by TenantMiddleware for exactly its recipient.
# How many reverse proxies this deployment actually controls. 0 means X-Forwarded-For
# is not trusted at all, which is the safe default: a spoofable client address turns
# every per-IP control into decoration.
TRUSTED_PROXY_COUNT = env.int("TRUSTED_PROXY_COUNT", default=0)

PLATFORM_URL = env.str("PLATFORM_URL", default="http://localhost:8000")
DEFAULT_FROM_EMAIL = env.str("DEFAULT_FROM_EMAIL", default="nao-responda@localhost")
# The named encarregado (DPO) required by LGPD art. 41. docs/lgpd.md carries the
# name and the escalation path; this is where rights requests are delivered.
LGPD_ENCARREGADO_EMAIL = env.str(
    "LGPD_ENCARREGADO_EMAIL", default="encarregado@localhost"
)
SERVER_EMAIL = DEFAULT_FROM_EMAIL

LOGIN_URL = "account_login"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"

# Email is the only identifier: accounts.User has no username column, so any other
# login method would authenticate against a field that does not exist.
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
# Left at allauth's "username" default, this raises AttributeError the first time any
# allauth template renders a user's display name.
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_USER_MODEL_EMAIL_FIELD = "email"
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
ACCOUNT_UNIQUE_EMAIL = True
# Keeps signup, password reset and login from answering "does this address exist
# here?" — which would enumerate the client list of every firm on the platform.
ACCOUNT_PREVENT_ENUMERATION = True
ACCOUNT_SESSION_REMEMBER = False
ACCOUNT_LOGOUT_ON_PASSWORD_CHANGE = True

# WebAuthn is out of scope for this phase. Recovery codes are on so that losing a
# phone is a support conversation rather than an account loss.
MFA_SUPPORTED_TYPES = ["totp", "recovery_codes"]
MFA_TOTP_ISSUER = env.str("MFA_TOTP_ISSUER", default="app-mei")

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
# `static/` holds the build output — the compiled Tailwind stylesheet and the vendored
# HTMX and Alpine bundles — and belongs to no app, so it needs an explicit source
# entry. `staticfiles/` above is collectstatic's destination and must stay separate.
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# Annotated because prod.py replaces the default backend with one that carries an
# OPTIONS mapping; inferred from this literal alone the value type would be str.
STORAGES: dict[str, dict[str, Any]] = {
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

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    },
}

# Read from settings so a deployment can tighten them without a code change, and
# so a test can assert the shipped values rather than a literal copied into one.
#
# The credential limits are keyed on email and IP and NEVER on tenant: credentials
# are platform-global, so a tenant-keyed bucket is a Host-header bypass.
RATELIMIT_LOGIN_EMAIL = env.str("RATELIMIT_LOGIN_EMAIL", default="5/m")
RATELIMIT_LOGIN_IP = env.str("RATELIMIT_LOGIN_IP", default="20/m")
RATELIMIT_WRITE = env.str("RATELIMIT_WRITE", default="60/m")
RATELIMIT_READ = env.str("RATELIMIT_READ", default="120/m")
# Unauthenticated POST endpoints that must not be floodable. Two kinds live here:
# credential forms, and the LGPD intake — which sends mail to the encarregado on
# every submission and would otherwise be an amplification vector.
RATELIMIT_PUBLIC_POST_URL_NAMES = [
    "account_login",
    "account_reset_password",
    "account_reset_password_from_key",
    "mfa_authenticate",
    "dsr-submit",
]


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

# Six months, per the Marco Civil access-log duty. A ceiling, not a floor: keeping
# longer converts a compliance obligation into a standing liability.
ACCESS_LOG_RETENTION_DAYS = env.int("ACCESS_LOG_RETENTION_DAYS", default=180)

# Infrastructure traffic, not a person reaching personal data. The liveness probe
# in particular is declared non_atomic_requests so a database blip cannot restart a
# healthy process, and logging it would put that dependency straight back.
#
# MEDIA_URL is NOT here, and its absence is the control. For a fiscal document vault,
# downloads are precisely the events an investigation asks for -- who fetched which
# client's evidence, and when -- so exempting the media prefix would have excluded the
# Marco Civil access log's most important rows by construction, silently, before the
# vault existed to notice. Nothing serves /media/ either, asserted separately: the two
# together mean the only route to a document's bytes is the portal view, which logs.
ACCESS_LOG_EXEMPT_PREFIXES = ["/healthz", STATIC_URL]

# V3. The portal serves document bytes from memory, never as a StreamingHttpResponse:
# PortalMiddleware refuses one, because its iterator would be consumed after the role
# and
# both GUCs are gone and would yield nothing. Buffering makes the cap load-bearing.
#
# The arithmetic, because the box is the binding constraint rather than a formality. The
# web container is limited to 400 MiB (docker-compose.prod.yml), NOT the host's 954 MiB,
# and gunicorn runs gthread with 2 workers x 2 threads = 4 concurrent slots. So the
# worst
# case is 4 x 10 MiB = 40 MiB, about a tenth of the limit that actually kills the
# process
# -- and an in-memory response can hold the payload twice, fetch buffer plus response
# body, while a slow client drains it. A MEI's DAS PDF is around 100 KB, so this bounds
# the pathological case rather than the normal one.
PORTAL_DOCUMENT_MAX_BYTES = 10 * 1024 * 1024

# The ingest cap. Equal to the download cap DELIBERATELY: a document larger than what
# can
# be served would be stored and then permanently unreadable, which fails success
# criterion
# 1 silently. Django's own defaults bound only what is held in memory before spooling to
# disk, not the total, so without this an authenticated portal user can fill the
# container's filesystem -- the one direction untrusted callers fully control.
PORTAL_UPLOAD_MAX_BYTES = PORTAL_DOCUMENT_MAX_BYTES

CELERY_BEAT_SCHEDULE: dict[str, Any] = {
    "purge-access-logs": {
        "task": "apps.audit.tasks.purge_access_logs",
        "schedule": crontab(hour=3, minute=30),
    },
    # Every five minutes, and /healthz alarms after fifteen. The scheduler's failure
    # mode is silence: when beat stops, no task raises and nothing reaches Sentry,
    # so the only way to detect it is to require a positive signal on a timer.
    # The literal is 300 seconds rather than an import: importing
    # apps.obligations.heartbeat here would load a model before the app registry is
    # ready. A test asserts this equals HEARTBEAT_INTERVAL, so the two cannot drift.
    "scheduler-heartbeat": {
        "task": "apps.obligations.tasks.record_scheduler_heartbeat",
        "schedule": 300.0,
    },
    # Before dawn in Brazil, so a newly entered competence month is already on the
    # queue when the first accountant opens the dashboard.
    "refresh-das-calendars": {
        "task": "apps.obligations.tasks.refresh_das_calendars",
        "schedule": crontab(hour=4, minute=0),
    },
}

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
