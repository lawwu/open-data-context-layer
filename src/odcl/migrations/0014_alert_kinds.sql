-- Alerts also tell of data a project uses that is failing a data-quality test.
ALTER TABLE alert_deliveries DROP CONSTRAINT alert_deliveries_kind_check;
ALTER TABLE alert_deliveries ADD CONSTRAINT alert_deliveries_kind_check CHECK (kind IN ('changed', 'retired', 'failing'));
