-- Alerts: who is told when data a project uses changes after the project used it, or is gone from its source.
-- A subscription covers one project, the projects of one owner, or every project, and is sent to a webhook, a Slack
-- incoming webhook or an email address. A delivery is recorded per subscription, project, entry and state of the
-- entry, so each change is sent once; a failed one is tried again on the next run.

CREATE TABLE alert_subscriptions (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  scope        text NOT NULL CHECK (scope IN ('project', 'owner', 'all')),
  target       text,
  channel      text NOT NULL CHECK (channel IN ('webhook', 'slack', 'email')),
  address      text NOT NULL,
  min_severity text NOT NULL DEFAULT 'semantic' CHECK (min_severity IN ('cosmetic', 'additive', 'semantic', 'breaking')),
  created_by   text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  CHECK ((scope = 'all') = (target IS NULL))
);

CREATE TABLE alert_deliveries (
  subscription_id bigint NOT NULL REFERENCES alert_subscriptions (id) ON DELETE CASCADE,
  project_key     text COLLATE "C" NOT NULL,
  entry_key       text COLLATE "C" NOT NULL,
  entry_state     text NOT NULL,
  kind            text NOT NULL CHECK (kind IN ('changed', 'retired')),
  severity        text NOT NULL,
  attempted_at    timestamptz NOT NULL DEFAULT now(),
  sent_at         timestamptz,
  error           text,
  PRIMARY KEY (subscription_id, project_key, entry_key, entry_state)
);
