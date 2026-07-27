"""Celery application.

Tasks run outside the request cycle, so they are never covered by the tenant
middleware. Tenant context for workers is set explicitly in T-013 — do not assume
middleware applies here.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("app_mei")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
