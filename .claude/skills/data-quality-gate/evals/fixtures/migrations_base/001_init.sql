-- Synthetic baseline fixture for the Data Quality Gate's own tests — not
-- real Taleemabad schema. Mirrors the spirit of
-- skills/data-standards/evals/fixtures/*.sql but this gate needs REAL
-- Postgres to introspect, so these are applied to a throwaway DB rather
-- than parsed as text.

CREATE TABLE schools (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) NOT NULL,
    phone VARCHAR(20),
    school_id UUID REFERENCES schools(id),
    status VARCHAR(20) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT users_email_key UNIQUE (email),
    CONSTRAINT users_status_check CHECK (status IN ('active', 'inactive', 'pending'))
);

CREATE INDEX idx_users_school_id ON users(school_id);

-- A composite-FK table, purely to exercise introspect.py's FK
-- pg_constraint/conkey-confkey path in CI against a real server (regression
-- coverage for the composite-FK cartesian-product bug found in review —
-- see reference/snapshot-schema.md).
CREATE TABLE school_terms (
    school_id UUID NOT NULL,
    term_code VARCHAR(10) NOT NULL,
    label TEXT NOT NULL,
    PRIMARY KEY (school_id, term_code)
);

CREATE TABLE enrollments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    school_id UUID NOT NULL,
    term_code VARCHAR(10) NOT NULL,
    user_id UUID REFERENCES users(id),
    CONSTRAINT fk_enrollments_school_term
        FOREIGN KEY (school_id, term_code) REFERENCES school_terms(school_id, term_code)
);
