"""Normal local middleware with a measurement-only outer timer."""

from config.settings import dev

globals().update({name: value for name, value in vars(dev).items() if name.isupper()})
DEBUG = False
MIDDLEWARE = ["ops.perf.timer.ServerTimer", *dev.MIDDLEWARE]
PERF_RESOURCE_ATTESTATION = True
PERF_QUERY_CAPTURE = True
# Final capture must not repeat the unbounded T28 planner-statistics stall.
DATABASES = {
    **dev.DATABASES,
    "default": {
        **dev.DATABASES["default"],
        "OPTIONS": {
            **dev.DATABASES["default"].get("OPTIONS", {}),
            "options": "-c statement_timeout=10000 -c lock_timeout=10000",
        },
    },
}
