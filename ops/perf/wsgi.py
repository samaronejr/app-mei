"""The benchmark server also refuses remote or production targets before setup."""

from django.core.wsgi import get_wsgi_application

from ops.perf.configuration import require_local_targets

require_local_targets()
application = get_wsgi_application()
