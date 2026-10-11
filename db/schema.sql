BEGIN;

CREATE TABLE alembic_version (
    version_num VARCHAR(32) NOT NULL, 
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

-- Running upgrade  -> 0001

CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE users (
    id BIGSERIAL NOT NULL, 
    username VARCHAR(50) NOT NULL, 
    password_hash TEXT NOT NULL, 
    password_changed_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_users PRIMARY KEY (id), 
    CONSTRAINT uq_users_username UNIQUE (username), 
    CONSTRAINT ck_users_username_length CHECK (char_length(username) >= 3)
);

CREATE TABLE sessions (
    id BIGSERIAL NOT NULL, 
    user_id BIGINT NOT NULL, 
    token_hash CHAR(64) NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    last_seen_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    ip INET, 
    user_agent VARCHAR(255), 
    CONSTRAINT pk_sessions PRIMARY KEY (id), 
    CONSTRAINT fk_sessions_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE, 
    CONSTRAINT uq_sessions_token_hash UNIQUE (token_hash), 
    CONSTRAINT ck_sessions_token_hash_format CHECK (token_hash ~ '^[0-9a-f]{64}$')
);

CREATE INDEX ix_sessions_user_id ON sessions (user_id);

CREATE INDEX ix_sessions_expires_at ON sessions (expires_at);

CREATE TABLE series (
    id BIGSERIAL NOT NULL, 
    name VARCHAR(100) NOT NULL, 
    min_value FLOAT(53) NOT NULL, 
    max_value FLOAT(53) NOT NULL, 
    color VARCHAR(7) NOT NULL, 
    icon VARCHAR(50), 
    unit VARCHAR(20), 
    description TEXT, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_series PRIMARY KEY (id), 
    CONSTRAINT ck_series_name_not_empty CHECK (char_length(name) >= 1), 
    CONSTRAINT ck_series_min_lt_max CHECK (min_value < max_value), 
    CONSTRAINT ck_series_min_finite CHECK (min_value NOT IN ('NaN'::float8, 'Infinity'::float8, '-Infinity'::float8)), 
    CONSTRAINT ck_series_max_finite CHECK (max_value NOT IN ('NaN'::float8, 'Infinity'::float8, '-Infinity'::float8)), 
    CONSTRAINT ck_series_color_format CHECK (color ~ '^#[0-9A-Fa-f]{6}$')
);

CREATE UNIQUE INDEX uq_series_name_lower ON series (lower(name));

CREATE TABLE sensors (
    id BIGSERIAL NOT NULL, 
    series_id BIGINT NOT NULL, 
    name VARCHAR(100) NOT NULL, 
    key_hash CHAR(64) NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    revoked_at TIMESTAMP WITH TIME ZONE, 
    last_measurement_at TIMESTAMP WITH TIME ZONE, 
    CONSTRAINT pk_sensors PRIMARY KEY (id), 
    CONSTRAINT fk_sensors_series_id_series FOREIGN KEY(series_id) REFERENCES series (id) ON DELETE CASCADE, 
    CONSTRAINT ck_sensors_name_not_empty CHECK (char_length(name) >= 1), 
    CONSTRAINT ck_sensors_key_hash_format CHECK (key_hash ~ '^[0-9a-f]{64}$')
);

CREATE INDEX ix_sensors_series_id ON sensors (series_id);

CREATE UNIQUE INDEX uq_sensors_key_hash_active ON sensors (key_hash) WHERE revoked_at IS NULL;

INSERT INTO alembic_version (version_num) VALUES ('0001') RETURNING alembic_version.version_num;

-- Running upgrade 0001 -> 0002

CREATE TABLE measurements (
    id BIGSERIAL NOT NULL, 
    ts TIMESTAMP WITH TIME ZONE NOT NULL, 
    series_id BIGINT NOT NULL, 
    sensor_id BIGINT, 
    value FLOAT(53) NOT NULL, 
    received_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_measurements PRIMARY KEY (id, ts), 
    CONSTRAINT fk_measurements_series_id_series FOREIGN KEY(series_id) REFERENCES series (id) ON DELETE CASCADE, 
    CONSTRAINT fk_measurements_sensor_id_sensors FOREIGN KEY(sensor_id) REFERENCES sensors (id) ON DELETE SET NULL, 
    CONSTRAINT ck_measurements_value_finite CHECK (value NOT IN ('NaN'::float8, 'Infinity'::float8, '-Infinity'::float8))
);

SELECT create_hypertable('measurements', by_range('ts', INTERVAL '7 days'));

CREATE INDEX ix_measurements_series_id_ts ON measurements (series_id, ts);

CREATE INDEX ix_measurements_id ON measurements (id);

UPDATE alembic_version SET version_num='0002' WHERE alembic_version.version_num = '0001';

-- Running upgrade 0002 -> 0003

CREATE TABLE measurement_rejections (
    id BIGSERIAL NOT NULL, 
    series_id BIGINT NOT NULL, 
    sensor_id BIGINT, 
    value FLOAT(53) NOT NULL, 
    ts TIMESTAMP WITH TIME ZONE NOT NULL, 
    reason VARCHAR(32) NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_measurement_rejections PRIMARY KEY (id), 
    CONSTRAINT fk_measurement_rejections_series_id_series FOREIGN KEY(series_id) REFERENCES series (id) ON DELETE CASCADE, 
    CONSTRAINT fk_measurement_rejections_sensor_id_sensors FOREIGN KEY(sensor_id) REFERENCES sensors (id) ON DELETE SET NULL, 
    CONSTRAINT ck_measurement_rejections_reason_known CHECK (reason IN ('out_of_range', 'future_timestamp'))
);

CREATE INDEX ix_measurement_rejections_series_id ON measurement_rejections (series_id);

CREATE INDEX ix_measurement_rejections_created_at ON measurement_rejections (created_at DESC);

UPDATE alembic_version SET version_num='0003' WHERE alembic_version.version_num = '0002';

COMMIT;

