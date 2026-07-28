"""Queue routes.

Firm-scoped: every view here resolves `request.tenant` and 404s without one.
"""

from django.urls import path

from apps.obligations.views import (
    queue_counts_view,
    queue_due_soon,
    queue_onboarding,
    queue_overdue,
    queue_threshold,
)

urlpatterns = [
    path("filas/vencendo/", queue_due_soon, name="queue-due-soon"),
    path("filas/atrasadas/", queue_overdue, name="queue-overdue"),
    path("filas/onboarding/", queue_onboarding, name="queue-onboarding"),
    path("filas/limite/", queue_threshold, name="queue-threshold"),
    path("filas/contagens/", queue_counts_view, name="queue-counts"),
]
