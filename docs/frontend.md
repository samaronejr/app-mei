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
