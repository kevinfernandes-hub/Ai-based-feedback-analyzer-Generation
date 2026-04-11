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