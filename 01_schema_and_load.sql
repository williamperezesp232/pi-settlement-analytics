-- =============================================================================
-- 01_schema_and_load.sql  |  PostgreSQL 14+
-- Personal Injury Settlement & Bottleneck Analytics
-- Run from the repo root:  psql -d pi_analytics -f sql/01_schema_and_load.sql
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS pi;

DROP TABLE IF EXISTS pi.cases CASCADE;
DROP TABLE IF EXISTS pi.report_params CASCADE;

-- Report "as of" date: drives aging of OPEN cases (set to CURRENT_DATE in production)
CREATE TABLE pi.report_params (
    as_of_date DATE NOT NULL
);
INSERT INTO pi.report_params VALUES (DATE '2026-10-13');

CREATE TABLE pi.cases (
    case_id                       VARCHAR(12)   PRIMARY KEY,
    client_name                   VARCHAR(100)  NOT NULL,
    injury_type                   VARCHAR(30)   NOT NULL
        CHECK (injury_type IN ('Motor Vehicle Accident','Slip & Fall','Dog Bite','Pedestrian')),
    case_manager                  VARCHAR(60)   NOT NULL,
    insurance_company             VARCHAR(30)   NOT NULL
        CHECK (insurance_company IN ('State Farm','GEICO','Progressive','Allstate','Liberty Mutual')),

    -- Money. NULL = not yet known (open case)
    total_medical_bills           NUMERIC(12,2) CHECK (total_medical_bills >= 0),   -- billed charges
    settlement_amount             NUMERIC(12,2) CHECK (settlement_amount  >= 0),    -- gross settlement
    attorney_fee_pct              NUMERIC(4,2)  NOT NULL
        CHECK (attorney_fee_pct IN (0.33, 0.40)),                                   -- 33% pre-suit / 40% litigation
    original_liens_amount         NUMERIC(12,2) CHECK (original_liens_amount >= 0), -- liens as asserted
    final_reduced_liens_amount    NUMERIC(12,2) CHECK (final_reduced_liens_amount >= 0), -- liens as negotiated

    -- Milestones
    intake_date                   DATE NOT NULL,
    treatment_completion_date     DATE,   -- MMI / discharge from treatment
    medical_records_received_date DATE,   -- complete records & bills in hand (EXTERNAL wait ends)
    demand_sent_date              DATE,   -- demand package to adjuster
    first_offer_date              DATE,   -- first written offer (EXTERNAL wait ends)
    settlement_accepted_date      DATE,   -- client authorizes / accepts
    closing_statement_date        DATE,   -- settlement statement signed / disbursement

    -- Business-rule integrity (NULL-safe): milestones must be chronological
    CONSTRAINT chk_dates_1 CHECK (treatment_completion_date     IS NULL OR treatment_completion_date     >= intake_date),
    CONSTRAINT chk_dates_2 CHECK (medical_records_received_date IS NULL OR treatment_completion_date IS NULL
                                  OR medical_records_received_date >= treatment_completion_date),
    CONSTRAINT chk_dates_3 CHECK (demand_sent_date              IS NULL OR medical_records_received_date IS NULL
                                  OR demand_sent_date >= medical_records_received_date),
    CONSTRAINT chk_dates_4 CHECK (first_offer_date              IS NULL OR demand_sent_date IS NULL
                                  OR first_offer_date >= demand_sent_date),
    CONSTRAINT chk_dates_5 CHECK (settlement_accepted_date      IS NULL OR first_offer_date IS NULL
                                  OR settlement_accepted_date >= first_offer_date),
    CONSTRAINT chk_dates_6 CHECK (closing_statement_date        IS NULL OR settlement_accepted_date IS NULL
                                  OR closing_statement_date >= settlement_accepted_date),
    -- A lien reduction can never increase the lien
    CONSTRAINT chk_liens   CHECK (final_reduced_liens_amount IS NULL OR original_liens_amount IS NULL
                                  OR final_reduced_liens_amount <= original_liens_amount)
);

CREATE INDEX idx_cases_manager  ON pi.cases (case_manager);
CREATE INDEX idx_cases_carrier  ON pi.cases (insurance_company);
CREATE INDEX idx_cases_intake   ON pi.cases (intake_date);

-- -----------------------------------------------------------------------------
-- Load CSV (psql client-side \copy; empty strings -> NULL)
-- -----------------------------------------------------------------------------
\copy pi.cases FROM 'data/pi_cases.csv' WITH (FORMAT csv, HEADER true, NULL '')

-- Sanity check
SELECT COUNT(*)                                            AS total_cases,
       COUNT(*) FILTER (WHERE closing_statement_date IS NULL) AS open_cases
FROM pi.cases;
