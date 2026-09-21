# CLIENT REGISTRY

## OVERVIEW
Firm-owned MEI records, assignment, readiness, and onboarding; score 12, distinct registry domain.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Registry fields/constraints | `models/company.py` | `ClientCompany`, status/trust choices |
| Staff portfolio assignment | `models/assignment.py`, `services.py` | Assign/unassign lifecycle |
| Onboarding items/templates | `models/onboarding.py`, `services.py` | Default checklist creation |
| Tags | `models/tagging.py` | Tag and client-tag records |
| Public model imports | `models/__init__.py` | Re-export boundary |
| Scoped query behavior | `managers.py` | `ClientCompanyManager` |
| Readiness/e-CAC blocker | `readiness.py` | Domain-derived state |
| CSV round trip | `exports.py` | Build/parse helpers |
| Registry pages/export | `views.py`, `urls.py` | HTML lives in `templates/clients/` |

## CONVENTIONS
- CNPJ uniqueness is per tenant: two firms may legitimately serve the same MEI.
- `(tenant, id)` uniqueness exists to support composite child foreign keys.
- Document values normalize in both `clean_fields()` and `save()`.
- Reuse fiscal normalization for storage, search, comparison, and export.
- CNPJ storage accepts uppercase alphanumeric identifiers, not digits only.
- Shape constraints use varchar plus CHECK, not space-padding fixed-width char.
- CPF absence is null; the database rejects the empty-string alternative.
- Recorded gov.br trust is user-supplied metadata, not verified OIDC identity.
- Unknown gov.br trust blocks e-CAC like bronze, rather than assuming eligibility.
- Model implementation is split; consumers import from `apps.clients.models`.

## ANTI-PATTERNS
- Do not replace per-tenant CNPJ uniqueness with a global unique index.
- Do not remove the seemingly redundant `(tenant, id)` unique constraint.
- Do not rely on an ordinary client FK to prevent cross-tenant references.
- Do not store certificate bytes/private keys in the client registry.
- Do not describe recorded gov.br trust as externally verified authentication.

## RELATED CHECKS
- `uv run pytest tests/clients tests/ui/test_clients_pages.py`
- Identifier persistence/export behavior also lives in `tests/fiscal/`.
- Cross-tenant relationship constraints are covered in `tests/isolation/`.
