-- Narrowing fixture: shrinks phone from VARCHAR(20) to VARCHAR(10) —
-- should BLOCK (can truncate existing values).

CREATE TABLE schools (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) NOT NULL,
    phone VARCHAR(10),
    school_id UUID REFERENCES schools(id),
    status VARCHAR(20) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT users_email_key UNIQUE (email),
    CONSTRAINT users_status_check CHECK (status IN ('active', 'inactive', 'pending'))
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
