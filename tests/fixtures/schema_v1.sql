
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, created_utc TEXT NOT NULL, ended_utc TEXT,
                state TEXT NOT NULL, team TEXT NOT NULL, client_id TEXT NOT NULL,
                competition TEXT NOT NULL, access_code TEXT NOT NULL,
                target_revision INTEGER NOT NULL, target_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rounds (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                number INTEGER NOT NULL, created_utc TEXT NOT NULL, received_utc TEXT,
                case_name TEXT NOT NULL, expected TEXT NOT NULL,
                timeout_ms INTEGER NOT NULL, action TEXT NOT NULL,
                target_revision INTEGER NOT NULL, target_json TEXT NOT NULL,
                notes TEXT NOT NULL, status TEXT NOT NULL,
                actual TEXT, matched INTEGER, elapsed_ms REAL,
                retries INTEGER NOT NULL DEFAULT 0, duplicates INTEGER NOT NULL DEFAULT 0,
                protocol_errors INTEGER NOT NULL DEFAULT 0, disconnects INTEGER NOT NULL DEFAULT 0,
                UNIQUE(session_id, number)
            );
            CREATE TABLE IF NOT EXISTS receipts (
                session_id TEXT NOT NULL, msg_id TEXT NOT NULL,
                round_id TEXT NOT NULL REFERENCES rounds(id), fingerprint TEXT NOT NULL,
                response_json TEXT NOT NULL, created_utc TEXT NOT NULL,
                PRIMARY KEY(session_id, msg_id)
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, at_utc TEXT NOT NULL,
                session_id TEXT, round_id TEXT, peer TEXT, direction TEXT NOT NULL,
                kind TEXT NOT NULL, text TEXT NOT NULL, raw_b64 TEXT
            );
            CREATE INDEX IF NOT EXISTS audit_session ON audit(session_id, id);
            CREATE INDEX IF NOT EXISTS rounds_session ON rounds(session_id, number);
            PRAGMA user_version=1;

