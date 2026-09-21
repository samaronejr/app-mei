# ACCOUNT IDENTITY AND INVITATIONS

## OVERVIEW
Email identity, MFA enforcement, and invitation lifecycle; score 9, distinct identity domain.

## WHERE TO LOOK
| Task | Location | Boundary |
|------|----------|----------|
| Email-only user | `models.py` | Swapped user model and manager |
| Invite issuance/redemption | `invites.py` | Service-layer identity checks |
| Team/member lifecycle | `views.py` | Last-owner and visibility rules |
| Invite input contracts | `forms.py` | Firm and client invitation forms |
| MFA redirect policy | `middleware.py`, `mfa.py` | Enrollment and exempt paths |
| Routes | `urls.py` | Named routes shared with templates/tests |
| Invitation persistence | `../tenants/models.py` | `Invite`, not an accounts model |
| Portal acceptance adapter | `../portal/invite_views.py` | Host/scope-specific acceptance |

## CONVENTIONS
- Invite possession alone is insufficient: redemption proves the invited mailbox.
- Existing-account acceptance checks a verified allauth email address.
- Invitation checks live in services, so non-view callers cannot bypass them.
- Firm and client invitations have distinct acceptance scopes and host checks.
- Host/scope mismatch errors deliberately inherit not-found semantics.
- View error mappings use exact error types; subclasses need explicit entries.
- `users.create` is the capability for roster administration, not a role-name list.
- Invitation events use platform audit so issuance and acceptance share one stream.
- Audit invitation metadata includes `client_id`, including null for a firm seat.
- Member deactivation protects the last active owner under locking.
- Invisible memberships are not-found; visible-but-forbidden operations are separate.

## ANTI-PATTERNS
- Never attach an invitation to whichever unrelated account happens to be signed in.
- Never log the raw bearer token; persistence keeps its digest only.
- Do not move invitation checks out of the locked acceptance path.
- Do not replace real allauth sign-in with session injection in MFA-sensitive tests.
- Do not allow self-deactivation or remove the last active owner.

## RELATED CHECKS
- `uv run pytest tests/accounts tests/ui/test_mfa_pages.py`
- Client invitation behavior also runs through `tests/portal/`.
- Team/confirmation page contracts live under `tests/ui/`, not this app.
