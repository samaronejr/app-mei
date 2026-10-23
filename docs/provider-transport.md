# Provider transport contract (AXRM-011)

`ProviderTransport.send(operation, ProviderRequest(...))` is the provider-only
wire boundary. Adapters call it during the existing provider-call protocol's
authorized I/O phase, outside transactions. It does not register an admission
policy, reserve money, retry, enable a feature or unpause a provider.

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
(60 seconds for `SP-SIT-R`). The body also has an elapsed read deadline, checked
between single socket reads. JSON/XML responses are limited to 5 MiB; direct PDF
responses to 10 MiB. Compressed responses are refused before decompression.
Provider adapters own format/schema validation and storage after this byte limit.
No request or response body is logged.

Only `FC-PDF` can follow a 302, once, to the exact documented
`focusnfe.s3.sa-east-1.amazonaws.com` HTTPS host. The second request contains no
provider credentials, cookies or Referer. Location URLs remain ephemeral.
Any additional host requires qualification and a reviewed catalog change.
SERPRO 304 is returned as a protocol status rather than treated as a redirect.

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

The broker service, its HMAC verification, certificate secrets and lifecycle are
AXRM-013 work. `SP-AUTH` cannot go through the provider transport, so workers cannot
call SAPI authentication or handle a contractor certificate.

## Testing and recovery

The scripted peers in `tests/fakes/serpro.py` and `tests/fakes/focus.py` use
ephemeral ports and per-instance request logs. `Reply` supplies status, body,
headers, chunks, reset and event-held latency. TLS tests generate a temporary test
certificate; DNS maps only the documented storage host to a local test server.
No test contacts a real provider.

There are no tables or migrations. Deployment remains inert until an adapter is
connected to the existing closed admission controls. Recovery is a code revert;
no external state or persisted provider data is changed by this foundation.
