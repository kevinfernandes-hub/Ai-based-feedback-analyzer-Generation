import os
from celery import Celery

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

celery = Celery(
    "ai_feedback",
    broker=REDIS_URL,
    backend=REDIS_URL,
)

# Recommended production Celery settings
celery.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_default_queue='default',
)


@celery.task(bind=True, acks_late=True, max_retries=3)
def analyze_response(self, payload):
    """Background task placeholder: process a submitted response payload.

    Payload is expected to be a dict containing at least `form_id`, `full_text`, and `answers`.
    Implement actual AI/model calls here in production (batch, rate-limit, retry).
    """
    try:
        # Lazy imports to avoid circular imports and reduce worker startup cost
        import json
        from datetime import datetime
        from app import get_db_connection, _adapt_placeholders

        form_id = payload.get("form_id")
        full_text = payload.get("full_text", "")
        answers = payload.get("answers", [])

        # Example lightweight processing: compute simple token count and store as a log
        token_count = len(str(full_text).split()) if full_text else 0

        # Optionally write a processed record or log to DB (keep idempotency in mind)
        conn = get_db_connection()
        c = conn.cursor()
        insert_q = _adapt_placeholders("INSERT INTO processed_responses (form_id, processed_at, token_count, payload_json) VALUES (?, ?, ?, ?)")
        try:
            c.execute(insert_q, (form_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), token_count, json.dumps(payload)))
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

        return {"status": "processed", "token_count": token_count}
    except Exception as e:
        # Let Celery handle retries if configured; for now just log
        try:
            print("analyze_response task error:", str(e))
        except Exception:
            pass
        # Retry with exponential backoff
        try:
            raise self.retry(exc=e, countdown=30)
        except Exception:
            raise
