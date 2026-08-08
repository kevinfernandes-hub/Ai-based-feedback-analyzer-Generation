import os

port = os.getenv("PORT", "5050")
bind = f"0.0.0.0:{port}"
workers = int(os.getenv("WEB_CONCURRENCY", "2"))
threads = 4
timeout = 120
accesslog = "-"
errorlog = "-"
