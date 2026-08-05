import multiprocessing
import os

bind = f"0.0.0.0:{os.getenv('PORT', '5050')}"
# Recommended default: override with WEB_CONCURRENCY env var when deploying
# Default to (2 * CPU) + 1 to allow more concurrent workers on multi-core machines
workers = int(os.getenv("WEB_CONCURRENCY", max(2, (multiprocessing.cpu_count() * 2) + 1)))
threads = int(os.getenv("GUNICORN_THREADS", "2"))
timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
keepalive = 5
worker_class = "gthread"
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("GUNICORN_LOG_LEVEL", "info")
