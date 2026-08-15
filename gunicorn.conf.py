"""Gunicorn configuration for FreelanceDash (used by railway.toml).

The request path contains blocking outbound I/O -- Polar API calls, NATS
publishes, and invoice-PDF rendering (up to ~30s) -- so plain sync workers
would let a single slow request occupy an entire worker. Threaded (gthread)
workers let a blocked request yield its slot, and a small worker count keeps
the memory footprint (one app copy per worker) within a modest Railway plan.

Everything is env-tunable so the process can be right-sized per plan without
a code change. Logs go to stdout/stderr (Railway captures only those).
"""
import multiprocessing
import os


def _int_env(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


bind = f"0.0.0.0:{os.environ.get('PORT', '5000')}"

# Default to a small, memory-bounded fleet; gthread handles concurrency via
# threads rather than many process copies. WEB_CONCURRENCY (Railway's
# convention) or GUNICORN_WORKERS override it.
workers = (
    _int_env("WEB_CONCURRENCY", 0)
    or _int_env("GUNICORN_WORKERS", 0)
    or min(4, multiprocessing.cpu_count() * 2 + 1)
)
worker_class = "gthread"
threads = _int_env("GUNICORN_THREADS", 4)

# Matches the previous inline --timeout; PDF rendering caps itself well below
# this, so the timeout is a backstop, not the normal path.
timeout = _int_env("GUNICORN_TIMEOUT", 120)
graceful_timeout = _int_env("GUNICORN_GRACEFUL_TIMEOUT", 30)

accesslog = "-"
errorlog = "-"
