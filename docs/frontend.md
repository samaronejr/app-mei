# Front-end assets

## Why the build output is committed

`static/css/app.css` and `static/js/*.min.js` are build artifacts, and they are in
version control on purpose. Neither the Docker image nor CI installs Node, so the
deploy path has no step that could produce them. Committing them means a release
cannot fail because npm had a bad day, and it makes the exact bytes that will be
served reviewable in a diff.

The cost is that the build must be re-run whenever a template gains a class Tailwind
has not already emitted. That is not left to memory:
`tests/ui/test_asset_pipeline.py::test_compiled_stylesheet_covers_every_class_the_templates_use`
scans every template and fails when a class has no compiled rule. A stale stylesheet
is otherwise invisible — the page renders, just unstyled.

```sh
npm install     # once
npm run build   # after any template or theme change, before committing
```

## Why Alpine is `@alpinejs/csp`

The ordinary Alpine distribution compiles every `x-*` expression with `new Function`.
Shipping it would require `script-src 'unsafe-eval'`, and that turns every attribute
in the page into a potential script. `@alpinejs/csp` evaluates a restricted expression
grammar instead and contains no evaluator at all —
`test_the_vendored_alpine_is_the_csp_build` asserts exactly that, because swapping the
package back would keep every other test green.

The consequence for template authors: Alpine's CSP build only accepts **property and
method references**, not arbitrary JavaScript. Component state goes in
`Alpine.data(...)` inside a static file, and `x-data` names it.

## Why nothing is interpolated into `x-*` or `hx-*`

Django autoescaping renders `'` as `&#x27;`, which is safe in text and in ordinary
attributes. It is **not** safe in an attribute that Alpine or HTMX later evaluates,
because the browser decodes the entity before the library ever sees the string — so a
tenant's `legal_name` interpolated into `x-data` escapes into the expression.

Pass tenant data through `data-*` attributes and read it with `$el.dataset`.
`test_no_tenant_value_is_interpolated_into_a_scripted_attribute` enforces this.

## Why HTMX is configured through a meta tag

Two of HTMX's defaults are incompatible with the policy, and both are turned off
declaratively in `templates/base.html` rather than by an inline script (which the
policy also forbids):

| Setting | Default | Here | Reason |
| --- | --- | --- | --- |
| `allowEval` | `true` | `false` | Removes the one `new Function` call HTMX keeps, for `hx-on:` handlers. |
| `includeIndicatorStyles` | `true` | `false` | HTMX would inject an inline `<style>`; `style-src 'self'` blocks it. The rules ship in `app.css`. |

## The design system

`assets/css/app.css` is the configuration — Tailwind v4 has no `tailwind.config.js`.
Colours, radii, shadows and fonts are declared in `@theme` and are the only way to
name them; a hard-coded hex in a template resolves to nothing. Status colours are
named after the domain (`ok`, `warn`, `over`, `late`) rather than after hues, so
retuning the palette cannot make a badge lie.

## The R1 product system

The shell and screens behind the `cycles_r1` flag use a second, strictly additive
layer of the same design system. Nothing declared for the MEI-era screens changes,
and Tailwind v4 emits a theme variable only where something uses it, so a flag-off
firm is served the page it was served before.

### Tokens (`assets/css/app.css`, second half of `@theme`)

| Token | Values | Rule |
| --- | --- | --- |
| Neutral | the existing `ink` ramp | One grey scale, not two. The shell raises `ink-500` to AA (5.5:1 on white) for its own pages through `.shell-r1`. |
| Brand ink | `brand-50..800`, ink `brand-700` = `oklch(0.42 0.08 200)` | 8.04:1 on white. Wordmark, primary action, active nav wash. |
| Evidence states | `state-{confirmed,manual,pending,unknown,stale,blocked,danger}` plus `-soft` and `-line` | Every ink is at least 5.9:1 on its own soft fill. Green is `confirmed` only. |
| Type | Tailwind `xs/sm/base/xl/2xl` plus `text-dense` (13px) | 12/13/14/16/20/24. Tabular numerals for money and dates. |
| Spacing | `--spacing: 0.25rem` | The 4px grid, stated. |
| Radius | `rounded-field` 4px, `rounded-panel` 6px | Controls, then surfaces. |
| Elevation | `shadow-overlay` | The only shadow: drawer, dialog, toast. Flat surfaces take a border. |
| Motion | `ease-standard`, at most 150ms | The reduced-motion block removes it. |

State is never colour alone: the status badge draws a hue, a glyph and a pt-BR word,
and the only coloured edge in the family is the keyboard focus ring.

### Partials (`templates/partials/ui/`)

`table`, `filter_bar`, `status_badge`, `drawer`, `dialog`, `field`, `upload_list`,
`effects_card`, `timeline_item`, `state_empty`, `state_error`, `state_stale`, `toast`,
plus `shell_header` for the flagged shell. Each documents its context in its own
header comment and renders from that context alone. `tests/ui/test_ui_partials.py`
pins the markup contracts; `tests/browser/test_components.py` drives the behaviour.

### Alpine components

`dialog` (native `<dialog>`, focus returned to the opener), `uploadList`
(preview, size and type refusals, removal), `unsavedGuard` (the browser's own
leave-page prompt) and `dismissable` (toasts) live in `static/js/app.js` and register
on `alpine:init`. That event fires once, when Alpine starts, so under the product
shell `base.html` loads Alpine *after* `app.js`; flag-off pages keep the old order and
carry no component. A control that needs script to act ships `hidden` and is revealed
by its component, so a page without script offers no dead button.

After any HTMX swap, focus moves to the element the server marked with
`data-focus-after-swap` (then the marker is removed), or to the swapped region when
nothing is marked.

### Browser tests

```sh
uv run playwright install chromium   # once; CI adds --with-deps
uv run pytest -m browser             # plain `uv run pytest` deselects these
BROWSER_SCREENSHOT_DIR=/tmp/shots uv run pytest -m browser   # also save screenshots
```

Every test runs at 1440x900 and 390x844 against a live server that runs as
`app_runtime`: `tests/browser/conftest.py` switches every off-main-thread
connection and a test-only middleware asserts `current_user` on every request.
`axe_check` fails on serious and critical violations; its positive control is a
planted control with no accessible name.

## Icons

Twenty Lucide glyphs are committed as inline-SVG partials under
`templates/partials/pure/icons/`, one file per glyph. Nothing is installed and nothing
is fetched: an inline `<svg>` is markup the parser has already consumed, so it needs no
CSP allowance, no widened `img-src`, and no entry in the vendored-JS manifest — which
stays JS-only, because `test_vendored_bundles_are_committed` resolves every entry as
`static/js/<file>`. `package.json` is untouched by design.

| | |
| --- | --- |
| Source | `https://github.com/lucide-icons/lucide` |
| Version | `1.28.0` — git tag, published 2026-07-30. Every glyph comes from this one release. |
| Licence | ISC, plus MIT for the Feather-derived glyphs — `static/icons/LICENSE-lucide.txt` |

Six of the first sixteen (`alert-triangle`, `chevron-right`, `download`, `info`,
`upload`, `x-circle`) are Feather-derived, so the MIT notice covers them as well as the
ISC licence. Both are reproduced in full; shipping only the ISC half would
under-attribute.

The four added for the R1 status badge and overlays -- `ban`, `circle-question-mark`,
`clock-fading` and `x` -- carry their Lucide 1.28.0 names. Upstream metadata credits
Feather's author on all four, so the MIT notice is taken to cover them too.

Four names here are names Lucide retired before its 1.0 release, so searching upstream
for them finds nothing. Same glyph, two names:

| This project | Lucide 1.28.0 |
| --- | --- |
| `alert-triangle` | `triangle-alert` |
| `check-circle` | `circle-check` |
| `home` | `house` |
| `x-circle` | `circle-x` |

### Decorative by default

Every partial carries `aria-hidden="true"` and `focusable="false"` on the root, so a
glyph is never announced beside a label that already says the word, and never becomes a
tab stop. Each also carries `class="icon"`, which is where size and colour come from — a
`1em` box and a `currentColor` stroke — so a glyph matches the text beside it instead of
the 24px it was drawn at. `tests/ui/test_icons.py` enforces those three, and the absence
of a script, an external origin and a `data:` URI.

An icon is never the only carrier of meaning. Status is always a token colour **and** a
pt-BR word, with the glyph as reinforcement; a monochrome print, a colour-blind reader
and a screen reader must all reach the same answer.

### An icon-only control REQUIRES its own name

Because the glyph is `aria-hidden`, a control containing nothing else has no accessible
name at all — it announces as "button" and is unusable. Give it visible text, or
`.visually-hidden` text where the design cannot afford it:

```html
<button type="button" class="btn btn-quiet">
  {% include 'partials/pure/icons/download.html' %}
  <span class="visually-hidden">Baixar DAS</span>
</button>
```

### Adding a glyph

Copy it from the same pinned release, keep the path data byte-identical, and add the
name to `EXPECTED_ICONS` in `tests/ui/test_icons.py` — the scan refuses to run against
a set that does not contain every name it expects, so a partial nobody registered and a
name nobody vendored both fail loudly. No rebuild is needed: every glyph reuses `.icon`
and introduces no new class.
