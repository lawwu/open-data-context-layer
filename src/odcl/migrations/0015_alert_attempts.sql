-- How often an alert delivery has been tried, and since when. A delivery that keeps failing is tried on each of the
-- first runs, then less often (see odcl.alerts); its subscription says it is failing until one gets through.
ALTER TABLE alert_deliveries
  ADD COLUMN attempts integer NOT NULL DEFAULT 1 CHECK (attempts >= 1),
  ADD COLUMN first_attempted_at timestamptz;
UPDATE alert_deliveries SET first_attempted_at = attempted_at;
ALTER TABLE alert_deliveries ALTER COLUMN first_attempted_at SET NOT NULL, ALTER COLUMN first_attempted_at SET DEFAULT now();

-- the deliveries still owed, per subscription: what says a subscription is failing
CREATE INDEX alert_deliveries_unsent ON alert_deliveries (subscription_id) WHERE sent_at IS NULL;
