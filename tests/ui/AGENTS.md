# RENDERED UI AND ASSET CONTRACTS

## OVERVIEW
Firm-page integration plus self-testing template scanners; score 11, distinct UI verification domain.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Firm/users/clients fixtures | `factories.py` | `Firm`, make/add helpers, assignments |
| Template discovery/tokenization | `templates_scan.py` | Root and app template directories |
| Accessible control names | `a11y_scan.py` | Icon-bearing control analysis |
| Responsive structure | `responsive_scan.py` | Width and enclosure checks |
| Compiled assets | `test_asset_pipeline.py` | Shipped CSS/vendor contracts |
| Registry screen | `test_clients_pages.py` | Search, portfolio, isolation |
| Fiscal queues | `test_queue_views.py` | Authorization and query behavior |
| MFA presentation/flow | `test_mfa_pages.py` | Real allauth sessions |
| Localized presentation | `test_ptbr_presentation.py` | Brazilian dates/documents/values |
| Page query budgets | Page-specific modules | Query count and N+1 regressions |

## CONVENTIONS
- `Firm.sign_in` posts real password login and then TOTP for the firm's host.
- Fixtures enroll every firm-side account before exercising protected pages.
- Direct session login omits allauth authentication age and invalidates sensitive-action tests.
- `client_base` includes the firm slug when constructing deterministic CNPJ fixtures.
- Different firms must not accidentally receive identical fixture documents in leak assertions.
- `add_member` requires the appropriate client scope for client-side membership roles.
- Template discovery includes app templates; portal HTML must not escape structural scans.
- Accessibility/responsive scanners prove their detector with planted failing and passing controls.
- Review-width constants belong to the responsive scanner, not browser timing assumptions.
- Page contract constants are spelled out rather than imported from the view under test.
- Query-count assertions catch regressions that a correct rendered page alone cannot expose.

## ANTI-PATTERNS
- Do not use force_login as a substitute for the allauth/MFA flow these tests assert.
- Do not reuse one firm's document values in another firm's isolation fixtures by accident.
- Do not let a scanner discover zero templates and call the application clean.
- Do not make a view rename automatically rewrite its test contract by importing that constant.
- Do not infer actual browser layout from source scans alone; they cover structural contracts.

## RELATED CHECKS
- `uv run pytest tests/ui`
- Template/theme changes require `npm run build` before asset-pipeline verification.
- `uv run pytest tests/ui/test_asset_pipeline.py` validates generated assets against the source surface.
