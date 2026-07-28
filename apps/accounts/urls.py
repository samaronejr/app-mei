"""Invitation routes.

Mounted at the platform level: see the module docstring in `views.py` for why
acceptance cannot live on a firm's subdomain.
"""

from django.urls import path

from apps.accounts.views import (
    accept_invite_view,
    invite_issued_view,
    issue_invite_view,
    team_view,
)

urlpatterns = [
    path("equipe/", team_view, name="team"),
    path("convites/", issue_invite_view, name="invite-issue"),
    path("convites/enviado/", invite_issued_view, name="invite-issued"),
    path("convites/aceitar/<str:token>/", accept_invite_view, name="invite-accept"),
]
