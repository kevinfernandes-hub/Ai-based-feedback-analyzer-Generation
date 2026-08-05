# AI-Based Feedback Analyzer

A Flask web application that analyzes student feedback and provides structured insights through an AI-assisted workflow.

## Features

- Student feedback input and analysis flow
- Dashboard and admin/student views
- Static assets and templates for a web UI
- Production-ready deployment files for Gunicorn, Nginx, systemd, and Render

## Project Structure

```text
Ai-based-feedback-analyzer/
	app.py
	requirements.txt
	wsgi.py
	gunicorn.conf.py
	render.yaml
	templates/
	static/
	instance/
```

## Local Setup

1. Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Configure environment variables:

- Copy `.env.example` (or `.env.template`) to `.env`
- Fill in required values

4. Run the app:

```bash
python app.py
```

The app will start on the configured host/port.

## Running in Production (recommended minimal stack)

1. Provision Redis (used for Celery and rate-limiting):

```bash
# macOS (homebrew)
brew install redis
redis-server /usr/local/etc/redis.conf
```

2. Start a Celery worker (from project root):

```bash
export REDIS_URL=redis://localhost:6379/0
celery -A celery_worker.celery worker --loglevel=info -Q default
```

3. Start Gunicorn with the provided config:

```bash
export FLASK_ENV=production
export FLASK_SECRET_KEY="<secure-secret>"
export ADMIN_PASSWORD="<strong-password>"
gunicorn -c gunicorn.conf.py wsgi:app
```

4. (Optional) Run the k6 load test against your local instance:

```bash
# Install k6 (https://k6.io/docs/getting-started/installation/)
k6 run load_test.js --env BASE_URL=http://127.0.0.1:5050
```

## Running Tests

```bash
python -m pytest
```

If pytest is not installed in your environment, install it first:

```bash
pip install pytest
```

## Deployment

This repository includes deployment helpers:

- `Procfile` for process-based hosting
- `render.yaml` for Render deployment
- `gunicorn.conf.py` for Gunicorn settings
- `nginx-config.template` and `systemd-service.template` for VM/server setups

## Notes

- Keep `.env` private and never commit secrets.
- Logs and runtime files are under `instance/`.