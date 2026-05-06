"""Invoices package init.

Owns the **module-level** ``ThreadPoolExecutor`` used to off-load
ReportLab PDF rendering off the gunicorn request thread. ReportLab is
purely CPU-bound but releases the GIL during image decoding (Pillow
under the hood), so a small thread pool gives a meaningful win on
many-line-item invoices with logos / signatures without needing Celery
or any new dependency.

Sizing rationale: ``max_workers=2`` per gunicorn worker process. With
the production workflow running 4 sync workers that's an upper bound of
8 concurrent PDF renders machine-wide -- enough headroom to absorb a
small burst without letting a flood of PDF requests starve the rest of
the worker pool. Each render holds ~10-30 MB peak, so 2 in flight per
worker stays well inside the container budget.

The executor is created **lazily** on first use so it never spins up
threads in environments that never render PDFs (the test suite, the
NATS subscriber processes, ad-hoc ``flask shell`` sessions). It is
shut down at process exit via ``atexit`` so workers can drain in-flight
renders cleanly on SIGTERM instead of orphaning them.
"""
from __future__ import annotations

import atexit
from concurrent.futures import ThreadPoolExecutor
from threading import Lock


_PDF_EXECUTOR_MAX_WORKERS = 2

_executor: ThreadPoolExecutor | None = None
_executor_lock = Lock()


def get_pdf_executor() -> ThreadPoolExecutor:
    """Return the process-wide PDF render executor, creating it on first
    call. Thread-safe under concurrent first-use (lock-protected double
    check)."""
    global _executor
    if _executor is not None:
        return _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=_PDF_EXECUTOR_MAX_WORKERS,
                thread_name_prefix='invoice-pdf',
            )
            # Drain in-flight renders on graceful shutdown so a worker
            # restart doesn't leave a half-rendered PDF in a buffer.
            atexit.register(_shutdown_executor)
    return _executor


def _shutdown_executor():
    """atexit hook: stop accepting new jobs and wait for in-flight ones."""
    global _executor
    if _executor is None:
        return
    # wait=True blocks for in-flight renders; cancel_futures=True drops
    # anything still queued. Available since Python 3.9.
    _executor.shutdown(wait=True, cancel_futures=True)
    _executor = None
