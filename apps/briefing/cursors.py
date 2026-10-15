"""Signed keyset boundaries bind to a person, tenant and filter selection."""

from dataclasses import asdict
from datetime import date
from hashlib import sha256
from json import dumps

from django.core import signing
from django.core.exceptions import ValidationError

from apps.accounts.models import User
from apps.briefing.types import BriefingFilters, BriefingItem, BriefingScope
from apps.cycles.services.cycle import tenant_id

CURSOR_SALT = "briefing.rules-v1"


def selection_key(actor: User, scope: BriefingScope, filters: BriefingFilters) -> str:
    """Bind a cursor to this selection without saving authority."""
    value = [str(tenant_id()), str(actor.pk), scope, asdict(filters)]
    return sha256(dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def cursor_for(item: BriefingItem, selection: str) -> str:
    """Encode the last rendered total-order key."""
    priority, due_on, identifier = item.order_key
    return signing.dumps(
        [selection, priority, due_on.isoformat(), identifier], salt=CURSOR_SALT
    )


def cursor_key(cursor: str, selection: str) -> tuple[int, date, str]:
    """Refuse malformed, forged or differently scoped positions."""
    message = "A página mudou ou o cursor é inválido. Recomece a lista."
    try:
        raw = signing.loads(cursor, salt=CURSOR_SALT)
        match raw:
            case [str(binding), int(priority), str(due), str(identifier)]:
                if binding != selection:
                    raise ValidationError(message)
                return priority, date.fromisoformat(due), identifier
            case _:
                raise ValidationError(message)
    except (signing.BadSignature, ValueError) as error:
        raise ValidationError(message) from error
