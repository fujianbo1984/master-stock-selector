CREATE TABLE IF NOT EXISTS user_account (
    user_id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'DISABLED')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS user_session (
    session_id_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    csrf_secret TEXT NOT NULL,
    created_at_epoch INTEGER NOT NULL,
    last_seen_epoch INTEGER NOT NULL,
    expires_at_epoch INTEGER NOT NULL,
    revoked_at_epoch INTEGER,
    FOREIGN KEY (user_id) REFERENCES user_account(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_user_session_user_expiry
ON user_session(user_id, expires_at_epoch);
CREATE TABLE IF NOT EXISTS user_api_token (
    token_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    token_prefix TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    scopes_json TEXT NOT NULL,
    created_at_epoch INTEGER NOT NULL,
    expires_at_epoch INTEGER NOT NULL,
    last_used_epoch INTEGER,
    revoked_at_epoch INTEGER,
    FOREIGN KEY (user_id) REFERENCES user_account(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_user_api_token_user
ON user_api_token(user_id, created_at_epoch DESC);
CREATE TABLE IF NOT EXISTS user_trade_batch (
    batch_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, idempotency_key),
    FOREIGN KEY (user_id) REFERENCES user_account(user_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS user_api_audit (
    audit_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    action TEXT NOT NULL,
    request_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES user_account(user_id) ON DELETE CASCADE,
    FOREIGN KEY (token_id) REFERENCES user_api_token(token_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_user_api_audit_user_created
ON user_api_audit(user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS user_stock_review (
    user_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    manual_state TEXT NOT NULL CHECK (
        manual_state IN ('WATCH', 'FOCUS', 'ARCHIVED')
    ),
    note TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, symbol),
    FOREIGN KEY (user_id) REFERENCES user_account(user_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS user_trade_execution (
    execution_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    traded_on TEXT NOT NULL,
    traded_at TEXT NOT NULL DEFAULT '',
    source_ref TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    price REAL NOT NULL CHECK (price > 0),
    fee REAL NOT NULL DEFAULT 0 CHECK (fee >= 0),
    method TEXT NOT NULL CHECK (method IN ('WEINSTEIN', 'MINERVINI', 'MANUAL')),
    setup_method TEXT NOT NULL DEFAULT 'PULLBACK_SUPPORT'
        CHECK (setup_method IN (
            'FAILED_TEST', 'PULLBACK_SUPPORT', 'LOWER_TIMEFRAME_BREAKOUT',
            'COMPLEX_PULLBACK', 'ANTI', 'PRE_BREAKOUT_BASE',
            'POST_BREAKOUT_FIRST_PULLBACK', 'FAILED_BREAKOUT'
        )),
    stop_price REAL CHECK (stop_price IS NULL OR stop_price > 0),
    rationale TEXT NOT NULL DEFAULT '',
    invalidation TEXT NOT NULL DEFAULT '',
    exit_reason TEXT NOT NULL DEFAULT '',
    market_context TEXT NOT NULL DEFAULT '',
    observation_snapshot_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    FOREIGN KEY (user_id) REFERENCES user_account(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_user_trade_date
ON user_trade_execution(user_id, traded_on, created_at);
CREATE INDEX IF NOT EXISTS idx_user_trade_symbol
ON user_trade_execution(user_id, symbol, traded_on, created_at);
CREATE TABLE IF NOT EXISTS user_chart_drawing (
    drawing_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price_scale_id TEXT NOT NULL,
    tool TEXT NOT NULL CHECK (tool IN ('trendline', 'horizontal')),
    anchors_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES user_account(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_user_chart_symbol
ON user_chart_drawing(user_id, symbol, price_scale_id, created_at);
CREATE TABLE IF NOT EXISTS user_access_log (
    access_id TEXT PRIMARY KEY,
    occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    user_id TEXT,
    client_ip TEXT NOT NULL,
    country_code TEXT NOT NULL DEFAULT '',
    country_name TEXT NOT NULL DEFAULT '',
    region_name TEXT NOT NULL DEFAULT '',
    city_name TEXT NOT NULL DEFAULT '',
    location_source TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES user_account(user_id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_user_access_occurred
ON user_access_log(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_access_user_occurred
ON user_access_log(user_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_access_path_occurred
ON user_access_log(path, occurred_at DESC);
PRAGMA user_version=4;
