"""Invitation routes.

Mounted at the platform level: see the module docstring in `views.py` for why
acceptance cannot live on a firm's subdomain.
"""

from django.urls import path

from apps.accounts.views import (
    accept_invite_view,
    invite_issued_view,
    invite_revoke_view,
    issue_invite_view,
    member_deactivate_view,
    member_reactivate_view,
    team_view,
)

urlpatterns = [
    path("equipe/", team_view, name="team"),
    path(
        "equipe/<uuid:pk>/desativar/",
        member_deactivate_view,
        name="member-deactivate",
    ),
    path(
        "equipe/<uuid:pk>/reativar/",
        member_reactivate_view,
        name="member-reactivate",
    ),
    path("convites/", issue_invite_view, name="invite-issue"),
    path("convites/enviado/", invite_issued_view, name="invite-issued"),
    path("convites/<uuid:pk>/revogar/", invite_revoke_view, name="invite-revoke"),
    path("convites/aceitar/<str:token>/", accept_invite_view, name="invite-accept"),
]
