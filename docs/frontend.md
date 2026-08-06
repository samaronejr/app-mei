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

## Icons

Sixteen Lucide glyphs are committed as inline-SVG partials under
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

Six of the sixteen (`alert-triangle`, `chevron-right`, `download`, `info`, `upload`,
`x-circle`) are Feather-derived, so the MIT notice covers them as well as the ISC
licence. Both are reproduced in full; shipping only the ISC half would under-attribute.

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
