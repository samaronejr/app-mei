# Provider transport contract (AXRM-011)

`ProviderTransport.send(operation, ProviderRequest(...))` is the provider-only
wire boundary. It does not register an admission policy, reserve money, retry,
enable a feature or unpause a provider.

Transmission is bound to the AXRM-009 protocol in code. `begin_io()` records the
dispatch it authorized and journaled; `transmit()` accepts only that exact,
unconsumed dispatch (anything else raises `StaleClaimError`) and, while its
adapter runs, opens a window for that call's provider, environment and operation.
`send()` consumes the window before any connection: no window, a window for
another call, or a second send in the same window raises `EnvioNaoAutorizado`
(code `envio_nao_autorizado`, a subclass of `OperacaoNaoPermitida`) with nothing
sent. A direct call from any other code path therefore cannot reach a provider,
whatever its environment, operation class or pause settings. The window lives in a
context variable, so it does not leak into other threads or later calls. The
firm-level `SP-TERMO` registration has no call row, so it cannot be sent until
AXRM-013 adds its own authorized, journaled phase that opens a window. A6's
"401 -> re-authenticate once" resend likewise needs AXRM-013 to grant it
explicitly; today one dispatch carries exactly one transmission.

The catalog chooses the HTTP method and path. Callers select an A6 operation and,
where needed, a route variant: `xml` or `cce` for `FC-NFE`, `xml` for `FC-NFS`,
`nfse` for `FC-PDF`, and `create`, `read` or `update` for `FC-EMP`.
SERPRO bodies must name that operation's exact system/service/version; the
manifestation route accepts only `{"tipo":"ciencia"}`. Configuration rejects
issuance switches, certificate material and unqualified automatic manifestation.
Unknown operations and arbitrary URLs are not supported.

Requests 2.34.2 was already locked as a development dependency; it is now an
explicit runtime dependency at that same version. A prepared request sent through
its adapter avoids Session cookie, netrc, proxy and redirect preparation. TLS
certificate and hostname verification cannot be disabled through this interface.
An optional CA bundle changes trust roots, not verification. Production/trial
origins are fixed. The explicit `fake` environment accepts only an ephemeral
`127.0.0.1` HTTP/HTTPS origin, with no credentials, query or path.

Each request has a 5-second connect timeout and 30-second read timeout
(60 seconds for `SP-SIT-R`). The body has the same elapsed budget: before each
single socket read the socket timeout is lowered to what remains, so a peer
dripping bytes cannot extend the read by one socket timeout per byte. A real-wire
regression sends bytes at 0, 20 and 40 s against the unmodified 30 s budget and
requires `tempo_limite` between 29 and 31 s (before the fix it took 40 s). JSON/XML responses are limited to 5 MiB; direct PDF
responses to 10 MiB. Compressed responses are refused before decompression.
Provider adapters own format/schema validation and storage after this byte limit.
No request or response body is logged.

Only `FC-PDF` can follow a 302, once, to the exact documented
`focusnfe.s3.sa-east-1.amazonaws.com` HTTPS host. The second request contains no
provider credentials, cookies or Referer. Location URLs remain ephemeral.
Any additional host requires qualification and a reviewed catalog change.
SERPRO 304 is returned as a protocol status rather than treated as a redirect.

## Diagnostics

The production console filter redacts log arguments (and non-string messages)
while their structure still exists: values under provider field names
(`Authorization`, `jwt_token`, `X-*Token*`, `body`, `data`, `payload`, ...)
and header-style `(name, value)` pairs become `[Filtered]`, and those values are
remembered. Only then is the message interpolated; the rendered text, stack and
the entire exception chain (causes, contexts and notes, re-rendered from
redacted arguments) are scrubbed again with the active credential values registered for
the current call and every remembered value. Text-level rules also cover rendered
pairs such as `('Authorization', '...')`, bare `Bearer` tokens, quoted body
fields and presigned query credentials (`X-Amz-*`, `Signature`, `AWSAccessKeyId`)
even without a scheme. While a wire call runs, its full URL, path-and-query and
query string are active secrets, so urllib3's DEBUG request line
(`"GET /file?... HTTP/1.1"`) is replaced whole; known values are replaced before
any pattern rule so a partial rewrite cannot defeat the match. The Sentry `before_send` hook applies the same pair rule to event lists.

`TransportFailure.result()` produces a minimized protocol result: unreadable
responses with a known status become `falhou`; connection/time-out ambiguity
becomes `incerta`. Billing stays unknown. The adapter records and finalizes that
result through AXRM-009; it never repeats an ambiguous registration or issuance.

## Broker client

`TokenBrokerClient(secret).tokens(firm_cnpj, refresh=False)` sends one
`POST http://serpro-broker:8080/token` on the internal service network. The body is
UTF-8 JSON with sorted keys and compact separators:
`{"firm_cnpj":"00000000000000","refresh":false}`.
`X-Broker-Token-HMAC` is the lowercase hex HMAC-SHA256 of those exact bytes using
the shared deployment secret. The 64-KiB response must contain `access_token`,
`jwt_token`, positive integer `expires_in`, and optionally
`autenticar_procurador_token`. Values live only in the calling process for that
call; there is no worker cache or authentication retry.

`tokens()` also requires an open SERPRO dispatch window, so per-call tokens are
fetched only for an authorized SERPRO call.

The broker service, its HMAC verification, certificate secrets and lifecycle are
AXRM-013 work. `SP-AUTH` cannot go through the provider transport, so workers cannot
call SAPI authentication or handle a contractor certificate.

## Testing and recovery

The scripted peers in `tests/fakes/serpro.py` and `tests/fakes/focus.py` use
ephemeral ports and per-instance request logs. `Reply` supplies status, body,
headers, chunks, reset and event-held latency; `gap` and `hold_open` exist only
for tests whose behaviour under test is elapsed time. Wire-level tests that have
no call row use `tests.fakes.provider.permitted_send`, which opens the same
single-send window as `transmit()`; a static test asserts that no application
module other than `call_dispatch` touches that window. TLS tests generate a temporary test
certificate; DNS maps only the documented storage host to a local test server.
No test contacts a real provider.

There are no tables or migrations. Deployment remains inert until an adapter is
connected to the existing closed admission controls. Recovery is a code revert;
no external state or persisted provider data is changed by this foundation.
