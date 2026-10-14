-- =============================================================================
-- 02_metrics_view.sql  |  Business logic layer: pi.v_case_metrics
-- One row per case. Power BI / Excel / Python all read from this view.
-- In PostgreSQL, DATE - DATE returns an INTEGER number of days.
-- =============================================================================

CREATE OR REPLACE VIEW pi.v_case_metrics AS
WITH p AS (SELECT as_of_date FROM pi.report_params LIMIT 1),

phases AS (
    SELECT
        c.*,
        p.as_of_date,

        -- ---- Phase durations (completed phases only; NULL if milestone pending) ----
        (c.treatment_completion_date     - c.intake_date)                      AS treatment_days,        -- client/medical driven
        (c.medical_records_received_date - c.treatment_completion_date)        AS records_wait_days,     -- EXTERNAL: providers
        (c.demand_sent_date              - c.medical_records_received_date)    AS demand_prep_days,      -- CONTROLLABLE: firm
        (c.first_offer_date              - c.demand_sent_date)                 AS offer_wait_days,       -- EXTERNAL: adjuster
        (c.settlement_accepted_date      - c.first_offer_date)                 AS negotiation_days,      -- strategic / shared
        (c.closing_statement_date        - c.settlement_accepted_date)         AS closing_days,          -- CONTROLLABLE: firm
        (c.demand_sent_date              - c.intake_date)                      AS intake_to_demand_days, -- gross (includes treatment + records wait)
        (c.closing_statement_date        - c.intake_date)                      AS total_cycle_days,

        -- ---- Ongoing waits for OPEN cases (aging against as_of_date) ----
        CASE WHEN c.treatment_completion_date IS NOT NULL AND c.medical_records_received_date IS NULL
             THEN p.as_of_date - c.treatment_completion_date END               AS records_wait_open_days,
        CASE WHEN c.demand_sent_date IS NOT NULL AND c.first_offer_date IS NULL
             THEN p.as_of_date - c.demand_sent_date END                        AS offer_wait_open_days
    FROM pi.cases c
    CROSS JOIN p
)

SELECT
    ph.*,

    -- ======================= Case status / stage =======================
    CASE WHEN closing_statement_date IS NOT NULL THEN 'Closed' ELSE 'Open' END AS case_status,
    CASE
        WHEN closing_statement_date        IS NOT NULL THEN '7. Closed'
        WHEN settlement_accepted_date      IS NOT NULL THEN '6. Closing / Disbursement'
        WHEN first_offer_date              IS NOT NULL THEN '5. Negotiation'
        WHEN demand_sent_date              IS NOT NULL THEN '4. Awaiting Adjuster Offer'
        WHEN medical_records_received_date IS NOT NULL THEN '3. Demand Preparation'
        WHEN treatment_completion_date     IS NOT NULL THEN '2. Awaiting Records/Bills'
        ELSE                                                  '1. In Treatment'
    END AS current_stage,

    -- ======================= Controllable vs External =======================
    -- Controllable = firm-owned work: demand prep + settlement-to-closing (disbursement)
    (demand_prep_days + closing_days)                       AS controllable_days,
    -- External = waiting on third parties: providers (records/bills) + adjuster (first offer)
    (records_wait_days + offer_wait_days)                   AS external_wait_days,
    ROUND(100.0 * (demand_prep_days + closing_days) / NULLIF(total_cycle_days, 0), 1) AS controllable_pct_of_cycle,
    ROUND(100.0 * (records_wait_days + offer_wait_days) / NULLIF(total_cycle_days, 0), 1) AS external_pct_of_cycle,

    -- ======================= Lien reduction =======================
    (original_liens_amount - final_reduced_liens_amount)                         AS lien_reduction_savings,
    ROUND(100.0 * (original_liens_amount - final_reduced_liens_amount)
          / NULLIF(original_liens_amount, 0), 1)                                 AS lien_reduction_pct,

    -- ======================= Fees & client recovery =======================
    -- Fee earned on GROSS settlement (case costs are pass-through; not modeled in this dataset)
    ROUND(settlement_amount * attorney_fee_pct, 2)                               AS net_attorney_fee,
    -- Client net before case costs = settlement - fee - final liens
    ROUND(settlement_amount - settlement_amount * attorney_fee_pct
          - final_reduced_liens_amount, 2)                                       AS client_net_recovery_before_costs,

    -- ======================= Stagnation alert (> 45 days) =======================
    -- Records wait OR adjuster response wait > 45 days.
    -- Closed/advanced cases: flags a HISTORICAL stall. Open cases still waiting: ACTIVE alert.
    COALESCE(COALESCE(records_wait_days, records_wait_open_days) > 45
          OR COALESCE(offer_wait_days, offer_wait_open_days)  > 45, FALSE)       AS stagnant_case_flag,

    (COALESCE(records_wait_open_days, 0) > 45
     OR COALESCE(offer_wait_open_days, 0) > 45)                                  AS active_stagnation_alert,

    CASE
        WHEN COALESCE(records_wait_days, records_wait_open_days) > 45
         AND COALESCE(offer_wait_days,  offer_wait_open_days)  > 45 THEN 'Records + Adjuster'
        WHEN COALESCE(records_wait_days, records_wait_open_days) > 45 THEN 'Records/Bills'
        WHEN COALESCE(offer_wait_days,  offer_wait_open_days)  > 45 THEN 'Adjuster Offer'
    END AS stagnation_type
FROM phases ph;

-- Quick check
-- SELECT current_stage, COUNT(*) FROM pi.v_case_metrics GROUP BY 1 ORDER BY 1;
