"""Run the worker as its own process (optional Railway service): python -m app.worker"""

import signal
import threading

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.worker.runner import WorkerPool


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    pool = WorkerPool(settings.worker_concurrency)
    stopped = threading.Event()

    def shutdown(*_) -> None:
        pool.stop()
        stopped.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    pool.start()
    stopped.wait()


if __name__ == "__main__":
    main()
