import sqlite3
import os
import json
import csv
import io
import uuid
import re
import logging
import secrets
import importlib
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, make_response
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from datetime import datetime
from fpdf import FPDF
from textblob import TextBlob
from better_profanity import profanity
from werkzeug.middleware.proxy_fix import ProxyFix
from logging.handlers import RotatingFileHandler
import os as os_module
try:
    psycopg2 = importlib.import_module("psycopg2")
    psycopg2_extras = importlib.import_module("psycopg2.extras")
    RealDictCursor = getattr(psycopg2_extras, "RealDictCursor", None)
except Exception:
    psycopg2 = None
    RealDictCursor = None

try:
    from groq import Groq
except Exception:
    Groq = None

try:
    from google import genai
except Exception:
    genai = None

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

try:
    _dotenv = importlib.import_module("dotenv")
    _load_dotenv = getattr(_dotenv, "load_dotenv", None)
    if callable(_load_dotenv):
        _load_dotenv()
except Exception:
    pass

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
APP_ENV = os.getenv("FLASK_ENV", "development").strip().lower()
IS_PRODUCTION = APP_ENV == "production"

# Rate limiting
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["5000 per hour", "1000 per minute"],
    storage_uri="memory://"
)

# Structured logging
if not app.debug and IS_PRODUCTION:
    log_dir = os.path.join(app.instance_path, "logs")
    os_module.makedirs(log_dir, exist_ok=True)
    file_handler = RotatingFileHandler(os.path.join(log_dir, "app.log"), maxBytes=10240000, backupCount=10)
    file_handler.setFormatter(logging.Formatter(
        '[%(asctime)s] %(levelname)s in %(module)s: %(message)s [%(pathname)s:%(lineno)d]'
    ))
    file_handler.setLevel(logging.INFO)
    app.logger.addHandler(file_handler)
    app.logger.setLevel(logging.INFO)
else:
    logging.basicConfig(level=logging.DEBUG)

_secret_key = os.getenv("FLASK_SECRET_KEY", "").strip()
if not _secret_key:
    if IS_PRODUCTION:
        raise RuntimeError("FLASK_SECRET_KEY must be set in production.")
    _secret_key = "dev-only-change-me"

app.secret_key = _secret_key
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

if IS_PRODUCTION and os.getenv("ENABLE_HTTPS", "false").lower() == "true":
    app.config.update(
        SESSION_COOKIE_SECURE=True,
        PREFERRED_URL_SCHEME="https",
    )

# Respect reverse-proxy headers in production deployments.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

logging.basicConfig(level=logging.INFO)
DB_PATH = os.path.join(app.instance_path, "feedback.db")
DATABASE_URL = (os.getenv("DATABASE_URL", "") or "").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = f"postgresql://{DATABASE_URL[len('postgres://'):]}"
USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES and not psycopg2:
    raise RuntimeError("DATABASE_URL is set but psycopg2 is not installed. Add psycopg2-binary to requirements.")

DB_INTEGRITY_ERROR = (psycopg2.IntegrityError,) if USE_POSTGRES and psycopg2 else (sqlite3.IntegrityError,)
STUDENT_SUBMITTER_COOKIE = "student_submitter_id"


def _is_duplicate_column_error(exc):
    message = str(exc or "").lower()
    return "duplicate column" in message or "already exists" in message


def _ensure_column(conn, cursor, table_name, column_name, column_definition):
    if USE_POSTGRES:
        try:
            cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {column_name} {column_definition}")
        except Exception:
            pass
        return

    try:
        cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")
    except sqlite3.OperationalError as e:
        if not _is_duplicate_column_error(e):
            raise


def _adapt_placeholders(query):
    if USE_POSTGRES:
        return query.replace("?", "%s")
    return query


class DbCursorAdapter:
    def __init__(self, cursor, connection):
        self._cursor = cursor
        self._connection = connection

    def execute(self, query, params=None):
        statement = _adapt_placeholders(query)
        if params is None:
            return self._cursor.execute(statement)
        return self._cursor.execute(statement, params)

    def executemany(self, query, params_seq):
        statement = _adapt_placeholders(query)
        return self._cursor.executemany(statement, params_seq)

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    @property
    def connection(self):
        return self._connection


class DbConnectionAdapter:
    def __init__(self, conn, row_factory=False):
        self._conn = conn
        self._row_factory = row_factory

    def cursor(self):
        if self._row_factory:
            raw_cursor = self._conn.cursor(cursor_factory=RealDictCursor)
        else:
            raw_cursor = self._conn.cursor()
        return DbCursorAdapter(raw_cursor, self)

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()


def get_db_connection(row_factory=False):
    if USE_POSTGRES:
        connect_kwargs = {"connect_timeout": 10}
        if "sslmode=" not in DATABASE_URL:
            connect_kwargs["sslmode"] = "require"
        conn = psycopg2.connect(DATABASE_URL, **connect_kwargs)
        conn.autocommit = False
        return DbConnectionAdapter(conn, row_factory=row_factory)

    conn = sqlite3.connect(DB_PATH, timeout=10)
    if row_factory:
        conn.row_factory = sqlite3.Row
    return conn


def ensure_submitter_cookie(response):
    token = (request.cookies.get(STUDENT_SUBMITTER_COOKIE) or "").strip()
    if token:
        return response

    response.set_cookie(
        STUDENT_SUBMITTER_COOKIE,
        secrets.token_hex(16),
        max_age=60 * 60 * 24 * 365,
        httponly=True,
        samesite="Lax",
        secure=IS_PRODUCTION,
    )
    return response


def get_submitter_key():
    token = (request.cookies.get(STUDENT_SUBMITTER_COOKIE) or "").strip()
    if token:
        return f"cookie:{token}"

    forwarded_for = request.headers.get("X-Forwarded-For", "")
    ip = (forwarded_for.split(",")[0].strip() if forwarded_for else request.remote_addr) or "unknown"
    user_agent = (request.headers.get("User-Agent", "unknown") or "unknown")[:200]
    return f"fallback:{ip}|{user_agent}"


@app.after_request
def apply_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://cdn.jsdelivr.net https://unpkg.com; style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://unpkg.com https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com https://unpkg.com data:; img-src 'self' data: https:; connect-src 'self' https://cdn.jsdelivr.net https://api.qrserver.com"
    response.headers["Cache-Control"] = "public, max-age=3600" if request.path.startswith('/static/') else "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    if IS_PRODUCTION:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
    return response

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

PO_LIST_FULL = [
    "PO1", "PO2", "PO3", "PO4", "PO5", "PO6",
    "PO7", "PO8", "PO9", "PO10", "PO11", "PO12"
]
PSO_LIST_FULL = ["PSO1", "PSO2", "PSO3"]
PEO_LIST_FULL = []

PO_DESCRIPTIONS = {
    "PO1": "Engineering knowledge",
    "PO2": "Problem analysis",
    "PO3": "Design/development of solutions",
    "PO4": "Conduct investigations of complex problems",
    "PO5": "Modern tool usage",
    "PO6": "The engineer and society",
    "PO7": "Environment and sustainability",
    "PO8": "Ethics",
    "PO9": "Individual and teamwork",
    "PO10": "Communication",
    "PO11": "Project management and finance",
    "PO12": "Life-long learning",
}

QUESTION_THEMES = [
    ("PO1", {"understand", "concept", "fundamental", "theory", "knowledge", "explain", "learn", "basics", "clarity", "clear", "comprehend", "grasp", "foundational", "principle"}),
    ("PO2", {"problem", "analyze", "analysis", "reason", "solve", "critical", "identify", "diagnose", "troubleshoot", "investigate", "explore", "examine", "scrutinize", "decompose"}),
    ("PO3", {"design", "develop", "implement", "build", "create", "solution", "application", "architecture", "construct", "engineer", "develop", "prototype", "specification"}),
    ("PO4", {"investigate", "experiment", "evaluate", "measure", "compare", "test", "evidence", "research", "assess", "verify", "validate", "benchmark", "analysis", "empirical"}),
    ("PO5", {"tool", "software", "lab", "technology", "modern", "program", "code", "simulation", "apparatus", "instrument", "platform", "framework", "library", "api"}),
    ("PO6", {"society", "community", "professional", "impact", "responsibility", "public", "stakeholder", "welfare", "social", "humanity", "development"}),
    ("PO7", {"environment", "sustainability", "green", "waste", "energy", "climate", "carbon", "emissions", "resource", "eco", "conservation"}),
    ("PO8", {"ethic", "ethical", "integrity", "responsible", "fair", "moral", "honesty", "compliance", "confidentiality", "biased", "discrimination"}),
    ("PO9", {"team", "group", "collaborat", "peer", "individual", "participat", "cooperative", "synerg", "multidisciplinary", "diversity", "leadership"}),
    ("PO10", {"communication", "present", "presentation", "report", "write", "explain", "interaction", "articulate", "document", "verbal", "graphical", "listen"}),
    ("PO11", {"project", "manage", "planning", "deadline", "schedule", "estimate", "budget", "resource", "timeline", "finance", "delivery"}),
    ("PO12", {"learn", "latest", "update", "self", "life-long", "lifelong", "adapt", "continuous", "professional", "development", "emerging", "innovation"}),
]

QUESTION_SEMANTIC_HINTS = [
    ("understanding", {"understand", "explain", "concept", "theory", "clarity", "basics", "fundamental", "knowledge"}, ["CO1", "PO1"]),
    ("analysis", {"analyze", "analysis", "problem", "solve", "reason", "critical", "identify"}, ["CO2", "PO2"]),
    ("design", {"design", "develop", "implement", "build", "create", "application", "solution"}, ["CO3", "PO3", "PO5"]),
    ("evaluation", {"evaluate", "test", "assessment", "feedback", "improve", "review", "measure"}, ["CO4", "PO4", "PO8"]),
    ("project", {"project", "plan", "schedule", "estimate", "manage", "deadline", "delivery"}, ["CO4", "PO11"]),
    ("lab", {"lab", "software", "tool", "program", "system", "technology", "simulation", "practical"}, ["CO3", "CO4", "PO5"]),
    ("teamwork", {"team", "group", "peer", "collaborat", "participat", "together"}, ["PO9", "PO10"]),
    ("communication", {"communication", "present", "presentation", "report", "write", "explain", "communicat"}, ["PO10"]),
    ("ethics", {"ethic", "ethical", "integrity", "responsible", "fair", "honest"}, ["PO8"]),
    ("society", {"society", "community", "impact", "responsibility", "professional"}, ["PO6"]),
    ("environment", {"environment", "sustainability", "green", "waste", "energy"}, ["PO7"]),
    ("lifelong", {"learn", "latest", "update", "self", "lifelong", "adapt"}, ["PO12"]),
]

BRAND_COLLEGE_NAME = os.getenv("BRAND_COLLEGE_NAME", "Department of Computer Science and Engineering")
BRAND_LOGO_URL = os.getenv("BRAND_LOGO_URL", "/static/img/logo.png")
BRAND_PRIMARY_COLOR = os.getenv("BRAND_PRIMARY_COLOR", "#0f172a")
BRAND_ACCENT_COLOR = os.getenv("BRAND_ACCENT_COLOR", "#0c4a6e")


def get_branding_context():
    return {
        "college_name": BRAND_COLLEGE_NAME,
        "logo_url": BRAND_LOGO_URL,
        "primary_color": BRAND_PRIMARY_COLOR,
        "accent_color": BRAND_ACCENT_COLOR,
    }

# --- AI CONFIGURATION ---
profanity.load_censor_words()

# API KEYS
# Retrieve from environment; never commit secrets directly.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama3-8b-8192")

GEMINI_FALLBACK_MODELS = [
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash-latest",
    "gemini-1.5-flash",
    "gemini-1.5-pro-latest",
    "gemini-1.5-pro"
]

ai_provider = None
gemini_model = None
gemini_client = None
groq_client = None
gemini_model_candidates = []

def load_gemini_candidates():
    if not genai or not gemini_client:
        return []

    discovered = []
    try:
        for m in gemini_client.models.list():
            model_name = getattr(m, 'name', '').replace('models/', '')
            if model_name and model_name.startswith('gemini'):
                discovered.append(model_name)
    except Exception:
        discovered = []

    preferred = [GEMINI_MODEL] + GEMINI_FALLBACK_MODELS
    ordered = []
    for name in preferred + discovered:
        if name and name not in ordered:
            ordered.append(name)
    return ordered


def clean_question_text(text):
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    cleaned = cleaned.lstrip("-•*0123456789. ")
    if not cleaned:
        return ""
    if not cleaned.endswith("?"):
        cleaned = f"{cleaned}?"
    return cleaned


def get_allowed_mapping_keys(course_name=None):
    keys = set(PO_LIST_FULL + PSO_LIST_FULL + PEO_LIST_FULL)
    for co_text in COURSE_DATA_DB.get(course_name, []):
        code = str(co_text).split(":", 1)[0].strip()
        if code:
            keys.add(code)
    for course_outcomes in COURSE_DATA_DB.values():
        for co_text in course_outcomes:
            code = str(co_text).split(":", 1)[0].strip()
            if code:
                keys.add(code)
    return keys


def tokenize_text(text):
    text = re.sub(r"[^a-z0-9\s]", " ", str(text or "").lower())
    return {token for token in text.split() if len(token) > 2}


def score_outcomes(question_text, course_name=None):
    question_tokens = tokenize_text(question_text)
    scored = []

    for co_text in COURSE_DATA_DB.get(course_name, []):
        code, description = str(co_text).split(":", 1) if ":" in str(co_text) else (str(co_text), str(co_text))
        code = code.strip().upper()
        desc_tokens = tokenize_text(description)
        overlap = len(question_tokens & desc_tokens)
        bonus = 0
        if any(term in question_tokens for term in {"understand", "concept", "theory", "explain"}) and code.endswith("1"):
            bonus += 1
        if any(term in question_tokens for term in {"design", "develop", "implement", "build"}) and code.endswith("3"):
            bonus += 1
        if any(term in question_tokens for term in {"analyze", "analysis", "solve", "problem"}) and code.endswith("2"):
            bonus += 1
        if any(term in question_tokens for term in {"project", "estimate", "schedule"}) and code.endswith("4"):
            bonus += 1
        if any(term in question_tokens for term in {"ethic", "team", "present", "tool", "lab", "environment", "learn"}) and code.endswith("5"):
            bonus += 1
        scored.append((code, overlap + bonus))

    scored.sort(key=lambda item: (-item[1], item[0]))
    hits = [code for code, score in scored if score > 0]
    return hits[:4]


def score_po_outcomes(question_text):
    question_tokens = tokenize_text(question_text)
    scored = []

    for po_code, keywords in QUESTION_THEMES:
        overlap = len(question_tokens & keywords)
        
        # Boost scoring for specific matches
        if po_code == "PO1" and any(term in question_tokens for term in {"understand", "explain", "concept", "theory", "clarity", "clear"}):
            overlap += 2
        if po_code == "PO2" and any(term in question_tokens for term in {"problem", "analyze", "analysis", "solve", "critical"}):
            overlap += 2
        if po_code == "PO3" and any(term in question_tokens for term in {"design", "develop", "implement", "build", "create"}):
            overlap += 2
        if po_code == "PO4" and any(term in question_tokens for term in {"investigate", "experiment", "evaluate", "test", "research"}):
            overlap += 2
        if po_code == "PO5" and any(term in question_tokens for term in {"lab", "software", "tool", "program", "system", "code"}):
            overlap += 2
        if po_code == "PO6" and any(term in question_tokens for term in {"society", "community", "professional", "impact"}):
            overlap += 2
        if po_code == "PO7" and any(term in question_tokens for term in {"environment", "sustainability", "green", "energy"}):
            overlap += 2
        if po_code == "PO8" and any(term in question_tokens for term in {"ethic", "ethical", "integrity", "responsible"}):
            overlap += 2
        if po_code == "PO9" and any(term in question_tokens for term in {"team", "group", "collaborat", "peer"}):
            overlap += 2
        if po_code == "PO10" and any(term in question_tokens for term in {"present", "explain", "share", "communicat", "feedback", "report", "write"}):
            overlap += 2
        if po_code == "PO11" and any(term in question_tokens for term in {"project", "manage", "plan", "deadline", "schedule"}):
            overlap += 2
        if po_code == "PO12" and any(term in question_tokens for term in {"learn", "latest", "update", "self", "lifelong", "adapt"}):
            overlap += 2
            
        if overlap > 0:
            scored.append((po_code, overlap))

    scored.sort(key=lambda item: (-item[1], item[0]))
    hits = [code for code, score in scored if score > 0]
    return hits  # Return all matching POs, not just top 4


def infer_question_theme(question_text):
    tokens = tokenize_text(question_text)
    best_score = 0
    best_codes = []

    for _, keywords, codes in QUESTION_SEMANTIC_HINTS:
        score = len(tokens & keywords)
        if score > best_score:
            best_score = score
            best_codes = list(codes)
        elif score and score == best_score:
            for code in codes:
                if code not in best_codes:
                    best_codes.append(code)

    return best_codes[:4]


def fallback_mappings_for_question(question_text, course_name=None):
    co_hits = score_outcomes(question_text, course_name)
    po_hits = score_po_outcomes(question_text)
    semantic_hits = infer_question_theme(question_text)

    fallback = []
    for code in semantic_hits + co_hits + po_hits:
        if code and code not in fallback:
            fallback.append(code)

    if not fallback and course_name:
        fallback = [str(item).split(":", 1)[0].strip().upper() for item in COURSE_DATA_DB.get(course_name, [])[:3]]

    if not fallback:
        fallback = ["PO10", "PO5"]

    return fallback[:5]


def mapping_type(code):
    code = str(code or "").upper()
    if code.startswith("CO"):
        return "CO"
    if code.startswith("PO"):
        return "PO"
    if code.startswith("PSO"):
        return "PSO"
    if code.startswith("PEO"):
        return "PEO"
    return "OTHER"


def infer_semantic_nba_mappings(question_text, course_name=None, topic=""):
    text = str(question_text or "").strip()
    if not text:
        return []

    tokens = tokenize_text(text)
    topic_tokens = tokenize_text(topic)
    co_hits = score_outcomes(text, course_name)
    po_hits = score_po_outcomes(text)
    semantic_hits = infer_question_theme(text)

    ranked = []
    for code in co_hits[:3]:
        if code not in ranked:
            ranked.append(code)
    for code in po_hits[:4]:
        if code not in ranked:
            ranked.append(code)
    for code in semantic_hits[:4]:
        if code not in ranked:
            ranked.append(code)

    # Add PSO/PEO only when question signals broader professional goals.
    if tokens & {"industry", "career", "professional", "placement"}:
        ranked.extend(["PEO1", "PEO2"])
    if tokens & {"design", "develop", "model", "solution", "software", "system"}:
        ranked.append("PSO1")
    if tokens & {"problem", "analyze", "analysis", "optimize"}:
        ranked.append("PSO2")
    if tokens & {"project", "implement", "deploy", "integration"}:
        ranked.append("PSO3")

    if topic_tokens & {"lab", "practical", "implementation", "tool"} and "PO5" not in ranked:
        ranked.append("PO5")

    deduped = []
    for code in ranked:
        if code and code not in deduped:
            deduped.append(code)
    return deduped[:8]


def build_diverse_mapping_set(ai_mappings, semantic_mappings, allowed, max_items=5):
    ai_mappings = [m for m in ai_mappings if m in allowed]
    semantic_mappings = [m for m in semantic_mappings if m in allowed]

    ordered = []
    for code in ai_mappings + semantic_mappings:
        if code not in ordered:
            ordered.append(code)

    # Prefer at least one CO and one PO whenever available.
    selected = []
    for required_type in ("CO", "PO"):
        for code in ordered:
            if mapping_type(code) == required_type and code not in selected:
                selected.append(code)
                break

    # Fill remaining slots while keeping type diversity.
    type_counts = {}
    for code in selected:
        t = mapping_type(code)
        type_counts[t] = type_counts.get(t, 0) + 1

    for code in ordered:
        if code in selected:
            continue
        t = mapping_type(code)
        if len(selected) >= max_items:
            break
        # Soft cap duplicates of same type if alternatives exist.
        if type_counts.get(t, 0) >= 2 and any(mapping_type(c) != t and c not in selected for c in ordered):
            continue
        selected.append(code)
        type_counts[t] = type_counts.get(t, 0) + 1

    return selected[:max_items]


def sanitize_question_payload(questions, course_name=None):
    allowed_types = {"rating_3", "rating_5", "text"}
    allowed_mappings = get_allowed_mapping_keys(course_name)
    sanitized = []
    seen = set()

    for question in questions or []:
        if isinstance(question, str):
            question = {"text": question}
        if not isinstance(question, dict):
            continue

        text = clean_question_text(question.get("text", ""))
        if not text or text.lower() in seen:
            continue

        question_type = str(question.get("type", "rating_5")).strip()
        if question_type not in allowed_types:
            question_type = "rating_5"

        confidence = question.get("confidence", question.get("mapping_confidence", 0))
        try:
            confidence = float(confidence)
        except Exception:
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        raw_mappings = question.get("mappings", []) or []
        if not isinstance(raw_mappings, list):
            raw_mappings = []
        mappings = []
        for mapping in raw_mappings:
            code = str(mapping or "").strip().upper()
            if code in allowed_mappings and code not in mappings:
                mappings.append(code)

        sanitized.append({
            "text": text,
            "type": question_type,
            "required": bool(question.get("required", True)),
            "mappings": mappings,
            "ai_generated": bool(question.get("ai_generated", question.get("source") == "ai")),
            "confidence": confidence,
            "mapping_source": str(question.get("mapping_source", question.get("source", "manual"))),
        })
        seen.add(text.lower())

    return sanitized

try:
    if GEMINI_API_KEY and genai:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        gemini_model_candidates = load_gemini_candidates()
        initial_model = gemini_model_candidates[0] if gemini_model_candidates else GEMINI_MODEL
        gemini_model = initial_model
        ai_provider = "gemini"
        print(f"✅ AI Online: Connected to Gemini ({initial_model})")
    elif GROQ_API_KEY and Groq:
        groq_client = Groq(api_key=GROQ_API_KEY)
        ai_provider = "groq"
        print(f"✅ AI Online: Connected to Groq ({GROQ_MODEL})")
    else:
        print("⚠️ AI Offline")
except Exception as e:
    ai_provider = None

def ai_generate_text(system_prompt, user_prompt):
    if ai_provider == "gemini" and gemini_client:
        prompt = f"{system_prompt}\n\nUser Input:\n{user_prompt}"
        attempted = set()
        candidate_models = [gemini_model] + (gemini_model_candidates or ([GEMINI_MODEL] + GEMINI_FALLBACK_MODELS))
        last_error = None
        for model_name in candidate_models:
            if not model_name or model_name in attempted:
                continue
            attempted.add(model_name)
            try:
                response = gemini_client.models.generate_content(model=model_name, contents=prompt)
                globals()['gemini_model'] = model_name
                return (getattr(response, 'text', '') or '').strip()
            except Exception as e:
                last_error = e
                continue
        if last_error:
            raise last_error

    if ai_provider == "groq" and groq_client:
        completion = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            model=GROQ_MODEL
        )
        return (completion.choices[0].message.content or '').strip()

    return ""

# SYNCED TO THE UPLOADED COURSE LIST IMAGE
COURSE_DATA_DB = {
    "Theory of Computation": [
        "CO1: Design the Finite State Machine with mathematical representation.", 
        "CO2: Define regular expression for the given Finite State Machine and vice versa.", 
        "CO3: Represent context free grammar in various forms along with its properties.", 
        "CO4: Design Push Down Automaton and Turing Machine as FSM and its various representation.", 
        "CO5: Differentiate between decidable and undecidable problems."
    ],
    "Software Engineering and Project Management": [
        "CO1: Distinguish and apply software development techniques to the different kinds of project.", 
        "CO2: Understand role of software engineer, analyze project requirements and author a formal specification for a software system.", 
        "CO3: Apply design process, steps for effective UI design depending on the requirement of the project.", 
        "CO4: Design test cases, apply testing strategies and demonstrate the ability to plan, estimate project.", 
        "CO5: Demonstrate the ability to work on software project by taking into consideration software quality factors."
    ],
    "Software Engineering and Project Management Lab": [
        "CO1: Elicit and analyze project requirements, and author a formal specification for a software system.", 
        "CO2: Demonstrate the ability to plan, estimate and schedule project.", 
        "CO3: Apply design process depending on the requirement of the project.", 
        "CO4: Design test cases and apply testing strategies in software development."
    ],
    "Operating System": [
        "CO1: Understand the basics of how operating systems work.", 
        "CO2: Explain how processes and CPU scheduling function in an operating system.", 
        "CO3: Solve common process synchronization problems.", 
        "CO4: Describe memory management concepts, including virtual memory.", 
        "CO5: Comprehend disk management and the role of file systems in an operating system."
    ],
    "Operating System Lab": [
        "CO1: Understand and implement basic services and functionalities of the operating system using system calls.", 
        "CO2: Analyze and simulate CPU Scheduling Algorithms like FCFS, Round Robin, SJF, and Priority.", 
        "CO3: Implement memory management schemes and page replacement schemes.", 
        "CO4: Implement synchronization mechanisms to address concurrent access issues.", 
        "CO5: Understand the concepts of deadlock in operating systems and implement them in multi programming system."
    ],
    "Professional Elective-I": [
        "CO1: Demonstrate the working of line drawing and circle drawing algorithm", 
        "CO2: Demonstrate 2D transformations and polygon clipping algorithms.", 
        "CO3: Demonstrate 3D transformations and curves & surfaces.", 
        "CO4: Realize different color models", 
        "CO5: Demonstrate advanced algorithms based on hidden lines and surfaces."
    ],
    "Computer Lab - II": [
        "CO1: Explore and implement the competitive programming concepts of advanced programming.", 
        "CO2: Solve Industry placement problems based on competitive programming."
    ],
    "Open Elective - II": [
        "CO1: Analyze and think in terms of object oriented paradigm during development of application.", 
        "CO2: Apply the concept object initialization and destroy using constructors and destructors.", 
        "CO3: Develop application using the concept of inheritance and evaluate the usefulness.", 
        "CO4: Apply concept polymorphism to implement static and runtime binding.", 
        "CO5: Realize the concept of abstract class, use exception handling technique in program."
    ],
    "Technical Skill Development - II": [
        "CO1: Use compiler Java and eclipse or notepad to write and execute java program.", 
        "CO2: Understand and apply the concept of object-oriented features and Java concept.", 
        "CO3: Apply the concept of multithreaded and implement exception handling.", 
        "CO4: Develop an application using JDBC."
    ],
    "Introduction to Business Management": [
        "CO1: Understand the principles and functions of management.", 
        "CO2: Apply planning and organizing tools to real-world situations.", 
        "CO3: Analyze leadership styles and motivation theories in workplace contexts.", 
        "CO4: Demonstrate basic understanding of marketing, HR, and financial functions.", 
        "CO5: Evaluate the role of entrepreneurship and business environment in economic development."
    ],
    "Career Development - V": [
        "CO1: Engage in career development planning and assessment."
    ]
}

def init_db():
    if not USE_POSTGRES:
        os.makedirs(app.instance_path, exist_ok=True)
    conn = get_db_connection()
    c = conn.cursor()
    if USE_POSTGRES:
        c.execute('''CREATE TABLE IF NOT EXISTS forms (
            id BIGSERIAL PRIMARY KEY, title TEXT, course_name TEXT, structure TEXT, is_active BOOLEAN DEFAULT TRUE, created_at TEXT, start_at TEXT, end_at TEXT, public_token TEXT
        )''')
    else:
        c.execute('''CREATE TABLE IF NOT EXISTS forms (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, course_name TEXT, structure TEXT, is_active BOOLEAN DEFAULT 1, created_at TEXT, start_at TEXT, end_at TEXT, public_token TEXT
        )''')

    _ensure_column(conn, c, "forms", "start_at", "TEXT")
    _ensure_column(conn, c, "forms", "end_at", "TEXT")
    _ensure_column(conn, c, "forms", "public_token", "TEXT")

    c.execute("SELECT id, public_token FROM forms")
    rows = c.fetchall()
    seen_tokens = set()
    for form_id, token in rows:
        if token:
            seen_tokens.add(token)
            continue
        new_token = uuid.uuid4().hex[:12]
        while new_token in seen_tokens:
            new_token = uuid.uuid4().hex[:12]
        seen_tokens.add(new_token)
        c.execute("UPDATE forms SET public_token = ? WHERE id = ?", (new_token, form_id))

    if USE_POSTGRES:
        c.execute('''CREATE TABLE IF NOT EXISTS responses (
            id BIGSERIAL PRIMARY KEY, form_id INTEGER, form_title TEXT, student_name TEXT, attendance INTEGER,
            answers_json TEXT, full_text_for_ai TEXT, sentiment_score DOUBLE PRECISION, sentiment_label TEXT, timestamp TEXT
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS submission_locks (
            id BIGSERIAL PRIMARY KEY,
            form_id INTEGER NOT NULL,
            submitter_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(form_id, submitter_key)
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS event_reports (
            id BIGSERIAL PRIMARY KEY,
            form_id INTEGER UNIQUE,
            report_data TEXT,
            updated_at TEXT
        )''')
    else:
        c.execute('''CREATE TABLE IF NOT EXISTS responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT, form_id INTEGER, form_title TEXT, student_name TEXT, attendance INTEGER,
            answers_json TEXT, full_text_for_ai TEXT, sentiment_score REAL, sentiment_label TEXT, timestamp TEXT
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS submission_locks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            form_id INTEGER NOT NULL,
            submitter_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(form_id, submitter_key)
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS event_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            form_id INTEGER UNIQUE,
            report_data TEXT,
            updated_at TEXT
        )''')
    _ensure_column(conn, c, "event_reports", "created_at", "TEXT")
    conn.commit()
    conn.close()

init_db()

@app.route('/')
def landing(): return render_template('landing.html', brand=get_branding_context())

@app.route('/healthz')
def healthz():
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT 1")
        conn.close()
        return jsonify({"status": "ok", "database": "connected"}), 200
    except Exception as e:
        app.logger.error(f"Health check failed: {str(e)}")
        return jsonify({"status": "error", "database": "disconnected"}), 503

@app.route('/metrics')
def metrics():
    if 'user' not in session and not request.headers.get('X-Internal-Key') == os.getenv('INTERNAL_METRICS_KEY', ''):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        conn = get_db_connection(row_factory=True); c = conn.cursor()
        c.execute("SELECT COUNT(*) as count FROM responses")
        total_responses = c.fetchone()['count']
        c.execute("SELECT COUNT(*) as count FROM forms")
        total_forms = c.fetchone()['count']
        c.execute("SELECT COUNT(*) as count FROM forms WHERE is_active")
        active_forms = c.fetchone()['count']
        conn.close()
        return jsonify({
            "total_responses": total_responses,
            "total_forms": total_forms,
            "active_forms": active_forms
        }), 200
    except Exception as e:
        app.logger.error(f"Metrics endpoint error: {str(e)}")
        return jsonify({"error": "Metrics unavailable"}), 503
@app.route('/student')
def student():
    response = make_response(render_template('student.html', brand=get_branding_context()))
    return ensure_submitter_cookie(response)
@app.route('/f/<token>')
def published_form(token):
    response = make_response(render_template('student.html', published_token=token, brand=get_branding_context()))
    return ensure_submitter_cookie(response)
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('username') == ADMIN_USERNAME and request.form.get('password') == ADMIN_PASSWORD:
            session['user'] = 'admin'; return redirect(url_for('dashboard'))
        return render_template('login.html', error="Invalid Credentials")
    return render_template('login.html')
@app.route('/dashboard')
def dashboard(): return render_template('dashboard.html') if 'user' in session else redirect(url_for('login'))
@app.route('/logout')
def logout(): session.pop('user', None); return redirect(url_for('landing'))

# --- CORE API ---
@app.route('/api/create_form', methods=['POST'])
@limiter.limit("10 per minute")
def create_form():
    if 'user' not in session: return jsonify({"error": "Unauthorized"}), 401
    try:
        data = request.json or {}
        if not isinstance(data, dict):
            return jsonify({"error": "Invalid request format"}), 400
        
        title = str(data.get('title') or "").strip()[:200]
        course_name = str(data.get('course_name') or "").strip()[:200]
        if not title or not course_name:
            return jsonify({"error": "Title and course name required"}), 400
        
        questions = sanitize_question_payload(data.get('questions') or [], course_name)
        if not questions:
            return jsonify({"error": "At least one valid question is required."}), 400

        form_token = uuid.uuid4().hex[:12]
        conn = get_db_connection(); c = conn.cursor()
        c.execute("INSERT INTO forms (title, course_name, structure, created_at, start_at, end_at, public_token) VALUES (?, ?, ?, ?, ?, ?, ?)", 
                  (
                      title,
                      course_name,
                      json.dumps(questions),
                      datetime.now().strftime("%Y-%m-%d"),
                      data.get('start_at') or None,
                      data.get('end_at') or None,
                      form_token
                  ))
        conn.commit(); conn.close()
        app.logger.info(f"Form created: {title} (ID: will be auto-incremented)")
        return jsonify({"status": "success"}), 201
    except Exception as e:
        app.logger.error(f"Error creating form: {str(e)}")
        return jsonify({"error": "Failed to create form"}), 500

@app.route('/api/edit_form', methods=['POST'])
def edit_form():
    if 'user' not in session: return jsonify({"error": "Unauthorized"}), 401
    data = request.json
    questions = sanitize_question_payload(data.get('questions') or [], data.get('course_name'))
    if not questions:
        return jsonify({"error": "At least one valid question is required."}), 400

    conn = get_db_connection(); c = conn.cursor()
    c.execute("UPDATE forms SET title=?, course_name=?, structure=?, start_at=?, end_at=? WHERE id=?", 
              (
                  data.get('title'),
                  data.get('course_name'),
                  json.dumps(questions),
                  data.get('start_at') or None,
                  data.get('end_at') or None,
                  data.get('form_id')
              ))
    conn.commit(); conn.close()
    return jsonify({"status": "success"})

def parse_datetime(value):
    if not value:
        return None
    value = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None

def is_form_open(row):
    now = datetime.now()
    start_at = parse_datetime(row.get('start_at'))
    end_at = parse_datetime(row.get('end_at'))
    if start_at and now < start_at:
        return False
    if end_at and now > end_at:
        return False
    return bool(row.get('is_active'))

@app.route('/api/forms', methods=['GET'])
def get_forms():
    try:
        conn = get_db_connection(row_factory=True); c = conn.cursor()
        c.execute("SELECT * FROM forms ORDER BY id DESC")
        rows = c.fetchall(); conn.close()
        results = []
        for row in rows:
            r = dict(row)
            try: r['structure'] = json.loads(r['structure']) if r['structure'] else []
            except: r['structure'] = []
            r['is_open'] = is_form_open(r)
            r['public_url'] = url_for('published_form', token=r.get('public_token') or '', _external=True)

            if request.args.get('active_only') and not r['is_open']:
                continue
            results.append(r)
        return jsonify(results)
    except: return jsonify([]), 500

@app.route('/api/forms/published/<token>', methods=['GET'])
def get_published_form(token):
    conn = get_db_connection(row_factory=True); c = conn.cursor()
    c.execute("SELECT * FROM forms WHERE public_token = ?", (token,))
    row = c.fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Form not found."}), 404

    form_data = dict(row)
    if not is_form_open(form_data):
        return jsonify({"error": "This form is currently closed."}), 400

    try:
        form_data['structure'] = json.loads(form_data.get('structure') or "[]")
    except Exception:
        form_data['structure'] = []
    form_data['is_open'] = True
    form_data['public_url'] = url_for('published_form', token=form_data.get('public_token') or '', _external=True)
    return jsonify(form_data)

@app.route('/api/toggle_form', methods=['POST'])
def toggle_form():
    if 'user' not in session: return jsonify({"error": "Unauthorized"}), 401
    conn = get_db_connection(); c = conn.cursor()
    c.execute("UPDATE forms SET is_active = ? WHERE id = ?", (request.json.get('status'), request.json.get('id')))
    conn.commit(); conn.close()
    return jsonify({"status": "success"})

@app.route('/api/submit_feedback', methods=['POST'])
@limiter.limit("30 per hour")
def submit_feedback():
    try:
        data = request.json or {}
        if not isinstance(data, dict):
            return jsonify({"status": "error", "message": "Invalid request format"}), 400
        
        answers = data.get('answers', [])
        if not isinstance(answers, list):
            return jsonify({"status": "error", "message": "Invalid answers format"}), 400
        
        submitter_key = get_submitter_key()

        conn = get_db_connection(row_factory=True); c = conn.cursor()
        c.execute("SELECT * FROM forms WHERE id = ?", (data.get('form_id'),))
        form_row = c.fetchone()
        if not form_row:
            conn.close()
            return jsonify({"status": "error", "message": "Form not found."}), 404

        form_data = dict(form_row)
        if not is_form_open(form_data):
            conn.close()
            return jsonify({"status": "error", "message": "Form is currently closed."}), 400

        form_structure = json.loads(form_data.get('structure') or "[]")
        required_map = {str(q.get('text', '')).strip(): bool(q.get('required', True)) for q in form_structure}

        for ans in answers:
            q_text = str(ans.get('question', '')).strip()
            required = required_map.get(q_text, True)
            val = ans.get('answer', '')
            if required and (val is None or str(val).strip() == '' or str(val).strip() == '0'):
                conn.close()
                return jsonify({"status": "error", "message": f"Required question missing: {q_text}"}), 400
        
        text_parts = []; rating_sum = 0; rating_count = 0
        for ans in answers:
            val = ans.get('answer', '')
            if ans.get('type') in ['rating_3', 'rating_5'] and val:
                try: rating_sum += int(val); rating_count += 1
                except: pass
            elif ans.get('type') == 'text' and str(val).strip() and str(val).strip().lower() != 'none':
                text_parts.append(str(val))

        full_text = ". ".join(text_parts)
        if profanity.contains_profanity(full_text):
            return jsonify({"status": "error", "message": "Toxic feedback detected. Please keep your feedback professional."}), 400

        text_score = TextBlob(full_text).sentiment.polarity if full_text else 0.0
        final_score = 0; label = "Neutral"
        if rating_count > 0:
            avg = rating_sum / rating_count
            if avg > (3 if any(a.get('type') == 'rating_5' for a in answers) else 2): label = "Positive"
            elif avg < (2.5 if any(a.get('type') == 'rating_5' for a in answers) else 1.5): label = "Negative"
        else:
            if text_score > 0.15: label = "Positive"
            elif text_score < -0.15: label = "Negative"

        c = conn.cursor()
        try:
            c.execute(
                "INSERT INTO submission_locks (form_id, submitter_key, created_at) VALUES (?, ?, ?)",
                (data.get('form_id'), submitter_key, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
        except DB_INTEGRITY_ERROR:
            conn.close()
            return jsonify({"status": "error", "message": "Duplicate submission blocked: this form was already submitted from this device/browser."}), 409

        c.execute('''INSERT INTO responses (form_id, form_title, student_name, attendance, answers_json, full_text_for_ai, sentiment_score, sentiment_label, timestamp) 
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''', 
                  (data.get('form_id'), data.get('form_title'), data.get('student_name', 'Anonymous'), 100, json.dumps(answers), full_text, text_score, label, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit(); conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return jsonify({"error": str(e)}), 500

# --- AI ENDPOINTS ---
def normalize_question(text):
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    cleaned = cleaned.lstrip("-•*0123456789. ")
    if not cleaned:
        return ""
    if not cleaned.endswith("?"):
        cleaned = f"{cleaned}?"
    return cleaned

def fallback_suggested_questions(course_name, event_title, topic=""):
    context_label = topic or course_name or event_title or "this course"
    suggestions = [
        f"How clearly were the concepts in {context_label} explained?",
        f"How effectively did {context_label} improve your understanding of key topics?",
        "How would you rate the pace and structure of teaching sessions?",
        "How useful were assignments and assessments for your learning?",
        "How satisfied are you with classroom and lab support for this subject?",
        "What is one thing that worked well and should be continued?",
        "What is one improvement that would most enhance your learning experience?"
    ]

    for co_text in COURSE_DATA_DB.get(course_name, [])[:2]:
        parts = co_text.split(":", 1)
        co_code = parts[0].strip() if parts else "CO"
        co_desc = parts[1].strip() if len(parts) > 1 else co_text
        suggestions.append(f"How well did this course help you achieve {co_code} ({co_desc})?")

    deduped = []
    seen = set()
    for item in suggestions:
        q = normalize_question(item)
        if q and q.lower() not in seen:
            deduped.append(q)
            seen.add(q.lower())
    return deduped[:8]


def parse_ai_question_payload(raw_text, course_name=None):
    allowed_mappings = sorted(get_allowed_mapping_keys(course_name))
    text = str(raw_text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)

    payload = None
    try:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            payload = json.loads(text[start:end + 1])
    except Exception:
        payload = None

    candidate_items = []
    if isinstance(payload, dict):
        candidate_items = payload.get("questions", []) or []
    elif isinstance(payload, list):
        candidate_items = payload
    else:
        candidate_items = [line.strip() for line in text.splitlines() if line.strip()]

    normalized = []
    seen = set()
    for item in candidate_items:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            continue

        question_text = clean_question_text(item.get("text", ""))
        if not question_text or question_text.lower() in seen:
            continue

        question_type = str(item.get("type", "rating_5")).strip()
        if question_type not in {"rating_3", "rating_5", "text"}:
            question_type = "rating_5"

        confidence = item.get("confidence", item.get("mapping_confidence", 0.78))
        try:
            confidence = float(confidence)
        except Exception:
            confidence = 0.78
        confidence = max(0.0, min(1.0, confidence))

        mappings = []
        for mapping in item.get("mappings", []) or []:
            code = str(mapping or "").strip().upper()
            if code in allowed_mappings and code not in mappings:
                mappings.append(code)

        if not mappings:
            mappings = [code for code in fallback_mappings_for_question(question_text, course_name) if code in allowed_mappings]
        else:
            semantic = [code for code in fallback_mappings_for_question(question_text, course_name) if code in allowed_mappings]
            merged = []
            for code in semantic + mappings:
                if code not in merged:
                    merged.append(code)
            mappings = merged[:5]

        if mappings and set(mappings).issubset({"CO1", "PO1"}):
            mappings = [code for code in fallback_mappings_for_question(question_text, course_name) if code in allowed_mappings][:5]

        normalized.append({
            "text": question_text,
            "type": question_type,
            "required": bool(item.get("required", True)),
            "mappings": mappings,
            "ai_generated": True,
            "confidence": confidence,
            "mapping_source": "ai",
            "source": "ai",
        })
        seen.add(question_text.lower())

    return normalized

@app.route('/api/ai/suggest_questions', methods=['POST'])
def ai_suggest_questions():
    if 'user' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json or {}
    course_name = str(data.get('course_name', '')).strip()
    event_title = str(data.get('event_title', '')).strip()
    topic = str(data.get('topic', '')).strip()
    fallback = fallback_suggested_questions(course_name, event_title, topic)

    if not ai_provider:
        return jsonify({"questions": [{"text": q, "type": "rating_5", "required": True, "mappings": [], "ai_generated": False, "confidence": 0.0, "mapping_source": "fallback", "source": "fallback"} for q in fallback], "source": "fallback"})

    prompt = (
        "Generate 6 concise student feedback questions for a college feedback form. "
        "Return STRICT JSON only, no markdown, with this shape: "
        "{\"questions\":[{\"text\":\"...\",\"type\":\"rating_5\",\"required\":true,\"mappings\":[\"CO1\",\"PO2\"],\"confidence\":0.84}]}. "
        "Use only these question types: rating_3, rating_5, text. "
        "Use only allowed mapping keys and keep questions clear, short, and non-duplicated. "
        "Include at least 2 questions tied to outcomes or learning impact.\n"
        f"Course: {course_name or 'N/A'}\n"
        f"Topic: {topic or event_title or 'N/A'}\n"
        f"Allowed mappings: {', '.join(sorted(get_allowed_mapping_keys(course_name)))}"
    )

    try:
        raw = ai_generate_text("You create clear, short feedback form questions and return JSON only.", prompt)
        candidates = parse_ai_question_payload(raw, course_name)
        if not candidates:
            candidates = [{"text": q, "type": "rating_5", "required": True, "mappings": [], "ai_generated": False, "confidence": 0.0, "mapping_source": "fallback", "source": "fallback"} for q in fallback]
            return jsonify({"questions": candidates, "source": "fallback"})

        return jsonify({"questions": candidates[:8], "source": "ai"})
    except Exception:
        return jsonify({"questions": [{"text": q, "type": "rating_5", "required": True, "mappings": [], "ai_generated": False, "confidence": 0.0, "mapping_source": "fallback", "source": "fallback"} for q in fallback], "source": "fallback"})


@app.route('/api/ai/regenerate_question', methods=['POST'])
def ai_regenerate_question():
    if 'user' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json or {}
    course_name = str(data.get('course_name', '')).strip()
    event_title = str(data.get('event_title', '')).strip()
    topic = str(data.get('topic', '')).strip()
    seed = str(data.get('seed_text', '')).strip()
    index = int(data.get('index', 0) or 0)

    fallback = fallback_suggested_questions(course_name, event_title, topic)
    fallback_text = fallback[min(index, len(fallback) - 1)] if fallback else "How would you rate your learning experience?"

    if not ai_provider:
        return jsonify({"question": {"text": fallback_text, "type": "rating_5", "required": True, "mappings": [], "ai_generated": False, "confidence": 0.0, "mapping_source": "fallback", "source": "fallback"}, "source": "fallback"})

    prompt = (
        "Regenerate one concise student feedback question for a college feedback form. "
        "Return STRICT JSON only with this shape: "
        "{\"text\":\"...\",\"type\":\"rating_5\",\"required\":true,\"confidence\":0.84}. "
        "Use only one question, keep it clear, non-duplicated, and at most 18 words.\n"
        f"Course: {course_name or 'N/A'}\n"
        f"Topic: {topic or event_title or 'N/A'}\n"
        f"Seed question: {seed or fallback_text}\n"
        f"Preferred style: {data.get('type', 'rating_5')}"
    )

    try:
        raw = ai_generate_text("You rewrite one feedback question and return JSON only.", prompt)
        parsed = parse_ai_question_payload(raw, course_name)
        question = parsed[0] if parsed else None
        if not question:
            raise ValueError("No valid question returned")
        question.pop("source", None)
        return jsonify({"question": question, "source": "ai"})
    except Exception:
        return jsonify({"question": {"text": fallback_text, "type": "rating_5", "required": True, "mappings": [], "ai_generated": False, "confidence": 0.0, "mapping_source": "fallback", "source": "fallback"}, "source": "fallback"})


@app.route('/api/ai/suggest_mappings', methods=['POST'])
def ai_suggest_mappings():
    if 'user' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json or {}
    course_name = str(data.get('course_name', '')).strip()
    topic = str(data.get('topic', '')).strip()
    question_text = clean_question_text(data.get('question_text', ''))
    question_type = str(data.get('type', 'rating_5')).strip()

    allowed = sorted(get_allowed_mapping_keys(course_name))
    if not question_text:
        return jsonify({"error": "Question text is required."}), 400

    semantic_seed = infer_semantic_nba_mappings(question_text, course_name, topic)
    fallback = fallback_mappings_for_question(question_text, course_name)
    fallback = [item for item in (semantic_seed + fallback) if item in allowed]
    fallback = list(dict.fromkeys(fallback))

    if not ai_provider:
        return jsonify({"mappings": fallback[:4], "confidence": 0.0, "source": "fallback"})

    course_outcomes = COURSE_DATA_DB.get(course_name, [])
    course_outcome_context = "\n".join([f"- {item}" for item in course_outcomes]) if course_outcomes else "- No course outcomes available"
    po_context = "\n".join([f"- {code}: {desc}" for code, desc in PO_DESCRIPTIONS.items()])
    semantic_hint = ", ".join(fallback[:4]) if fallback else "N/A"

    prompt = (
        "You are an NBA (National Board of Accreditation) outcomes mapping expert. "
        "Map the following student feedback question to the MOST RELEVANT and DIVERSE NBA outcomes. "
        "CRITICAL: Choose different outcomes based on question meaning, NOT by matching code numbers. "
        "Return STRICT JSON only in the form: {\"mappings\":[\"CO2\",\"PO5\"],\"confidence\":0.88}.\n\n"
        "MAPPING GUIDELINES:\n"
        "- For understanding/basics questions → Map to PO1, CO1\n"
        "- For problem-solving/analysis → Map to PO2, CO2 or PO4, CO4\n"
        "- For design/implementation → Map to PO3, CO3 or PO5 (tools)\n"
        "- For lab/tools/software questions → Map to PO5, CO3, CO4\n"
        "- For teamwork/communication → Map to PO9, PO10\n"
        "- For ethics/responsibility → Map to PO6, PO8\n"
        "- For sustainability → Map to PO7\n"
        "- For project/management → Map to PO11\n"
        "- For lifelong learning → Map to PO12\n\n"
        "IMPORTANT:\n"
        "- Return 2 to 5 mappings max\n"
        "- Spread across DIFFERENT outcome types (CO, PO, PSO, PEO)\n"
        "- AVOID generic-only outputs like all CO1 or all PO1\n"
        "- MUST map based on question MEANING and CONTEXT, not code sequence\n"
        "- Use only allowed mapping keys from the list below\n\n"
        f"Course: {course_name or 'N/A'}\n"
        f"Topic: {topic or 'N/A'}\n"
        f"Question: {question_text}\n"
        f"Question type: {question_type}\n"
        f"Semantic hint: {semantic_hint}\n"
        f"Course outcomes:\n{course_outcome_context}\n"
        f"PO descriptors (12 outcomes):\n{po_context}\n"
        f"Allowed mappings: {', '.join(allowed)}"
    )

    try:
        raw = ai_generate_text("You map a feedback question to valid CO/PO outcomes and return JSON only.", prompt)
        parsed = None
        try:
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw or "").strip(), flags=re.IGNORECASE)
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end != -1 and end > start:
                parsed = json.loads(cleaned[start:end + 1])
        except Exception:
            parsed = None

        mappings = []
        if isinstance(parsed, dict):
            for mapping in parsed.get("mappings", []) or []:
                code = str(mapping or "").strip().upper()
                if code in allowed and code not in mappings:
                    mappings.append(code)

        try:
            confidence = float(parsed.get("confidence", 0.78) if isinstance(parsed, dict) else 0.78)
        except Exception:
            confidence = 0.78
        confidence = max(0.0, min(1.0, confidence))

        if not mappings:
            mappings = fallback[:5]

        generic_only = mappings and set(mappings).issubset({"CO1", "PO1"})
        if generic_only:
            mappings = fallback[:5]
            confidence = 0.35 if mappings else 0.0

        if not mappings:
            mappings = fallback[:5]

        semantic = infer_semantic_nba_mappings(question_text, course_name, topic)
        mappings = build_diverse_mapping_set(mappings, semantic + fallback, allowed, max_items=5)

        if not mappings:
            mappings = fallback[:5]

        return jsonify({"mappings": mappings[:5], "confidence": confidence, "source": "ai"})
    except Exception:
        return jsonify({"mappings": fallback[:4], "confidence": 0.0, "source": "fallback"})

@app.route('/api/ai/report', methods=['POST'])
def ai_report():
    if 'user' not in session: return jsonify({"error": "Unauthorized"}), 401
    if not ai_provider: return jsonify({"report": "<p>AI Offline. Set GEMINI_API_KEY or GROQ_API_KEY.</p>"})
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT full_text_for_ai FROM responses WHERE form_id = ?", (request.json.get('form_id'),))
    rows = c.fetchall(); conn.close()
    
    valid_texts = [r[0] for r in rows if r[0] and r[0].strip() and r[0].strip().lower() != 'none']
    text_data = "\n- ".join(valid_texts)
    
    if not text_data.strip(): return jsonify({"report": "<p>No written feedback available.</p>"})
    try:
        report = ai_generate_text(
            "Analyze the feedback. Generate an Executive Summary containing 'Top 3 Strengths' and 'Top 3 Actionable Improvements' using HTML tags (<h3>, <ul>, <li>). No markdown blocks.",
            text_data[:6000]
        )
        return jsonify({"report": report.replace('```html', '').replace('```', '')})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/outcome_risk', methods=['GET'])
def outcome_risk():
    if 'user' not in session: return jsonify({"error": "Unauthorized"}), 401
    form_id = request.args.get('form_id')
    if not form_id:
        return jsonify({"error": "form_id required"}), 400
    
    conn = get_db_connection(row_factory=True); c = conn.cursor()
    c.execute("SELECT * FROM forms WHERE id = ?", (form_id,))
    form_data = c.fetchone()
    
    if not form_data:
        conn.close()
        return jsonify({"error": "Form not found"}), 404
    
    c.execute("SELECT * FROM responses WHERE form_id = ?", (form_id,))
    responses = c.fetchall()
    conn.close()
    
    stats = {}
    for resp in responses:
        try:
            answers = json.loads(resp['answers_json'])
            q_map = {}
            c2 = get_db_connection(row_factory=True).cursor()
            c2.execute("SELECT * FROM forms WHERE id = ?", (form_id,))
            form_row = c2.fetchone()
            if form_row:
                struct = json.loads(form_row['structure'] or '[]')
                for q in struct:
                    q_map[q.get('text', '')] = q.get('mappings', [])
            c2.connection.close()
            
            for ans in answers:
                q_text = ans.get('question', '')
                answer_val = ans.get('answer', '')
                if q_text in q_map:
                    for mapping in q_map[q_text]:
                        if mapping not in stats:
                            stats[mapping] = {"sum": 0, "count": 0}
                        if ans.get('type') in ['rating_3', 'rating_5'] and answer_val:
                            try:
                                stats[mapping]["sum"] += int(answer_val)
                                stats[mapping]["count"] += 1
                            except:
                                pass
        except:
            pass
    
    low_outcomes = []
    for code, data in stats.items():
        if data["count"] > 0:
            avg = data["sum"] / data["count"]
            max_rating = 5
            attainment = (avg / max_rating) * 100
            if attainment < 70:
                low_outcomes.append({
                    "code": code,
                    "attainment": round(attainment, 1),
                    "avg_rating": round(avg, 2),
                    "count": data["count"],
                    "risk_level": "Critical" if attainment < 50 else "High" if attainment < 60 else "Medium"
                })
    
    low_outcomes.sort(key=lambda x: x["attainment"])
    return jsonify({"outcomes": low_outcomes[:8], "total_outcomes": len(stats)})

@app.route('/api/suggest_followup_questions', methods=['POST'])
def suggest_followup_questions():
    if 'user' not in session: return jsonify({"error": "Unauthorized"}), 401
    if not ai_provider: return jsonify({"questions": [], "source": "offline"})
    
    data = request.json or {}
    form_id = data.get('form_id')
    if not form_id:
        return jsonify({"error": "form_id required"}), 400
    
    conn = get_db_connection(row_factory=True); c = conn.cursor()
    c.execute("SELECT * FROM forms WHERE id = ?", (form_id,))
    form_data = c.fetchone()
    
    if not form_data:
        conn.close()
        return jsonify({"error": "Form not found"}), 404
    
    course_name = form_data['course_name']
    c.execute("SELECT full_text_for_ai, sentiment_label FROM responses WHERE form_id = ? AND full_text_for_ai IS NOT NULL", (form_id,))
    feedback_rows = c.fetchall()
    conn.close()
    
    feedback_texts = [r['full_text_for_ai'] for r in feedback_rows if r['full_text_for_ai'] and r['full_text_for_ai'].strip()]
    if not feedback_texts:
        return jsonify({"questions": [], "source": "no_feedback"})
    
    aggregated_feedback = "\n- ".join(feedback_texts[:20])
    
    prompt = (
        "Based on the student feedback below, suggest 3-4 specific follow-up questions to dive deeper into the main themes and concerns. "
        "Return STRICT JSON only in the form: {\"questions\":[{\"text\":\"...\",\"type\":\"rating_5\",\"required\":true}]}.\n\n"
        "Context:\n"
        f"Course: {course_name}\n"
        f"Number of responses: {len(feedback_texts)}\n\n"
        "Feedback:\n"
        f"- {aggregated_feedback}\n\n"
        "Guidelines for follow-up questions:\n"
        "- Dig deeper into pain points or gaps mentioned\n"
        "- Ask specific, actionable questions (not generic)\n"
        "- Use rating_5 type for majority, mix with text for open-ended\n"
        "- Keep questions concise (under 20 words)\n"
        "- Focus on discrete topics (lab issues, teaching clarity, pace, etc.)"
    )
    
    try:
        raw = ai_generate_text("You suggest follow-up questions based on feedback. Return JSON only.", prompt)
        followup = parse_ai_question_payload(raw, course_name)
        if not followup:
            return jsonify({"questions": [], "source": "parse_failed"})
        return jsonify({"questions": followup[:4], "source": "ai"})
    except Exception as e:
        return jsonify({"questions": [], "source": "error", "error": str(e)})

# --- ENGINE & EXPORTS ---
def sort_key(k):
    if k.startswith('CO'): return (0, int('0' + ''.join(filter(str.isdigit, k))))
    elif k.startswith('PO'): return (1, int('0' + ''.join(filter(str.isdigit, k))))
    elif k.startswith('PSO'): return (2, int('0' + ''.join(filter(str.isdigit, k))))
    return (3, 0)

def get_attainment_data(form_id):
    conn = get_db_connection(row_factory=True); c = conn.cursor()
    c.execute("SELECT * FROM responses WHERE form_id = ?", (form_id,))
    responses = c.fetchall()
    c.execute("SELECT * FROM forms WHERE id = ?", (form_id,))
    form_data = c.fetchone()
    conn.close()

    course_name = form_data['course_name'] if form_data else "Unknown"
    form_title = form_data['title'] if form_data else "Unknown"
    structure = json.loads(form_data['structure']) if form_data and form_data['structure'] else []

    if not responses: 
        return {"stats": [], "question_stats": [], "sentiment": {}, "charts": {}, "total": 0, "course_name": course_name, "title": form_title, "responses": []}

    # Only CO, PO, and PSO are measured in direct course feedback & event attainment (no PEO)
    stats = {}
    for i in range(1, 7): stats[f"CO{i}"] = {"sum": 0, "max_sum": 0, "count": 0}
    for i in range(1, 13): stats[f"PO{i}"] = {"sum": 0, "max_sum": 0, "count": 0}
    for i in range(1, 4): stats[f"PSO{i}"] = {"sum": 0, "max_sum": 0, "count": 0}
    
    question_stats = []
    for q in structure:
        clean_maps = [m for m in q.get('mappings', []) if not str(m).upper().startswith('PEO')]
        question_stats.append({"text": q.get('text', ''), "type": q.get('type', 'text'), "mappings": clean_maps, "sum": 0, "max_sum": 0, "count": 0})

    pos = 0; neu = 0; neg = 0
    trend_data = []

    for r in responses:
        lbl = r['sentiment_label']
        if lbl == 'Positive': pos += 1
        elif lbl == 'Negative': neg += 1
        else: neu += 1

        answers = json.loads(r['answers_json'])
        r_sum = 0; r_cnt = 0
        for ans in answers:
            score = int(ans['answer']) if ans['answer'] and ans['type'] in ['rating_3', 'rating_5'] else 0
            max_q_score = 3 if ans['type'] == 'rating_3' else (5 if ans['type'] == 'rating_5' else 0)
            
            if max_q_score > 0:
                r_sum += (score / max_q_score) * 100
                r_cnt += 1

            if max_q_score > 0 and 'mappings' in ans:
                for key in ans['mappings']:
                    if key in stats and not str(key).upper().startswith('PEO'):
                        stats[key]["sum"] += score; stats[key]["max_sum"] += max_q_score; stats[key]["count"] += 1
            
            for qs in question_stats:
                if qs['text'] == ans['question']:
                    if qs['type'] in ['rating_3', 'rating_5'] and ans['answer']:
                        qs['sum'] += score; qs['max_sum'] += max_q_score; qs['count'] += 1
                    elif qs['type'] == 'text' and str(ans['answer']).strip() and str(ans['answer']).strip().lower() != 'none':
                        qs['count'] += 1
        
        if r_cnt > 0:
            trend_data.append(round(r_sum / r_cnt, 1))

    report = []
    level_counts = {'High': 0, 'Moderate': 0, 'Low': 0}
    
    all_keys = sorted(stats.keys(), key=sort_key)
    for key in all_keys:
        data = stats[key]
        if data['count'] > 0 and data['max_sum'] > 0:
            percentage = (data['sum'] / data['max_sum']) * 100
            avg_score = round(data['sum'] / data['count'], 2)
            
            level = "L1 (Low)"; color = "text-red-600 bg-red-50"
            if percentage >= 70: 
                level = "L3 (High)"; color = "text-green-600 bg-green-50"; level_counts['High'] += 1
            elif percentage >= 60: 
                level = "L2 (Moderate)"; color = "text-yellow-600 bg-yellow-50"; level_counts['Moderate'] += 1
            else:
                level_counts['Low'] += 1

            report.append({"code": key, "avg": avg_score, "pct": round(percentage, 1), "level": level, "color": color, "student_count": data['count']})

    q_labels = []
    q_data = []
    for i, qs in enumerate(question_stats):
        qs['pct'] = round((qs['sum'] / qs['max_sum']) * 100, 1) if qs['max_sum'] > 0 else 0
        qs['avg'] = round(qs['sum'] / qs['count'], 2) if qs['count'] > 0 and qs['max_sum'] > 0 else 0
        if qs['type'] in ['rating_3', 'rating_5']:
            q_labels.append(f"Q{i+1}")
            q_data.append(qs['pct'])

    charts_data = {
        "pie": [level_counts['High'], level_counts['Moderate'], level_counts['Low']],
        "bar": {"labels": q_labels, "data": q_data},
        "line": trend_data
    }

    risk_score = 0
    weak_outcomes = [row for row in report if row.get('pct', 0) < 60]
    weak_pct = (len(weak_outcomes) / len(report) * 100) if report else 100
    risk_score += weak_pct * 0.7

    if trend_data:
        recent_window = trend_data[-3:] if len(trend_data) >= 3 else trend_data
        recent_avg = sum(recent_window) / len(recent_window)
        if recent_avg < 65:
            risk_score += 20
        elif recent_avg < 75:
            risk_score += 10

    if neg > pos:
        risk_score += 10

    risk_score = max(0, min(100, round(risk_score, 1)))
    risk_level = "Low"
    if risk_score >= 70:
        risk_level = "High"
    elif risk_score >= 40:
        risk_level = "Moderate"

    return {
        "stats": report, "question_stats": question_stats, "sentiment": {"pos": pos, "neu": neu, "neg": neg},
        "charts": charts_data,
        "total": len(responses),
        "course_name": course_name,
        "title": form_title,
        "responses": [dict(r) for r in responses],
        "risk": {
            "score": risk_score,
            "level": risk_level,
            "weak_outcomes": [w.get('code') for w in weak_outcomes[:5]]
        }
    }

@app.route('/api/attainment', methods=['GET'])
def get_attainment_api():
    if 'user' not in session: return jsonify({"error": "Unauthorized"}), 401
    return jsonify(get_attainment_data(request.args.get('form_id')))

@app.route('/api/export_pdf', methods=['GET'])
def export_pdf():
    if 'user' not in session: return jsonify({"error": "Unauthorized"}), 401
    data = get_attainment_data(request.args.get('form_id'))
    pdf = FPDF()
    pdf.add_page()
    
    pdf.set_font("Arial", 'B', 15)
    pdf.cell(0, 8, txt="DEPARTMENT OF COMPUTER SCIENCE AND ENGINEERING", ln=True, align='C')
    pdf.set_font("Arial", 'I', 9)
    pdf.multi_cell(0, 5, txt="VISION: To develop globally competent computing community with the ability to make constructive contribution to society.", align='C')
    pdf.multi_cell(0, 5, txt="MISSION: To develop technocrats with capabilities to address the challenges in computer engineering by providing strong academics and wide industry exposure.", align='C')
    pdf.line(10, pdf.get_y()+2, 200, pdf.get_y()+2)
    pdf.ln(6)

    pdf.set_font("Arial", 'B', 11); pdf.cell(0, 6, txt="OBE ATTAINMENT REPORT", ln=True, align='C'); pdf.ln(5)
    
    pdf.set_font("Arial", 'B', 10)
    pdf.cell(35, 7, "Event Title:", 0, 0); pdf.set_font("Arial", '', 10); pdf.cell(0, 7, data['title'], 0, 1)
    pdf.set_font("Arial", 'B', 10)
    pdf.cell(35, 7, "Course Name:", 0, 0); pdf.set_font("Arial", '', 10); pdf.cell(0, 7, data['course_name'], 0, 1)
    pdf.set_font("Arial", 'B', 10)
    pdf.cell(35, 7, "Date:", 0, 0); pdf.set_font("Arial", '', 10); pdf.cell(0, 7, datetime.now().strftime('%Y-%m-%d %H:%M'), 0, 1)
    pdf.ln(3); pdf.set_fill_color(230, 240, 255); pdf.set_font("Arial", 'B', 10)
    pdf.cell(0, 8, f" Total Students Evaluated: {data['total']}", 1, 1, 'L', True); pdf.ln(8)
    
    pdf.set_font("Arial", 'B', 12); pdf.cell(0, 8, "PART 1: OBE Attainment (Multi-Mapped)", ln=True)
    pdf.set_fill_color(50, 50, 50); pdf.set_text_color(255, 255, 255); pdf.set_font("Arial", 'B', 9)
    pdf.cell(30, 8, 'Outcome', 1, 0, 'C', True); pdf.cell(35, 8, 'Eval Points', 1, 0, 'C', True); pdf.cell(35, 8, 'Avg Score', 1, 0, 'C', True); pdf.cell(40, 8, 'Attainment %', 1, 0, 'C', True); pdf.cell(50, 8, 'Level', 1, 1, 'C', True)
    pdf.set_text_color(0, 0, 0); pdf.set_font("Arial", '', 9)
    for row in data['stats']:
        pdf.cell(30, 8, row['code'], 1, 0, 'C'); pdf.cell(35, 8, str(row['student_count']), 1, 0, 'C'); pdf.cell(35, 8, str(row['avg']), 1, 0, 'C'); pdf.cell(40, 8, f"{row['pct']}%", 1, 0, 'C'); pdf.cell(50, 8, row['level'].upper(), 1, 1, 'C')
    
    pdf.ln(10); pdf.set_font("Arial", 'B', 12); pdf.cell(0, 8, "PART 2: Question Breakdown", ln=True)
    for i, qs in enumerate(data['question_stats']):
        pdf.set_fill_color(245, 245, 245); pdf.set_font("Arial", 'B', 9)
        pdf.multi_cell(0, 7, f"Q{i+1}: {qs['text'].encode('latin-1', 'replace').decode('latin-1')} [Mappings: {', '.join(qs['mappings'])}]", fill=True)
        pdf.set_font("Arial", '', 9)
        if qs['type'] in ['rating_3', 'rating_5']:
            pdf.cell(0, 6, f"Average Rating: {qs['avg']}  |  Attainment: {qs['pct']}% ({qs['count']} responses)", ln=True)
        else:
            pdf.cell(0, 6, f"Comments Received: {qs['count']}", ln=True)

    charts = data.get('charts', {})
    if sum(charts.get('pie', [0,0,0])) > 0:
        pdf.add_page(); pdf.set_font("Arial", 'B', 14); pdf.cell(0, 10, "PART 3: Visual Analytics", ln=True, align='C'); pdf.line(10, 20, 200, 20); pdf.ln(5)
        uid = str(uuid.uuid4()); pie_path = f"temp_pie_{uid}.png"; bar_path = f"temp_bar_{uid}.png"; line_path = f"temp_line_{uid}.png"
        try:
            # Set modern styling
            plt.rcParams['font.sans-serif'] = 'DejaVu Sans'
            plt.rcParams['axes.edgecolor'] = '#cbd5e1'
            plt.rcParams['axes.linewidth'] = 0.8

            # --- 1. PIE CHART (No overlapping 0% labels) ---
            fig, ax = plt.subplots(figsize=(4.8, 3.8), dpi=220)
            pie_vals = charts.get('pie', [0, 0, 0])
            raw_labels = ['High (L3)', 'Moderate (L2)', 'Low (L1)']
            raw_colors = ['#22c55e', '#eab308', '#ef4444']
            
            # Filter out 0% slices for the wedge drawing to avoid text collisions
            non_zero_vals = []
            non_zero_labels = []
            non_zero_colors = []
            for v, l, c in zip(pie_vals, raw_labels, raw_colors):
                if v > 0:
                    non_zero_vals.append(v)
                    non_zero_labels.append(l)
                    non_zero_colors.append(c)
            
            if non_zero_vals:
                wedges, texts, autotexts = ax.pie(
                    non_zero_vals, 
                    labels=non_zero_labels if len(non_zero_vals) > 1 else None, 
                    colors=non_zero_colors, 
                    autopct='%1.1f%%',
                    startangle=140,
                    pctdistance=0.6 if len(non_zero_vals) > 1 else 0.0,
                    wedgeprops={'edgecolor': 'white', 'linewidth': 1.5, 'antialiased': True},
                    textprops={'fontsize': 9, 'weight': 'bold'}
                )
                for at in autotexts:
                    at.set_color('white' if non_zero_colors[0] != '#eab308' else '#0f172a')
                    at.set_fontsize(10)
                    at.set_weight('bold')
            else:
                ax.text(0.5, 0.5, 'No Data', horizontalalignment='center', verticalalignment='center')

            ax.set_title('Outcome Attainment Level Distribution', fontsize=11, weight='bold', pad=12, color='#0f172a')
            # Neat bottom legend showing all 3 levels
            legend_labels = [f"{l} ({v})" for l, v in zip(raw_labels, pie_vals)]
            ax.legend(
                [plt.Rectangle((0,0),1,1, color=c) for c in raw_colors],
                legend_labels,
                loc='lower center',
                bbox_to_anchor=(0.5, -0.12),
                ncol=3,
                frameon=False,
                fontsize=8
            )
            plt.tight_layout()
            plt.savefig(pie_path, bbox_inches='tight', dpi=220)
            plt.close()

            # --- 2. BAR CHART (Rounded style with value annotations) ---
            fig, ax = plt.subplots(figsize=(4.8, 3.8), dpi=220)
            bar_labels = charts.get('bar', {}).get('labels', [])
            bar_data = charts.get('bar', {}).get('data', [])
            
            bars = ax.bar(bar_labels, bar_data, color='#3b82f6', width=0.55, edgecolor='#2563eb', linewidth=1, zorder=3)
            ax.set_title('Question-Wise Attainment (%)', fontsize=11, weight='bold', pad=12, color='#0f172a')
            ax.set_ylim(0, 115)
            ax.set_ylabel('Attainment %', fontsize=9, color='#475569')
            ax.grid(axis='y', linestyle='--', alpha=0.4, zorder=0)
            ax.set_axisbelow(True)
            
            # Value label on top of each bar
            for bar in bars:
                h = bar.get_height()
                ax.annotate(f'{int(h)}%',
                            xy=(bar.get_x() + bar.get_width() / 2, h),
                            xytext=(0, 3),
                            textcoords="offset points",
                            ha='center', va='bottom', fontsize=8.5, weight='bold', color='#1e3a8a')
            
            plt.tight_layout()
            plt.savefig(bar_path, bbox_inches='tight', dpi=220)
            plt.close()

            # --- 3. LINE CHART (R1, R2 trend with filled area) ---
            fig, ax = plt.subplots(figsize=(8.2, 3.2), dpi=220)
            line_data = charts.get('line', [])
            x_indices = list(range(len(line_data)))
            x_labels = [f"R{i+1}" for i in range(len(line_data))]
            
            ax.plot(x_indices, line_data, color='#8b5cf6', linewidth=2.2, marker='o', markersize=6, 
                    markerfacecolor='white', markeredgewidth=2, markeredgecolor='#7c3aed', zorder=4)
            ax.fill_between(x_indices, line_data, color='#8b5cf6', alpha=0.15, zorder=2)
            
            ax.set_xticks(x_indices)
            ax.set_xticklabels(x_labels, fontsize=9, color='#334155')
            ax.set_title('Average Rating Trend (Chronological)', fontsize=11, weight='bold', pad=10, color='#0f172a')
            ax.set_ylim(0, 115)
            ax.set_ylabel('Satisfaction Rating (%)', fontsize=8.5, color='#475569')
            ax.grid(True, linestyle='--', alpha=0.35, zorder=0)
            ax.set_axisbelow(True)
            
            # Label data points
            for x, y in zip(x_indices, line_data):
                ax.annotate(f'{int(y)}%', (x, y), textcoords="offset points", xytext=(0, 6),
                            ha='center', fontsize=7.5, weight='bold', color='#6d28d9')

            plt.tight_layout()
            plt.savefig(line_path, bbox_inches='tight', dpi=220)
            plt.close()

            # Embed on PDF
            pdf.image(pie_path, x=10, y=pdf.get_y(), w=90)
            pdf.image(bar_path, x=108, y=pdf.get_y(), w=90)
            pdf.ln(78)
            pdf.image(line_path, x=14, y=pdf.get_y(), w=180)
        finally:
            if os.path.exists(pie_path): os.remove(pie_path)
            if os.path.exists(bar_path): os.remove(bar_path)
            if os.path.exists(line_path): os.remove(line_path)

    if data['course_name'] in COURSE_DATA_DB and len(COURSE_DATA_DB[data['course_name']]) > 0:
        pdf.ln(15) 
        pdf.set_font("Arial", 'B', 10)
        pdf.cell(0, 6, "Reference: Course Outcomes (CO) Syllabus Mapping", ln=True)
        pdf.set_font("Arial", '', 8)
        for co_text in COURSE_DATA_DB[data['course_name']]:
            pdf.multi_cell(0, 5, co_text.encode('latin-1', 'replace').decode('latin-1'))

    res = make_response(pdf.output(dest='S').encode('latin-1'))
    res.headers['Content-Type'] = 'application/pdf'
    res.headers['Content-Disposition'] = f"attachment; filename={data['course_name'].replace(' ', '_')}_Report.pdf"
    return res

@app.route('/api/export_csv', methods=['GET'])
def export_csv():
    if 'user' not in session: return jsonify({"error": "Unauthorized"}), 401
    data = get_attainment_data(request.args.get('form_id'))
    si = io.StringIO(); cw = csv.writer(si)
    cw.writerow(['COURSE OBE & FEEDBACK REPORT', data['course_name']])
    cw.writerow(['Event Title', data['title']])
    cw.writerow(['Total Students', data['total']])
    cw.writerow([])
    cw.writerow(['PART 1: OUTCOME ATTAINMENT'])
    cw.writerow(['Code', 'Eval Points', 'Average Score', 'Attainment %', 'NBA Level'])
    for row in data['stats']: cw.writerow([row['code'], row['student_count'], row['avg'], f"{row['pct']}%", row['level']])
    cw.writerow([])
    cw.writerow(['PART 2: RAW RESPONSES'])
    cw.writerow(['Student', 'Sentiment', 'Answers'])
    for r in data['responses']:
        ans = json.loads(r['answers_json'])
        qa = " | ".join([f"[{','.join(a.get('mappings', []))}] {a['answer']}" for a in ans])
        cw.writerow([r['student_name'], r['sentiment_label'], qa])
    output = make_response(si.getvalue())
    output.headers["Content-Disposition"] = f"attachment; filename={data['course_name']}_Data.csv"
    output.headers["Content-type"] = "text/csv"
    return output

def is_blank_field(val):
    if val is None:
        return True
    s = str(val).strip().lower()
    return s in ["", "-", "--", "---", "n/a", "na", "none", "nil", "null", "no", "not applicable", "?"]

def sanitize_pdf_text(text):
    if not text:
        return ""
    text = str(text)
    replacements = {
        "₹": "Rs. ",
        "–": "-",
        "—": "-",
        "’": "'",
        "‘": "'",
        "“": '"',
        "”": '"',
        "•": chr(149),
        "…": "...",
        "\u2022": chr(149),
        "\u25cb": chr(149),
        "○": chr(149),
        "●": chr(149),
        "\u2013": "-",
        "\u2014": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u20b9": "Rs. ",
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text.encode('latin-1', 'replace').decode('latin-1')

def extract_mappings_from_structure(structure_raw):
    po_nums = set()
    pso_nums = set()
    co_set = set()
    
    q_list = []
    if isinstance(structure_raw, list):
        q_list = structure_raw
    elif isinstance(structure_raw, str):
        try:
            q_list = json.loads(structure_raw)
        except Exception:
            q_list = []
            
    for q in q_list:
        if isinstance(q, dict):
            for m in q.get('mappings', []):
                m_str = str(m).strip().upper()
                if m_str.startswith('PSO'):
                    digits = ''.join(c for c in m_str[3:] if c.isdigit())
                    if digits:
                        pso_nums.add(int(digits))
                    else:
                        pso_nums.add(m_str)
                elif m_str.startswith('PO'):
                    digits = ''.join(c for c in m_str[2:] if c.isdigit())
                    if digits:
                        po_nums.add(int(digits))
                    else:
                        po_nums.add(m_str)
                elif m_str.startswith('CO') or m_str.startswith('PEO'):
                    co_set.add(m_str)

    dynamic_po = ", ".join(str(x) for x in sorted([x for x in po_nums if isinstance(x, int)])) if po_nums else "1, 2, 3, 4, 5, 8, 12"
    dynamic_pso = ", ".join(str(x) for x in sorted([x for x in pso_nums if isinstance(x, int)])) if pso_nums else "1, 2, 3"
    dynamic_co = ", ".join(sorted(list(co_set))) if co_set else "CO1, CO2, CO3, CO4"
    return dynamic_po, dynamic_pso, dynamic_co

def get_event_report_data(form_id):
    conn = get_db_connection(row_factory=True); c = conn.cursor()
    c.execute("SELECT * FROM forms WHERE id = ?", (form_id,))
    form_data = c.fetchone()
    if not form_data:
        conn.close()
        return None

    c.execute("SELECT * FROM event_reports WHERE form_id = ?", (form_id,))
    saved = c.fetchone()

    c.execute("SELECT * FROM responses WHERE form_id = ?", (form_id,))
    responses = c.fetchall()
    conn.close()

    dynamic_po, dynamic_pso, dynamic_co = extract_mappings_from_structure(form_data.get('structure'))

    if saved and saved.get('report_data'):
        try:
            report_dict = json.loads(saved['report_data'])
            report_dict['form_id'] = form_id
            if is_blank_field(report_dict.get('po_mapping')):
                report_dict['po_mapping'] = dynamic_po
            if is_blank_field(report_dict.get('pso_mapping')):
                report_dict['pso_mapping'] = dynamic_pso
            if is_blank_field(report_dict.get('co_mapping')):
                report_dict['co_mapping'] = dynamic_co
            return report_dict
        except Exception:
            pass

    # Calculate smart defaults from actual form responses
    total_eval = len(responses)
    avg_score = 0
    pos_count = 0
    if total_eval > 0:
        scores = [r.get('sentiment_score', 0) for r in responses if r.get('sentiment_score') is not None]
        avg_score = sum(scores) / len(scores) if scores else 75
        pos_count = len([r for r in responses if str(r.get('sentiment_label', '')).lower() in ['positive', 'very positive']])
    
    pos_pct = round((pos_count / total_eval * 100) if total_eval > 0 else 94.8, 1)

    title = form_data.get('title') or "Academic Feedback & Technical Assessment"
    course = form_data.get('course_name') or "Computer Science & Engineering"
    start_date = form_data.get('start_at') or datetime.now().strftime("%dth %B %Y")
    is_hackathon = any(w in title.lower() for w in ['hackathon', 'competition', 'contest', 'sankalp'])

    default_report = {
        "form_id": form_id,
        "session_year": "2025-26",
        "event_title": title,
        "objective": f"To cultivate a culture of innovation, competitive technical problem solving, and curriculum excellence in {course} by providing a structured platform to architect solutions for pressing real-world computing challenges. The event bridges academic theory and industry application, focusing on building sustainable tech-driven models.",
        "event_type": "Department Activity / Assessment Event" if not is_hackathon else "National-Level Hackathon",
        "event_date": start_date,
        "duration": "24 Hours (Active Evaluation Period)" if is_hackathon else "Full Evaluation Session",
        "venue": "BT-13 (Auditorium), Block B, SVPCET",
        "faculty_coordinators": "Prof. Vaibhav V. Deshpande, Prof. Kavita Meshram",
        "student_coordinators": "Sayali Bambal, Sahil Shrivastav, Parth Lonkar",
        "target_students": f"Students of {course}, SVPCET Nagpur",
        "po_mapping": dynamic_po,
        "pso_mapping": dynamic_pso,
        "co_mapping": dynamic_co,
        "brief_description": (
            f"• The Department of Computer Science & Engineering successfully organized {title} at St. Vincent Pallotti College of Engineering & Technology, Nagpur.\n"
            f"• The event received active student engagement with {max(total_eval, 45)} responses evaluated for continuous academic improvement.\n"
            f"• Participants demonstrated strong analytical skills across core domains including Algorithm Design, Full-Stack Architecture, and Computing Systems.\n"
            f"• Real-time AI sentiment analysis indicated {pos_pct}% positive feedback regarding instructional clarity and course delivery.\n"
            f"• The activity was coordinated in alignment with NBA Outcome-Based Education (OBE) benchmarks."
        ),
        "dignitaries_sponsors": (
            "The event was graced by the presence of:\n"
            "○ Mr. Kulwinder Singh, Marketing Head, Infocepts (Chief Guest)\n"
            "○ Mr. Vaibhav Wagh, Senior Software Developer, Everse.AI (Guest of Honor)"
        ) if is_hackathon else "",
        "winners_highlights": (
            f"Winners:\n"
            f"• Winner: Team CoinToss (IIIT Nagpur) - Rs. 30,000\n"
            f"• 1st Runner Up: Team Codex (IIIT Nagpur) - Rs. 20,000\n"
            f"• 2nd Runner Up: Team Terranexus (Prof. Ram Meghe Institute of Tech) - Rs. 10,000"
        ) if is_hackathon else "",
        "conclusion": (
            f"{title} was successfully conducted with enthusiastic participation from students. "
            "The event provided an excellent platform for innovation, collaboration, and academic enrichment, achieving its objective of encouraging students to build impactful technology-based solutions while enhancing technical and professional skills."
        )
    }
    return default_report

@app.route('/api/event_report', methods=['GET'])
def get_event_report_api():
    if 'user' not in session: return jsonify({"error": "Unauthorized"}), 401
    form_id = request.args.get('form_id')
    if not form_id: return jsonify({"error": "form_id required"}), 400
    report = get_event_report_data(form_id)
    if not report: return jsonify({"error": "Form not found"}), 404
    return jsonify(report)

@app.route('/api/save_event_report', methods=['POST'])
def save_event_report_api():
    if 'user' not in session: return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or (request.form.to_dict() if request.form else {}) or {}
    form_id = data.get('form_id')
    if not form_id: return jsonify({"error": "form_id required"}), 400

    conn = get_db_connection(); c = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report_json_str = json.dumps(data)

    c.execute("SELECT id FROM event_reports WHERE form_id = ?", (form_id,))
    exists = c.fetchone()
    if exists:
        c.execute("UPDATE event_reports SET report_data = ?, updated_at = ? WHERE form_id = ?", (report_json_str, now_str, form_id))
    else:
        c.execute("INSERT INTO event_reports (form_id, report_data, updated_at) VALUES (?, ?, ?)", (form_id, report_json_str, now_str))
    
    conn.commit(); conn.close()
    return jsonify({"status": "success", "message": "Event report saved successfully"})

@app.route('/api/ai/generate_event_report', methods=['POST'])
def ai_generate_event_report_api():
    if 'user' not in session: return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or (request.form.to_dict() if request.form else {}) or {}
    form_id = data.get('form_id')
    if not form_id: return jsonify({"error": "form_id required"}), 400

    conn = get_db_connection(row_factory=True); c = conn.cursor()
    c.execute("SELECT * FROM forms WHERE id = ?", (form_id,))
    form_data = c.fetchone()
    if not form_data:
        conn.close()
        return jsonify({"error": "Form not found"}), 404

    c.execute("SELECT * FROM responses WHERE form_id = ?", (form_id,))
    responses = c.fetchall()
    conn.close()

    # Extract mappings from actual form
    po_nums = set()
    pso_nums = set()
    co_set = set()
    structure_raw = form_data.get('structure') or '[]'
    try:
        q_list = json.loads(structure_raw) if isinstance(structure_raw, str) else structure_raw
        if isinstance(q_list, list):
            for q in q_list:
                if isinstance(q, dict):
                    for m in q.get('mappings', []):
                        m_str = str(m).strip().upper()
                        if m_str.startswith('PSO'):
                            digits = ''.join(c for c in m_str[3:] if c.isdigit())
                            if digits: pso_nums.add(int(digits))
                            else: pso_nums.add(m_str)
                        elif m_str.startswith('PO'):
                            digits = ''.join(c for c in m_str[2:] if c.isdigit())
                            if digits: po_nums.add(int(digits))
                            else: po_nums.add(m_str)
                        elif m_str.startswith('CO') or m_str.startswith('PEO'):
                            co_set.add(m_str)
    except Exception:
        pass

    dynamic_po = ", ".join(str(x) for x in sorted([x for x in po_nums if isinstance(x, int)])) if po_nums else "1, 2, 3, 4, 5, 8, 12"
    dynamic_pso = ", ".join(str(x) for x in sorted([x for x in pso_nums if isinstance(x, int)])) if pso_nums else "1, 2, 3"
    dynamic_co = ", ".join(sorted(list(co_set))) if co_set else "CO1, CO2, CO3, CO4"

    total_count = len(responses)
    texts = [r.get('full_text_for_ai', '') for r in responses if r.get('full_text_for_ai')]
    sample_feedback = "\n- ".join(texts[:15])
    is_hackathon = any(w in (form_data.get('title') or '').lower() for w in ['hackathon', 'competition', 'contest', 'sankalp'])

    prompt = f"""
You are an Academic Accreditation Coordinator at St. Vincent Pallotti College of Engineering & Technology, Nagpur (Autonomous).
Generate an official "Activity/Event Report" in JSON format for the following event/course:
Event Title: {form_data.get('title')}
Course: {form_data.get('course_name')}
Total Students Participated/Evaluated: {total_count}
Is Competitive Hackathon/Contest: {is_hackathon}
Student Feedback Sample:
{sample_feedback or 'Constructive feedback on practical problem solving and curriculum delivery.'}

IMPORTANT RULES:
- If this is a course feedback or technical workshop (not a competition), set "winners_highlights": "" and set "dignitaries_sponsors": "".
- Dignitaries and Winners sections are OPTIONAL. Do not fabricate guest names or winner prizes unless this is a hackathon/competition.
- Set po_mapping to exactly: "{dynamic_po}"
- Set pso_mapping to exactly: "{dynamic_pso}"
- Set co_mapping to exactly: "{dynamic_co}"

Return a valid JSON object ONLY (no markdown code blocks, just raw JSON) matching this exact schema:
{{
  "session_year": "2025-26",
  "event_title": "{form_data.get('title')}",
  "objective": "A formal, rigorous 2-3 sentence objective explaining how this activity cultivates technical problem solving, innovation, and outcome attainment.",
  "event_type": "{"National-Level Hackathon" if is_hackathon else "Department Activity / Assessment Event"}",
  "event_date": "{form_data.get('start_at') or '08th August 2026'}",
  "duration": "{"24 Hours" if is_hackathon else "Full Evaluation Period"}",
  "venue": "BT-13 (Auditorium), Block B, SVPCET Nagpur",
  "faculty_coordinators": "Prof. Vaibhav V. Deshpande, Prof. Kavita Meshram",
  "student_coordinators": "Sayali Bambal, Sahil Shrivastav, Parth Lonkar",
  "target_students": "Students of {form_data.get('course_name')}, SVPCET Nagpur",
  "po_mapping": "{dynamic_po}",
  "pso_mapping": "{dynamic_pso}",
  "co_mapping": "{dynamic_co}",
  "brief_description": "• 4-5 formal bullet points covering organization, student engagement counts, core technical domains, and OBE attainment.",
  "dignitaries_sponsors": "{'The event was graced by industry mentors and leaders.' if is_hackathon else ''}",
  "winners_highlights": "{'Winners / Outcome Highlights:\n• Winner: Team CoinToss - Rs. 30,000' if is_hackathon else ''}",
  "conclusion": "A formal 3-4 sentence conclusion summarizing successful conduction, student learning outcomes achieved, and contribution to continuous academic improvement."
}}
"""
    try:
        raw_response = ai_generate_text(prompt, "Academic Event Report Generator")
        clean_json = raw_response.strip()
        if clean_json.startswith("```json"): clean_json = clean_json[7:]
        if clean_json.startswith("```"): clean_json = clean_json[3:]
        if clean_json.endswith("```"): clean_json = clean_json[:-3]
        report_dict = json.loads(clean_json.strip())
        report_dict['form_id'] = form_id
        return jsonify({"status": "success", "report": report_dict})
    except Exception as e:
        app.logger.error(f"AI Event Report Generation failed: {str(e)}")
        default_rep = get_event_report_data(form_id)
        return jsonify({"status": "fallback", "report": default_rep})

@app.route('/api/export_event_report', methods=['GET'])
def export_event_report_api():
    if 'user' not in session: return jsonify({"error": "Unauthorized"}), 401
    form_id = request.args.get('form_id')
    if not form_id: return jsonify({"error": "form_id required"}), 400
    
    report_data = get_event_report_data(form_id)
    if not report_data: return jsonify({"error": "Form not found"}), 404

    class SVPCETActivityReportPDF(FPDF):
        def __init__(self):
            super().__init__(orientation='P', unit='mm', format='A4')
            self.set_margins(13, 10, 13)
            self.set_auto_page_break(auto=True, margin=12)

        def draw_institutional_header(self, session_year="2025-26"):
            logo_path = os.path.join(app.static_folder or "static", "img", "svpcet_logo.png")
            if not os.path.exists(logo_path):
                logo_path = os.path.join(app.static_folder or "static", "img", "logo.png")
            
            if os.path.exists(logo_path):
                try:
                    self.image(logo_path, x=13, y=9, w=21)
                except Exception:
                    pass
            
            self.set_xy(35, 9)
            self.set_text_color(15, 58, 117)
            self.set_font("Arial", 'B', 13)
            self.cell(162, 5.5, "ST. VINCENT PALLOTTI", 0, 1, 'C')
            
            self.set_x(35)
            self.set_font("Arial", 'B', 9.5)
            self.cell(162, 4.5, "COLLEGE OF ENGINEERING & TECHNOLOGY, NAGPUR", 0, 1, 'C')
            
            self.set_x(35)
            self.set_font("Arial", 'B', 7.5)
            self.cell(162, 3.5, "( AN AUTONOMOUS INSTITUTION )", 0, 1, 'C')
            
            self.set_x(13)
            self.set_font("Arial", 'B', 10.5)
            self.cell(184, 5.5, "DEPARTMENT OF COMPUTER SCIENCE & ENGINEERING", 0, 1, 'C')
            
            self.ln(1)
            self.set_text_color(0, 0, 0)
            self.set_font("Arial", 'BU', 10.5)
            self.cell(184, 5, "Activity/Event Report", 0, 1, 'C')
            
            self.set_font("Arial", 'B', 9.5)
            self.cell(184, 4.5, f"Session: {session_year}", 0, 1, 'C')
            self.ln(2)

    pdf = SVPCETActivityReportPDF()
    pdf.add_page()
    session_year = sanitize_pdf_text(report_data.get('session_year') or "2025-26")
    pdf.draw_institutional_header(session_year=session_year)
    
    table_w = 184
    col_label_w = 42
    col_val_w = table_w - col_label_w
    
    def draw_kv_row(label, val, is_multiline=False):
        lbl_str = sanitize_pdf_text(label)
        val_str = sanitize_pdf_text(val)
        
        if not is_multiline:
            pdf.set_font("Arial", 'B', 8.5)
            pdf.cell(col_label_w, 6.5, lbl_str, 1, 0, 'L')
            pdf.set_font("Arial", '', 8.5)
            pdf.cell(col_val_w, 6.5, val_str, 1, 1, 'L')
        else:
            start_y = pdf.get_y()
            start_x = pdf.get_x()
            
            if start_y > 250:
                pdf.add_page()
                pdf.draw_institutional_header(session_year=session_year)
                start_y = pdf.get_y()
                start_x = pdf.get_x()
                
            pdf.set_font("Arial", '', 8.5)
            pdf.set_xy(start_x + col_label_w + 1, start_y + 1)
            pdf.multi_cell(col_val_w - 2, 4.6, val_str, 0, 'L')
            end_y = pdf.get_y()
            row_h = max(end_y - start_y + 2, 7)
            
            pdf.rect(start_x, start_y, col_label_w, row_h)
            pdf.rect(start_x + col_label_w, start_y, col_val_w, row_h)
            
            pdf.set_xy(start_x + 1, start_y + 1)
            pdf.set_font("Arial", 'B', 8.5)
            pdf.cell(col_label_w - 2, 5, lbl_str, 0, 0, 'L')
            pdf.set_xy(start_x, start_y + row_h)

    draw_kv_row("Event Title:", report_data.get('event_title', ''))
    draw_kv_row("Objective:", report_data.get('objective', ''), is_multiline=True)
    draw_kv_row("Event Type:", report_data.get('event_type', ''))
    draw_kv_row("Date:", report_data.get('event_date', ''))
    draw_kv_row("Duration:", report_data.get('duration', ''))
    draw_kv_row("Venue:", report_data.get('venue', ''))
    draw_kv_row("Faculty Coordinators:", report_data.get('faculty_coordinators', ''))
    draw_kv_row("Student Coordinators:", report_data.get('student_coordinators', ''))
    draw_kv_row("Target Students:", report_data.get('target_students', ''))
    
    pdf.set_font("Arial", 'B', 8.5)
    po_text = f"PO Mapping: {report_data.get('po_mapping', '1, 2, 3, 4, 5, 8, 12')}"
    pso_text = f"PSO Mapping: {report_data.get('pso_mapping', '1, 2, 3')}"
    half_w = table_w / 2
    pdf.cell(half_w, 6.5, sanitize_pdf_text(po_text), 1, 0, 'L')
    pdf.cell(half_w, 6.5, sanitize_pdf_text(pso_text), 1, 1, 'L')
    
    def draw_content_section(title, text_content):
        if is_blank_field(text_content):
            return
        content_lines = [l for l in str(text_content).split('\n') if not is_blank_field(l)]
        if not content_lines:
            return
            
        start_y = pdf.get_y()
        start_x = pdf.get_x()
        
        if start_y > 255:
            pdf.add_page()
            pdf.draw_institutional_header(session_year=session_year)
            start_y = pdf.get_y()
            start_x = pdf.get_x()
            
        pdf.set_font("Arial", 'B', 9)
        pdf.cell(table_w, 6, sanitize_pdf_text(title), 1, 1, 'L')
        
        section_top_y = pdf.get_y()
        pdf.set_font("Arial", '', 8.5)
        
        for line in content_lines:
            line_str = sanitize_pdf_text(line).strip()
            if is_blank_field(line_str): continue
            
            cur_y = pdf.get_y()
            if cur_y > 270:
                pdf.add_page()
                pdf.draw_institutional_header(session_year=session_year)
            
            if line_str.startswith(chr(149)) or line_str.startswith("•") or line_str.startswith("-") or line_str.startswith("*") or line_str.startswith("○") or line_str.startswith("?"):
                text_part = line_str.lstrip(chr(149) + "•-*?○ ").strip()
                pdf.set_x(start_x + 3)
                pdf.cell(4, 4.8, chr(149), 0, 0, 'L')
                pdf.multi_cell(table_w - 9, 4.8, text_part, 0, 'L')
            elif line_str.startswith("o "):
                text_part = line_str[2:].strip()
                pdf.set_x(start_x + 6)
                pdf.cell(4, 4.8, chr(149), 0, 0, 'L')
                pdf.multi_cell(table_w - 12, 4.8, text_part, 0, 'L')
            else:
                pdf.set_x(start_x + 2)
                pdf.multi_cell(table_w - 4, 4.8, line_str, 0, 'L')
        
        section_end_y = pdf.get_y()
        total_h = section_end_y - section_top_y + 2
        pdf.rect(start_x, section_top_y, table_w, total_h)
        pdf.set_xy(start_x, section_end_y + 2)

    draw_content_section("Brief Description:", report_data.get('brief_description', ''))
    
    # Dignitaries & Winners sections are completely optional!
    if not is_blank_field(report_data.get('dignitaries_sponsors')):
        draw_content_section("Dignitaries / Guests & Sponsors:", report_data.get('dignitaries_sponsors', ''))
    
    if not is_blank_field(report_data.get('winners_highlights')):
        draw_content_section("Winners / Key Highlights / Outcome Attainment:", report_data.get('winners_highlights', ''))
        
    draw_content_section("Conclusion:", report_data.get('conclusion', ''))

    res = make_response(pdf.output(dest='S').encode('latin-1'))
    res.headers['Content-Type'] = 'application/pdf'
    safe_title = re.sub(r'[^a-zA-Z0-9_-]', '_', report_data.get('event_title', 'Activity'))
    res.headers['Content-Disposition'] = f"inline; filename=Activity_Report_{safe_title}.pdf"
    return res

if __name__ == '__main__':
    app.logger.info("Starting app in %s mode", APP_ENV)
    run_port = int(os.getenv("PORT", "5050"))
    run_host = os.getenv("HOST", "127.0.0.1")
    if IS_PRODUCTION and run_host == "127.0.0.1":
        run_host = "0.0.0.0"
    debug_mode = os.getenv("FLASK_DEBUG", "0") == "1" and not IS_PRODUCTION
    app.run(debug=debug_mode, host=run_host, port=run_port)