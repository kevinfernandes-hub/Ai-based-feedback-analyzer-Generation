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


@celery.task(bind=True, acks_late=True, max_retries=3)
def ai_report_task(self, form_id):
    """Generate an AI report for a given form_id and return HTML/text report."""
    try:
        import json
        from datetime import datetime
        # lazy import to avoid circular dependency
        from app import get_db_connection, ai_generate_text

        conn = get_db_connection(); c = conn.cursor()
        # Use appropriate placeholder depending on DB driver
        q = "SELECT full_text_for_ai FROM responses WHERE form_id = %s" if hasattr(c, 'mogrify') else "SELECT full_text_for_ai FROM responses WHERE form_id = ?"
        c.execute(q, (form_id,))
        rows = c.fetchall()
        conn.close()

        valid_texts = []
        for r in rows:
            if isinstance(r, (list, tuple)):
                val = r[0]
            elif isinstance(r, dict):
                val = r.get('full_text_for_ai')
            else:
                val = None
            if val and str(val).strip() and str(val).strip().lower() != 'none':
                valid_texts.append(val)

        text_data = "\n- ".join(valid_texts)

        if not text_data.strip():
            return {"status": "no_feedback", "report": "<p>No written feedback available.</p>"}

        report = ai_generate_text(
            "Analyze the feedback. Generate an Executive Summary containing 'Top 3 Strengths' and 'Top 3 Actionable Improvements' using HTML tags (<h3>, <ul>, <li>). No markdown blocks.",
            text_data[:6000]
        )
        return {"status": "done", "report": report.replace('```html', '').replace('```', '')}
    except Exception as e:
        try:
            raise self.retry(exc=e, countdown=60)
        except Exception:
            raise


@celery.task(bind=True, acks_late=True, max_retries=3)
def suggest_followup_task(self, form_id):
    try:
        from app import get_db_connection, parse_ai_question_payload, ai_generate_text
        conn = get_db_connection(); c = conn.cursor()
        q = "SELECT full_text_for_ai FROM responses WHERE form_id = %s AND full_text_for_ai IS NOT NULL" if hasattr(c, 'mogrify') else "SELECT full_text_for_ai FROM responses WHERE form_id = ? AND full_text_for_ai IS NOT NULL"
        c.execute(q, (form_id,))
        rows = c.fetchall(); conn.close()
        feedback_texts = []
        for r in rows:
            if isinstance(r, (list, tuple)):
                val = r[0]
            elif isinstance(r, dict):
                val = r.get('full_text_for_ai')
            else:
                val = None
            if val and str(val).strip() and str(val).strip().lower() != 'none':
                feedback_texts.append(val)
        if not feedback_texts:
            return {"status": "no_feedback", "questions": []}

        aggregated_feedback = "\n- ".join(feedback_texts[:20])
        prompt = (
            "Based on the student feedback below, suggest 3-4 specific follow-up questions to dive deeper into the main themes and concerns. "
            "Return STRICT JSON only in the form: {\"questions\":[{\"text\":\"...\",\"type\":\"rating_5\",\"required\":true}]}.\n\n"
            "Feedback:\n"
            f"- {aggregated_feedback}\n"
        )
        raw = ai_generate_text("You suggest follow-up questions based on feedback. Return JSON only.", prompt)
        questions = parse_ai_question_payload(raw)
        return {"status": "done", "questions": questions[:4]}
    except Exception as e:
        try:
            raise self.retry(exc=e, countdown=60)
        except Exception:
            raise
