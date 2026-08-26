CREATE TABLE IF NOT EXISTS users(
  user_id BIGINT PRIMARY KEY,
  username TEXT,
  first_name TEXT,
  last_name TEXT,
  phone TEXT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','blocked')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);
CREATE INDEX IF NOT EXISTS idx_users_updated_at ON users(updated_at DESC);

CREATE TABLE IF NOT EXISTS admin_state(
  user_id BIGINT PRIMARY KEY,
  awaiting_broadcast BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS temporary_bot_messages(
  chat_id BIGINT NOT NULL,
  message_id BIGINT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY(chat_id,message_id)
);

CREATE INDEX IF NOT EXISTS idx_temporary_bot_messages_created_at
  ON temporary_bot_messages(created_at);

CREATE TABLE IF NOT EXISTS admin_notifications(
  user_id BIGINT NOT NULL,
  admin_chat_id BIGINT NOT NULL,
  message_id BIGINT NOT NULL,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY(user_id,admin_chat_id,message_id)
);

CREATE INDEX IF NOT EXISTS idx_admin_notifications_active
  ON admin_notifications(user_id,active);

CREATE TABLE IF NOT EXISTS service_rate_limits(
  service TEXT PRIMARY KEY,
  next_allowed_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS broadcast_jobs(
  job_id UUID PRIMARY KEY,
  admin_chat_id BIGINT NOT NULL,
  public_url TEXT NOT NULL,
  message_text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','complete')),
  fanout_complete BOOLEAN NOT NULL DEFAULT FALSE,
  total INTEGER NOT NULL DEFAULT 0,
  delivered INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS broadcast_deliveries(
  job_id UUID NOT NULL REFERENCES broadcast_jobs(job_id) ON DELETE CASCADE,
  user_id BIGINT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','sending','sent','failed')),
  attempts INTEGER NOT NULL DEFAULT 0,
  lease_until TIMESTAMPTZ,
  last_error TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY(job_id,user_id)
);

CREATE INDEX IF NOT EXISTS idx_broadcast_deliveries_status
  ON broadcast_deliveries(job_id,status);
