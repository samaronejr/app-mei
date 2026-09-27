# 0007: The client portal is responsive web, with no PWA or offline cache

- **Status**: Accepted
- **Date**: 2026-09-27
- **Gate**: AXR1-001 of the local-only plan `axorim-pivot-r1` (see `docs/project-status.md`)
- **Applies to**: the client portal host and every portal screen added in R1

## Question

In R1 clients answer document requests from their phones. Should the portal become an
installable progressive web app with a service worker and offline cache, or should the
firm get a store app, so uploads and status survive poor connections?

## Decision

The portal stays a responsive, firm-branded web application with no service worker, no
offline cache and no store app in R1.

- No service worker is registered, and no portal response is written to Cache Storage
  or IndexedDB.
- Uploads are per request, with visible progress, cancel and retry. Progress comes from
  the `htmx:xhr:progress` event bound in an `Alpine.data` component. A failed or
  offline upload keeps the list of selected files on screen and offers "Tentar
  novamente"; nothing is queued in the background.
- Duplicates are caught on the server by hash and explained in plain Portuguese, for
  example "Este arquivo já foi enviado em 03/04 e foi vinculado a esta solicitação".
- Email never attaches sensitive documents. It carries an authenticated link back to
  the portal.
- The portal stays on its own host with its own database role. Today it pins a user to
  their first membership (`apps/portal/middleware.py:235-258`); R1 replaces that with
  an explicit company choice. The client is never inferred from upload data or
  filenames.

## Alternatives considered

| Option | Why it was not taken |
| --- | --- |
| PWA with offline cache | Portal pages hold fiscal documents and personal data. A cache on a shared or lost phone outlives logout and membership revocation, and cached pages can show a stale status as current, which is the exact failure R1 is meant to remove. A service worker also adds a script origin the CSP must admit. |
| Background upload queue (service worker plus background sync) | Moves the retry out of the user's sight. For documents that decide a monthly cycle, the client should see that an upload failed and retry it deliberately. |
| White-label store app per firm | Store accounts, review cycles and update distribution per firm. Out of scope for R1; it can be reconsidered once real usage data exists. |

## Consequences

- A client with a bad connection sees a clear failure and a retry button instead of a
  silent queue.
- Revoking a portal membership takes effect on the next request, with no cached copy
  left on the device.
- A test that fails if a service worker registration or an offline cache appears in
  portal templates or scripts is planned with the first R1 portal change. It's
  described in words here because it doesn't exist yet.
- Revisit if usability sessions show mobile upload completion below the release bar
  for reasons a cache would fix and a retry button wouldn't.
