"""Which address to attribute a request to, given how many proxies sit in front.

Getting this wrong fails in one of two directions and both are silent:

* Trust `X-Forwarded-For` when nothing rewrites it, and any caller sets their own
  address — every per-IP control becomes decorative.
* Ignore it behind a reverse proxy, and every request in the system reports the
  proxy's address — every per-IP control collapses into one shared bucket, and the
  first rate-limited user locks out everybody.

So the number of proxies is configuration, not a guess. `TRUSTED_PROXY_COUNT` counts
the hops the deployment actually controls; the address is taken that many entries from
the right of the header, because entries to the left are attacker-supplied.
"""

from django.conf import settings
from django.http import HttpRequest

FORWARDED_FOR = "HTTP_X_FORWARDED_FOR"


def client_ip(request: HttpRequest) -> str | None:
    """Return the caller's address, or None when it cannot be determined."""
    proxies = int(getattr(settings, "TRUSTED_PROXY_COUNT", 0))
    if proxies > 0:
        forwarded = str(request.META.get(FORWARDED_FOR, ""))
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        if len(hops) >= proxies:
            # Counted from the right: the rightmost entry was appended by the proxy
            # nearest to us, so entry -proxies is the last one we can vouch for.
            return hops[-proxies]
    remote = request.META.get("REMOTE_ADDR")
    return str(remote) if remote else None
