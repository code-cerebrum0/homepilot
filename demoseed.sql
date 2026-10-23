-- ═══════════════════════════════════════════════════════════════════════════
-- HomePilot — full demo seed
-- Run:  mysql -u homepilot -p homepilot < demo_seed.sql

-- What it does:
--   1. Wipes any existing demo data for household 1 (safe to re-run)
--   2. Inserts household, tariff, 5 consumer devices + 1 solar panel
--   3. Inserts one hourly reading per device for the last 24 h  (24 × 6 = 144 rows)
--   4. Inserts 30 days of hourly history so daily/monthly tools have data
--   5. Inserts one OAuth client for MCP Inspector

-- NOTE: does NOT create the user — register via POST /api/v1/auth/register first,
--       which creates user id=1 + household id=1 + membership automatically.
-- ═══════════════════════════════════════════════════════════════════════════

USE homepilot;

-- ── 0. Guard: make sure tables exist (server must have started at least once)
SELECT 'Checking tables exist...' AS status;
SELECT COUNT(*) INTO @tbl_ok FROM information_schema.tables
  WHERE table_schema = 'homepilot' AND table_name = 'devices';
SELECT IF(@tbl_ok = 0, 'ERROR: start the server first so tables are created.', 'Tables OK') AS check_result;

-- ── 1. Clean up previous demo run (keeps user + household + membership)
DELETE FROM readings  WHERE device_id IN (SELECT id FROM devices WHERE household_id = 1);
DELETE FROM devices   WHERE household_id = 1;
DELETE FROM tariffs   WHERE household_id = 1;
DELETE FROM oauth_clients WHERE id = 'mcp-inspector';

-- ── 2. Tariff  (flat ₹8/kWh + ₹150 fixed + 18% GST)
INSERT INTO tariffs (household_id, name, currency, type, flat_rate_per_kwh, fixed_monthly_charge, tax_percent)
VALUES (1, 'Home Tariff', 'INR', 'flat', 8.00, 150.00, 18.00);

-- ── 3. Devices
INSERT INTO devices (household_id, name, source, kind, status, secret_hash, created_at) VALUES
  (1, 'Air Conditioner',  'simulated', 'consumer', 'active', 'demo-hash-1', NOW()),
  (1, 'Refrigerator',     'simulated', 'consumer', 'active', 'demo-hash-2', NOW()),
  (1, 'Water Heater',     'simulated', 'consumer', 'active', 'demo-hash-3', NOW()),
  (1, 'Washing Machine',  'simulated', 'consumer', 'active', 'demo-hash-4', NOW()),
  (1, 'Lighting',         'simulated', 'consumer', 'active', 'demo-hash-5', NOW()),
  (1, 'Solar Panels',     'solar_inverter', 'generator', 'active', 'demo-hash-6', NOW());

-- Capture device IDs (assumes they were just inserted in order)
SET @ac   = (SELECT id FROM devices WHERE household_id=1 AND name='Air Conditioner' LIMIT 1);
SET @fridge = (SELECT id FROM devices WHERE household_id=1 AND name='Refrigerator' LIMIT 1);
SET @heater = (SELECT id FROM devices WHERE household_id=1 AND name='Water Heater' LIMIT 1);
SET @wash   = (SELECT id FROM devices WHERE household_id=1 AND name='Washing Machine' LIMIT 1);
SET @lights = (SELECT id FROM devices WHERE household_id=1 AND name='Lighting' LIMIT 1);
SET @solar  = (SELECT id FROM devices WHERE household_id=1 AND name='Solar Panels' LIMIT 1);

-- ── 4. Readings — 30 days of hourly history using a recursive CTE
-- Each device gets a realistic watt profile per hour-of-day.
-- Solar uses a Gaussian bell curve (zero at night, peaks ~1400 W at noon).
-- All times stored as UTC; Asia/Kolkata is UTC+5:30 so we offset by -330 min.

INSERT INTO readings
  (device_id, measured_at, ingested_at, power_w, energy_kwh, interval_seconds, is_simulated, idempotency_key)
WITH RECURSIVE ts(t) AS (
  SELECT DATE_SUB(NOW(), INTERVAL 30 DAY)
  UNION ALL
  SELECT t + INTERVAL 1 HOUR FROM ts WHERE t < NOW()
)
SELECT
  d.id                                                         AS device_id,
  ts.t                                                         AS measured_at,
  NOW()                                                        AS ingested_at,
  -- Local hour (UTC+5:30 = add 330 min then extract hour)
  ROUND(
    CASE d.id
      -- AC: 1500 W in hot hours (10:00-22:00 local), 80 W standby at night
      WHEN @ac THEN
        CASE WHEN HOUR(ts.t + INTERVAL 330 MINUTE) BETWEEN 10 AND 22
          THEN 1400 + RAND()*200
          ELSE 70  + RAND()*30
        END
      -- Fridge: constant ~140 W with small variation
      WHEN @fridge THEN 120 + RAND()*40
      -- Water heater: spikes morning (06:00-08:00) and evening (19:00-21:00)
      WHEN @heater THEN
        CASE WHEN HOUR(ts.t + INTERVAL 330 MINUTE) BETWEEN  6 AND  8
              OR  HOUR(ts.t + INTERVAL 330 MINUTE) BETWEEN 19 AND 21
          THEN 2000 + RAND()*500
          ELSE 0
        END
      -- Washing machine: runs late morning (09:00-11:00)
      WHEN @wash THEN
        CASE WHEN HOUR(ts.t + INTERVAL 330 MINUTE) BETWEEN 9 AND 11
          THEN 400 + RAND()*150
          ELSE 0
        END
      -- Lights: evening only (18:00-23:00)
      WHEN @lights THEN
        CASE WHEN HOUR(ts.t + INTERVAL 330 MINUTE) BETWEEN 18 AND 23
          THEN 60 + RAND()*40
          ELSE 8
        END
      -- Solar: bell curve centred on noon (12:00 local), zero before 06:00 / after 18:00
      -- Gaussian: peak 1400 W, sigma≈2.5h → EXP(-((h-12)^2)/12.5)
      WHEN @solar THEN
        CASE WHEN HOUR(ts.t + INTERVAL 330 MINUTE) BETWEEN 6 AND 18
          THEN GREATEST(0, ROUND(
            1400 * EXP(-POW(HOUR(ts.t + INTERVAL 330 MINUTE) - 12, 2) / 12.5)
            * (0.85 + RAND()*0.15)   -- cloud factor: 85-100% of peak
          ))
          ELSE 0
        END
      ELSE 0
    END
  , 1)                                                         AS power_w,
  -- energy_kwh = power_w * 1h / 1000
  ROUND(
    CASE d.id
      WHEN @ac THEN
        CASE WHEN HOUR(ts.t + INTERVAL 330 MINUTE) BETWEEN 10 AND 22
          THEN (1400 + RAND()*200) / 1000
          ELSE (70   + RAND()*30)  / 1000
        END
      WHEN @fridge THEN (120 + RAND()*40) / 1000
      WHEN @heater THEN
        CASE WHEN HOUR(ts.t + INTERVAL 330 MINUTE) BETWEEN  6 AND  8
              OR  HOUR(ts.t + INTERVAL 330 MINUTE) BETWEEN 19 AND 21
          THEN (2000 + RAND()*500) / 1000
          ELSE 0
        END
      WHEN @wash THEN
        CASE WHEN HOUR(ts.t + INTERVAL 330 MINUTE) BETWEEN 9 AND 11
          THEN (400 + RAND()*150) / 1000
          ELSE 0
        END
      WHEN @lights THEN
        CASE WHEN HOUR(ts.t + INTERVAL 330 MINUTE) BETWEEN 18 AND 23
          THEN (60 + RAND()*40) / 1000
          ELSE 8 / 1000
        END
      WHEN @solar THEN
        CASE WHEN HOUR(ts.t + INTERVAL 330 MINUTE) BETWEEN 6 AND 18
          THEN GREATEST(0, ROUND(
            1400 * EXP(-POW(HOUR(ts.t + INTERVAL 330 MINUTE) - 12, 2) / 12.5)
            * (0.85 + RAND()*0.15)
          )) / 1000
          ELSE 0
        END
      ELSE 0
    END
  , 4)                                                         AS energy_kwh,
  3600                                                         AS interval_seconds,
  1                                                            AS is_simulated,
  CONCAT('demo-', d.id, '-', UNIX_TIMESTAMP(ts.t))            AS idempotency_key
FROM ts
CROSS JOIN devices d
WHERE d.household_id = 1;

-- ── 5. Update last_seen_at on all devices
UPDATE devices SET last_seen_at = NOW() WHERE household_id = 1;

-- ── 6. OAuth client for MCP Inspector
INSERT INTO oauth_clients (id, name, secret_hash, redirect_uris, is_public, created_at)
VALUES (
  'mcp-inspector',
  'MCP Inspector',
  NULL,
  JSON_ARRAY('https://inspector.modelcontextprotocol.io/callback'),
  1,
  NOW()
);

-- ── 7. Summary
SELECT
  (SELECT COUNT(*) FROM devices  WHERE household_id = 1)          AS devices_created,
  (SELECT COUNT(*) FROM readings WHERE device_id IN
    (SELECT id FROM devices WHERE household_id = 1))               AS readings_inserted,
  (SELECT SUM(energy_kwh) FROM readings WHERE device_id = @solar)  AS solar_kwh_total,
  (SELECT SUM(energy_kwh) FROM readings WHERE device_id IN
    (SELECT id FROM devices WHERE household_id=1 AND kind='consumer')) AS consumer_kwh_total;