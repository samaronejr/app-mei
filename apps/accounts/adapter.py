"""Account policy for an invitation-only product."""

from allauth.account.adapter import DefaultAccountAdapter
from django.http import HttpRequest


class InvitationOnlyAccountAdapter(DefaultAccountAdapter):  # type: ignore[misc]
    """Keep allauth account management while refusing standalone registration."""

    def is_open_for_signup(self, request: HttpRequest) -> bool:
        """Return false because invitations create their accounts directly."""
        del request
        return False
