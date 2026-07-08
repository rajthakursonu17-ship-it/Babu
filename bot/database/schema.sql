-- Shriji Institute Telegram Bot – Postgres schema

CREATE TABLE IF NOT EXISTS users (
    telegram_id           BIGINT PRIMARY KEY,
    full_name             TEXT,
    telegram_username     TEXT,
    edu_username          TEXT,
    edu_password          TEXT,
    purchased_batches     BIGINT[] DEFAULT '{}',
    trial_start           TIMESTAMPTZ,
    trial_active          BOOLEAN DEFAULT TRUE,
    trial_open_count      INTEGER DEFAULT 0,
    paid_open_count       JSONB   DEFAULT '{}'::jsonb,   -- {batch_id: count}
    referral_code         TEXT UNIQUE,
    referred_by           BIGINT,
    referral_bonus_hours  INTEGER DEFAULT 0,
    joined_at             TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id      BIGSERIAL PRIMARY KEY,
    batch_code    TEXT UNIQUE NOT NULL,
    name          TEXT NOT NULL,
    description   TEXT,
    price         NUMERIC(10,2) DEFAULT 0,
    image_file_id TEXT,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS subjects (
    subject_id BIGSERIAL PRIMARY KEY,
    batch_id   BIGINT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
    name       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chapters (
    chapter_id BIGSERIAL PRIMARY KEY,
    subject_id BIGINT NOT NULL REFERENCES subjects(subject_id) ON DELETE CASCADE,
    name       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lectures (
    lecture_id BIGSERIAL PRIMARY KEY,
    chapter_id BIGINT NOT NULL REFERENCES chapters(chapter_id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    message_id BIGINT,
    channel_id BIGINT,
    pdf_link   TEXT,
    dpp_link   TEXT,
    pdf_message_id BIGINT,
    dpp_message_id BIGINT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(channel_id, message_id)
);
-- add columns for legacy DBs
ALTER TABLE lectures ADD COLUMN IF NOT EXISTS pdf_message_id BIGINT;
ALTER TABLE lectures ADD COLUMN IF NOT EXISTS dpp_message_id BIGINT;

CREATE TABLE IF NOT EXISTS user_lecture_access (
    id              BIGSERIAL PRIMARY KEY,
    telegram_id     BIGINT NOT NULL,
    lecture_id      BIGINT NOT NULL REFERENCES lectures(lecture_id) ON DELETE CASCADE,
    batch_id        BIGINT,
    sent_message_id BIGINT,
    accessed_at     TIMESTAMPTZ DEFAULT NOW(),
    delete_at       TIMESTAMPTZ,
    sequence_number BIGINT,
    deleted         BOOLEAN DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_ula_user_batch ON user_lecture_access(telegram_id, batch_id) WHERE deleted = FALSE;
CREATE INDEX IF NOT EXISTS idx_ula_delete_at  ON user_lecture_access(delete_at) WHERE deleted = FALSE;

CREATE TABLE IF NOT EXISTS referrals (
    id             BIGSERIAL PRIMARY KEY,
    referrer_id    BIGINT NOT NULL,
    referred_id    BIGINT NOT NULL UNIQUE,
    bonus_applied  BOOLEAN DEFAULT FALSE,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS scan_jobs (
    job_id                 BIGSERIAL PRIMARY KEY,
    batch_id               BIGINT REFERENCES batches(batch_id) ON DELETE CASCADE,
    subject_id             BIGINT REFERENCES subjects(subject_id) ON DELETE SET NULL,
    channel_id             BIGINT,
    status                 TEXT DEFAULT 'running',
    total_found            INTEGER DEFAULT 0,
    total_processed        INTEGER DEFAULT 0,
    last_message_id_scanned BIGINT DEFAULT 0,
    started_at             TIMESTAMPTZ DEFAULT NOW(),
    completed_at           TIMESTAMPTZ,
    log                    TEXT
);

CREATE TABLE IF NOT EXISTS pending_payments (
    id           BIGSERIAL PRIMARY KEY,
    telegram_id  BIGINT NOT NULL,
    batch_id     BIGINT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
    status       TEXT DEFAULT 'pending',
    created_at   TIMESTAMPTZ DEFAULT NOW()
);
