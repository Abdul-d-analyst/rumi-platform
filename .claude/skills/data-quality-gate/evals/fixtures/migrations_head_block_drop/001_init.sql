-- Destructive fixture: drops the `phone` column and the users_status_check
-- CHECK constraint entirely — should BLOCK (D-shaped: removed column,
-- removed constraint) with no change.yaml present.

CREATE TABLE schools (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) NOT NULL,
    school_id UUID REFERENCES schools(id),
    status VARCHAR(20) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT users_email_key UNIQUE (email)
);

CREATE INDEX idx_users_school_id ON users(school_id);

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
