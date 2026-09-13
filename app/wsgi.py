"""Production WSGI bootstrap with one scheduler and one pair of queue workers."""

import atexit
import logging
import os
import signal
import threading

from audible_service import AudibleService
from logging_config import configure_logging
from web import WebConfigurationError, automatic_scheduler, create_app

os.umask(0o077)
configure_logging()
LOGGER = logging.getLogger(__name__)

try:
    service = AudibleService()
    application = create_app(service, enforce_startup_security=True)
except (ValueError, WebConfigurationError) as exc:
    LOGGER.critical("Start abgebrochen: %s", exc)
    raise SystemExit(78) from None

service.reconcile_existing_outputs()
service.start_job_workers()
atexit.register(service.stop_job_workers)

threading.Thread(
    target=automatic_scheduler,
    args=(service,),
    name="automatic-scheduler",
    daemon=True,
).start()

if __name__ == "__main__":
    from waitress import serve

    def stop_service(signum, frame):
        del signum, frame
        service.stop_job_workers()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop_service)
    signal.signal(signal.SIGINT, stop_service)

    serve(
        application,
        host=os.getenv("WEB_HOST", "127.0.0.1"),
        port=int(os.getenv("WEB_PORT", "8080")),
        threads=max(2, int(os.getenv("WEB_THREADS", "4"))),
    )
