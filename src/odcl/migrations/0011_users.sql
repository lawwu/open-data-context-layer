-- Users, their API tokens, and the browser sessions of people who signed in through single sign-on.
-- A user's name is what revisions record as their actor. Tokens and session ids are kept only as SHA-256 digests.

CREATE TABLE users (
  name            text COLLATE "C" PRIMARY KEY,
  display_name    text,
  email           text,
  role            text NOT NULL CHECK (role IN ('reader', 'editor', 'importer', 'admin')),
  oidc_issuer     text,
  oidc_subject    text,
  last_sign_in_at timestamptz,
  disabled_at     timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  created_by      text NOT NULL,
  UNIQUE (oidc_issuer, oidc_subject)
);

CREATE TABLE user_tokens (
  id           text PRIMARY KEY,
  user_name    text COLLATE "C" NOT NULL REFERENCES users (name),
  digest       text NOT NULL UNIQUE,
  label        text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  created_by   text NOT NULL,
  last_used_at timestamptz,
  revoked_at   timestamptz
);
CREATE INDEX user_tokens_user ON user_tokens (user_name);

CREATE TABLE sessions (
  digest     text PRIMARY KEY,
  user_name  text COLLATE "C" NOT NULL REFERENCES users (name),
  expires_at timestamptz NOT NULL
);
