-- Initial migration: create tables used by the app

CREATE TABLE IF NOT EXISTS forms (
    id BIGSERIAL PRIMARY KEY,
    title TEXT,
    course_name TEXT,
    structure TEXT,
    created_at TEXT,
    start_at TEXT,
    end_at TEXT,
    public_token TEXT,
    is_active BOOLEAN DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS responses (
    id BIGSERIAL PRIMARY KEY,
    form_id INTEGER,
    form_title TEXT,
    student_name TEXT,
    attendance INTEGER,
    answers_json TEXT,
    full_text_for_ai TEXT,
    sentiment_score DOUBLE PRECISION,
    sentiment_label TEXT,
    timestamp TEXT
);

CREATE TABLE IF NOT EXISTS submission_locks (
    id BIGSERIAL PRIMARY KEY,
    form_id INTEGER NOT NULL,
    submitter_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(form_id, submitter_key)
);

CREATE TABLE IF NOT EXISTS processed_responses (
    id BIGSERIAL PRIMARY KEY,
    form_id INTEGER,
    processed_at TIMESTAMP,
    token_count INTEGER,
    payload_json TEXT
);
