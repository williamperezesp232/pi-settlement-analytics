-- =============================================================================
-- 03_kpi_queries.sql  |  Executive KPI queries on pi.v_case_metrics
-- =============================================================================

-- Q1. Executive snapshot ------------------------------------------------------
SELECT COUNT(*)                                                       AS total_cases,
       COUNT(*) FILTER (WHERE case_status = 'Closed')                 AS closed_cases,
       COUNT(*) FILTER (WHERE case_status = 'Open')                   AS open_cases,
       COUNT(*) FILTER (WHERE active_stagnation_alert)                AS active_stagnation_alerts,
       ROUND(AVG(total_cycle_days), 0)                                AS avg_cycle_days,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY total_cycle_days)  AS median_cycle_days,
       ROUND(AVG(controllable_pct_of_cycle), 1)                       AS avg_controllable_pct,
       ROUND(AVG(external_pct_of_cycle), 1)                           AS avg_external_pct,
       SUM(settlement_amount)                                         AS gross_settlements,
       SUM(net_attorney_fee)                                          AS total_attorney_fees,
       ROUND(100.0 * SUM(lien_reduction_savings) / NULLIF(SUM(original_liens_amount) FILTER (WHERE closing_statement_date IS NOT NULL), 0), 1)
                                                                      AS dollar_weighted_lien_reduction_pct
FROM pi.v_case_metrics;

-- Q2. Where does the time go? (average days per phase, closed cases) -----------
SELECT ROUND(AVG(treatment_days), 1)   AS treatment_days_client_medical,
       ROUND(AVG(records_wait_days),1) AS records_wait_external,
       ROUND(AVG(demand_prep_days), 1) AS demand_prep_controllable,
       ROUND(AVG(offer_wait_days), 1)  AS adjuster_wait_external,
       ROUND(AVG(negotiation_days), 1) AS negotiation_days,
       ROUND(AVG(closing_days), 1)     AS closing_controllable
FROM pi.v_case_metrics
WHERE case_status = 'Closed';

-- Q3. Case manager scorecard (internal efficiency + lien results) --------------
SELECT case_manager,
       COUNT(*)                                            AS cases,
       ROUND(AVG(demand_prep_days), 1)                     AS avg_demand_prep_days,
       ROUND(AVG(closing_days), 1)                         AS avg_closing_days,
       ROUND(AVG(controllable_days), 1)                    AS avg_controllable_days,
       ROUND(100.0 * SUM(lien_reduction_savings)
             / NULLIF(SUM(original_liens_amount) FILTER (WHERE closing_statement_date IS NOT NULL), 0), 1) AS lien_reduction_pct_weighted,
       SUM(lien_reduction_savings)                         AS lien_savings_usd,
       SUM(net_attorney_fee)                               AS fees_usd
FROM pi.v_case_metrics
GROUP BY case_manager
ORDER BY avg_controllable_days;

-- Q4. Carrier response time (external) and settlement profile -------------------
SELECT insurance_company,
       COUNT(offer_wait_days)                                       AS cases_with_offer,
       ROUND(AVG(offer_wait_days), 1)                               AS avg_days_demand_to_first_offer,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY offer_wait_days) AS median_days_to_offer,
       ROUND(100.0 * COUNT(*) FILTER (WHERE offer_wait_days > 45)
             / NULLIF(COUNT(offer_wait_days), 0), 1)                AS pct_offers_over_45d,
       ROUND(AVG(settlement_amount), 0)                             AS avg_settlement
FROM pi.v_case_metrics
GROUP BY insurance_company
ORDER BY avg_days_demand_to_first_offer DESC;

-- Q5. Injury type economics -----------------------------------------------------
SELECT injury_type,
       COUNT(*)                          AS cases,
       ROUND(AVG(total_medical_bills),0) AS avg_billed,
       ROUND(AVG(settlement_amount), 0)  AS avg_settlement,
       ROUND(AVG(total_cycle_days), 0)   AS avg_cycle_days,
       ROUND(100.0 * COUNT(*) FILTER (WHERE attorney_fee_pct = 0.40) / COUNT(*), 1) AS pct_litigated_40
FROM pi.v_case_metrics
GROUP BY injury_type
ORDER BY cases DESC;

-- Q6. ACTIVE stagnation alerts: open cases to work TODAY ---------------------------
SELECT case_id, client_name, case_manager, insurance_company, current_stage, stagnation_type,
       COALESCE(records_wait_open_days, offer_wait_open_days) AS days_without_progress
FROM pi.v_case_metrics
WHERE active_stagnation_alert
ORDER BY days_without_progress DESC;

-- Q7. Historical stagnation rate by bottleneck type -------------------------------
SELECT stagnation_type, COUNT(*) AS cases,
       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct_of_stagnant
FROM pi.v_case_metrics
WHERE stagnant_case_flag
GROUP BY stagnation_type
ORDER BY cases DESC;

-- Q8. Lien reduction: does larger lien = larger fight? (buckets, closed cases) -------
SELECT CASE WHEN original_liens_amount = 0     THEN '0. No liens'
            WHEN original_liens_amount < 5000  THEN '1. < $5K'
            WHEN original_liens_amount < 15000 THEN '2. $5K-$15K'
            ELSE                                    '3. > $15K' END AS lien_bucket,
       COUNT(*)                                    AS cases,
       SUM(lien_reduction_savings)                 AS savings_usd,
       ROUND(100.0 * SUM(lien_reduction_savings) / NULLIF(SUM(original_liens_amount), 0), 1) AS reduction_pct,
       ROUND(AVG(closing_days), 1)                 AS avg_closing_days
FROM pi.v_case_metrics
WHERE case_status = 'Closed'
GROUP BY 1
ORDER BY 1;

-- Q9. Monthly closings and fee revenue ----------------------------------------------
SELECT DATE_TRUNC('month', closing_statement_date)::date AS closing_month,
       COUNT(*)                                          AS closed_cases,
       SUM(settlement_amount)                            AS gross_settlements,
       SUM(net_attorney_fee)                             AS attorney_fees
FROM pi.v_case_metrics
WHERE case_status = 'Closed'
GROUP BY 1
ORDER BY 1;
