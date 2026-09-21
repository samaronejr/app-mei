# CLIENT PORTAL

## OVERVIEW
Host-dispatched, single-client request surface and document transfer; score 9, distinct restricted-role domain.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Portal host parsing | `hosts.py` | Distinct from ordinary firm slugs |
| URLconf selection | `middleware.py` | `HostDispatchMiddleware` |
| Role/client transaction | `middleware.py` | `PortalMiddleware` |
| Pages and document bytes | `views.py` | Home, payments, documents, account, upload/download |
| Invitation acceptance | `invite_views.py` | Client-seat acceptance adapter |
| Mounted account routes | `urls.py` | Portal URL tree includes allauth |
| Portal presentation | `templates/portal/` | App-local pages and partials |

## CONVENTIONS
- The client identity comes from the selected membership row, never request inputs.
- Assume `app_portal` inside the outer atomic block and positively verify the role.
- Tenant/client GUCs and ContextVars are established for the confined request.
- The middleware owns the outer transaction: role changes merge upward through savepoints.
- Render template responses before leaving that transaction.
- Reject streaming responses because iteration would escape the confinement lifetime.
- Account and invitation prefixes deliberately remain unconfined as `app_runtime`.
- Those flows need identity tables unavailable to the portal role and no client rows.
- Preserve the portal's own admin refusal; firm admin middleware does not replace it.
- Pre-role authorization data is held in the authz portal stash.
- Upload/download uses generated storage keys and document metadata, with a 10 MiB cap.
- Portal writes must surface conflicts rather than reveal invisible-row existence by suppression.

## ANTI-PATTERNS
- Never derive `app.client_id` from host, query parameters, headers, or session values.
- Never move SET LOCAL ROLE outside atomic or nest the portal transaction under another.
- Do not remove the account/invitation exemptions to make every route look uniform.
- Do not use ON CONFLICT, get_or_create, or ignore_conflicts in portal write paths.
- Do not serve media bytes through a path bypassing permission, RLS, and access logging.
- Do not return StreamingHttpResponse from portal views.

## RELATED CHECKS
- `uv run pytest tests/portal tests/isolation`
- Middleware integration requires `django_db(transaction=True)` to expose outer-transaction behavior.
- Provenance and conflict guards self-test their detectors before scanning this package.
