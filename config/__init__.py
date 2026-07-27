"""Project configuration package.

The Celery application is imported here so that ``@shared_task`` binds to it as soon
as Django starts, whichever entrypoint is used.
"""

from config.celery import app as celery_app

__all__ = ("celery_app",)
