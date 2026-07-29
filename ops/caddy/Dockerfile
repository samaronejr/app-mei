# Caddy with the Cloudflare DNS module compiled in.
#
# The stock `caddy:2-alpine` image cannot serve this product. A wildcard certificate is
# mandatory here — every firm is reached at `<slug>.<domain>` and every portal at
# `<slug>-portal.<domain>`, so the hosts are not known ahead of time — and ACME issues a
# wildcard ONLY through the DNS-01 challenge. HTTP-01 cannot do it: proving control of
# `*.example.com` by serving a file at one hostname would prove nothing.
#
# DNS-01 needs a provider module to write the `_acme-challenge` TXT record, and those
# modules are not in the stock image. Without this build `tls { dns cloudflare ... }`
# fails at config load as an unrecognised directive, which surfaces as a container that
# will not start rather than as a certificate that quietly never renews.
FROM caddy:2-builder-alpine AS builder

RUN xcaddy build --with github.com/caddy-dns/cloudflare

FROM caddy:2-alpine

COPY --from=builder /usr/bin/caddy /usr/bin/caddy
