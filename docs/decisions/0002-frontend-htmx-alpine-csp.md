# 0002: Frontend stays server-rendered with HTMX and the CSP build of Alpine

- **Status**: Accepted
- **Date**: 2026-09-27
- **Gate**: AXR1-001 of the local-only plan `axorim-pivot-r1` (see `docs/project-status.md`)
- **Applies to**: every firm-side and portal screen added by the R1 release

## Question

R1 adds much denser screens than the product has today: a daily exception list, a
client workspace per period, document review, a playbook editor, approval cards and a
mobile-friendly upload list in the portal. Can the current server-rendered stack carry
them, or does R1 need a single-page application?

## What the stack is today

| Piece | Version or rule | Where it is pinned or checked |
| --- | --- | --- |
| Rendering | Django templates | `templates/base.html` |
| Partial updates | HTMX 2.0.10, `allowEval: false` | `package.json`, `templates/base.html:13` |
| Local state | `@alpinejs/csp` 3.15.12 | `package.json` |
| Styling | Tailwind 4.3.3, tokens in `assets/css/app.css` | `package.json` |
| Built assets | committed under `static/`, rebuilt by `npm run build` | `tests/ui/test_asset_pipeline.py` |
| Content Security Policy | self-only, no eval, no inline script | `apps/security/csp.py`, `tests/ui/test_csp.py` |

## Decision

Keep it. New screens are Django templates built from shared partials under
`templates/partials/ui/` (table, filter bar, status badge, drawer, dialog, form field,
upload list, approval card, timeline item, empty, error and stale states). HTMX handles
partial updates. Alpine, CSP build only, holds local UI state through `Alpine.data`
components registered from `static/js/`.

The rules that come with the decision:

- No inline scripts, no `hx-on:*` attributes, and no user-supplied strings inside
  `x-*` or `hx-*` attributes. The existing CSP guard keeps enforcing this.
- Focus is restored after HTMX swaps by handlers on `htmx:afterSwap` in
  `static/js/app.js`, since `hx-on` is off the table.
- Server validation is the authority. Forms that edit versioned rows carry the version,
  and a stale save gets HTTP 409 with a prompt to reload.
- `hx-disabled-elt` guards every submit that has a side effect, so a double click
  can't send twice.
- Any change to templates, CSS or JS ships with `npm run build` output committed in
  the same PR.

Typed client-side islands are allowed only when a prototype shows an interaction that
HTMX and Alpine can't express. Each island needs its own ADR, and it must pass the same
CSP without exceptions.

## Alternatives considered

| Option | Why it was not taken |
| --- | --- |
| Rewrite as a single-page app (React, Vue or similar) over a JSON API | Rebuilds every screen and duplicates the tenant, portal and permission boundaries in a second codebase. The new API surface would need its own isolation tests, and the build tooling pushes toward a looser CSP. Nothing in R1 needs it. |
| Standard (non-CSP) Alpine build | Requires `unsafe-eval`. The CSP is never weakened. |
| Typed islands from day one | Premature. The R1 screens, built behind the `cycles_r1` feature flag on a synthetic-data tenant, are themselves the prototypes that will show whether an island is needed. |

## Consequences

- The R1 screens double as prototypes. They're exercised at 1440x900 and 390x844 on
  synthetic data before any real firm sees them. Usability sessions with accounting
  firms are an open operator gate, and no result is assumed before they run.
- Runtime checks are needed on top of today's static template scans. A browser test
  job (Playwright plus axe-core, run at both viewports) is planned for the todo that
  adds the new product shell. It's described in words here because it doesn't exist
  yet.
- The accessibility target is WCAG 2.2 AA, including focus not obscured, targets of at
  least 24 by 24 CSS pixels, table semantics and live regions for status changes.
  Automated scores never claim compliance on their own; manual screen-reader journeys
  are recorded alongside them.
- Revisit this ADR if a prototype fails its usability bar because of an interaction the
  stack can't express. That evidence, not preference, reopens the SPA question.
