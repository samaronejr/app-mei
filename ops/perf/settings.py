"""Normal local middleware with a measurement-only outer timer."""

from config.settings import dev

globals().update({name: value for name, value in vars(dev).items() if name.isupper()})
DEBUG = False
MIDDLEWARE = ["ops.perf.timer.ServerTimer", *dev.MIDDLEWARE]
PERF_RESOURCE_ATTESTATION = True
