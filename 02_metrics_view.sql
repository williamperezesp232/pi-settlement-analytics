-- =============================================================================
-- 02_metrics_view.sql
-- Proyecto : Legal Operations & Analytics - Personal Injury (Plaintiff-side)
-- Requiere : 01_schema_and_load.sql (pi.pi_cases y pi.report_params)
--
-- Crea:
--   pi.v_case_phases          -> 1 fila por caso: las 9 fases, clasificación, montos
--   pi.v_reconciliation_check -> 1 fila resumen: DEBE mostrar 0 diferencias
--
-- ─────────────────────────────────────────────────────────────────────────────
-- FÓRMULA EXACTA DE RECONCILIACIÓN (camino crítico)
-- ─────────────────────────────────────────────────────────────────────────────
--   ready = GREATEST(settlement_check_received, reductions_confirmed)
--
--   1 treatment            = treatment_completion  - intake
--   2 records_wait         = records_received      - treatment_completion
--   3 demand_prep          = demand_sent           - records_received
--   4 offer_wait           = first_offer           - demand_sent
--   5 negotiation          = settlement_accepted   - first_offer
--   6 release_execution    = release_SIGNED        - settlement_ACCEPTED   <-- incluye el
--                                                    desfase accepted -> release_sent
--   7 carrier_check_wait   = check_received        - release_signed
--   8 reductions_blocking  = GREATEST(0, reductions_confirmed - check_received)
--                          = ready - check_received
--   9 disbursement_prep    = disbursement          - ready
--
--   Suma telescópica:  (1..5) = accepted - intake
--                      + 6 + 7 = check - intake
--                      + 8     = ready - intake
--                      + 9     = disbursement - intake        == total_cycle_days  ✔
--
--   Errores típicos que producen diferencias de varios días:
--     * release_execution = signed - SENT  (omite accepted -> sent)
--     * reductions_blocking = reductions - accepted (cuenta la pista paralela completa;
--       esa es 'reductions_parallel_wait_days', un dato informativo, NO una fase)
--     * disbursement_prep = disbursement - check (ignora que reducciones pudo llegar después)
-- =============================================================================

DROP VIEW IF EXISTS pi.v_reconciliation_check;
-- CASCADE: si ya existen las vistas de 03_kpi_views.sql (dependen de esta), se eliminan
-- también; basta volver a ejecutar 03_kpi_views.sql después.
DROP VIEW IF EXISTS pi.v_case_phases CASCADE;

CREATE VIEW pi.v_case_phases AS
WITH params AS (
    SELECT (SELECT value_date FROM pi.report_params WHERE param_key = 'report_cutoff_date') AS cutoff
),
base AS (
    SELECT
        c.*,
        p.cutoff,
        (c.closing_statement_disbursement_date IS NOT NULL)            AS is_closed,
        -- Punto de convergencia de las dos pistas (solo existe si AMBAS ya ocurrieron)
        CASE WHEN c.settlement_check_received_date IS NOT NULL
              AND c.reductions_confirmed_date      IS NOT NULL
             THEN GREATEST(c.settlement_check_received_date,
                           c.reductions_confirmed_date)
        END                                                            AS ready_to_disburse_date
    FROM pi.pi_cases c
    CROSS JOIN params p
),
phases AS (
    SELECT
        b.*,
        -- Fases: cada una es NULL mientras no existan sus dos extremos ---------------
        (treatment_completion_date - intake_date)                                   AS treatment_days,
        (medical_records_received_date - treatment_completion_date)                 AS records_wait_days,
        (demand_sent_date - medical_records_received_date)                          AS demand_prep_days,
        (first_offer_date - demand_sent_date)                                       AS offer_wait_days,
        (settlement_accepted_date - first_offer_date)                               AS negotiation_days,
        (release_signed_and_sent_to_carrier_date - settlement_accepted_date)        AS release_execution_days,
        (settlement_check_received_date - release_signed_and_sent_to_carrier_date)  AS carrier_check_wait_days,
        (ready_to_disburse_date - settlement_check_received_date)                   AS reductions_blocking_days,
        (closing_statement_disbursement_date - ready_to_disburse_date)              AS disbursement_prep_days,
        -- Pista paralela completa (informativa, NO es una de las 9 fases) ------------
        (reductions_confirmed_date - settlement_accepted_date)                      AS reductions_parallel_wait_days,
        -- Ciclo total medido de forma INDEPENDIENTE (extremo a extremo) --------------
        (closing_statement_disbursement_date - intake_date)                         AS total_cycle_days
    FROM base b
)
SELECT
    -- Identificación ------------------------------------------------------------------
    case_id,
    injury_type,
    insurance_company,
    case_manager,
    is_closed,
    CASE WHEN is_closed THEN 'Closed' ELSE 'Open' END                               AS case_status,
    CASE WHEN attorney_fee_pct = 0.40 THEN 'Litigation' ELSE 'Pre-suit' END         AS case_stage,

    -- Fechas de referencia --------------------------------------------------------------
    intake_date,
    settlement_accepted_date,
    settlement_check_received_date,
    reductions_confirmed_date,
    ready_to_disburse_date,
    closing_statement_disbursement_date,

    -- Las 9 fases (días) ---------------------------------------------------------------
    treatment_days,
    records_wait_days,
    demand_prep_days,
    offer_wait_days,
    negotiation_days,
    release_execution_days,
    carrier_check_wait_days,
    reductions_blocking_days,
    disbursement_prep_days,

    -- Clasificación (solo casos cerrados: en abiertos las fases futuras son NULL) ------
    CASE WHEN is_closed THEN demand_prep_days + release_execution_days + disbursement_prep_days END
                                                                                    AS controllable_days,
    CASE WHEN is_closed THEN records_wait_days + offer_wait_days
                             + carrier_check_wait_days + reductions_blocking_days END
                                                                                    AS external_days,
    CASE WHEN is_closed THEN treatment_days END                                     AS clinical_days,
    CASE WHEN is_closed THEN negotiation_days END                                   AS strategic_days,

    -- Camino crítico post-acuerdo --------------------------------------------------------
    reductions_parallel_wait_days,
    (reductions_blocking_days > 0)                                                  AS was_blocked_by_reductions,
    CASE
        WHEN ready_to_disburse_date IS NULL                                  THEN NULL
        WHEN reductions_confirmed_date > settlement_check_received_date      THEN 'Reductions'
        WHEN reductions_confirmed_date < settlement_check_received_date      THEN 'Carrier Check'
        ELSE 'Tie'
    END                                                                             AS critical_path_driver,

    -- RECONCILIACIÓN (casos cerrados) ----------------------------------------------------
    total_cycle_days,
    CASE WHEN is_closed THEN
        treatment_days + records_wait_days + demand_prep_days + offer_wait_days
        + negotiation_days + release_execution_days + carrier_check_wait_days
        + reductions_blocking_days + disbursement_prep_days
    END                                                                             AS phases_sum_days,
    CASE WHEN is_closed THEN
        (treatment_days + records_wait_days + demand_prep_days + offer_wait_days
         + negotiation_days + release_execution_days + carrier_check_wait_days
         + reductions_blocking_days + disbursement_prep_days) - total_cycle_days
    END                                                                             AS reconciliation_diff,

    -- Caso ABIERTO: fase actual y días en ella (medido contra la fecha de corte) ----------
    CASE WHEN NOT is_closed THEN (cutoff - intake_date) END                         AS days_open,
    CASE WHEN is_closed THEN NULL
         WHEN treatment_completion_date IS NULL                      THEN 'treatment'
         WHEN medical_records_received_date IS NULL                  THEN 'records_wait'
         WHEN demand_sent_date IS NULL                               THEN 'demand_prep'
         WHEN first_offer_date IS NULL                               THEN 'offer_wait'
         WHEN settlement_accepted_date IS NULL                       THEN 'negotiation'
         WHEN release_signed_and_sent_to_carrier_date IS NULL        THEN 'release_execution'
         WHEN settlement_check_received_date IS NULL                 THEN 'carrier_check_wait'
         WHEN reductions_confirmed_date IS NULL                      THEN 'reductions_blocking'
         ELSE                                                             'disbursement_prep'
    END                                                                             AS current_phase,
    CASE WHEN is_closed THEN NULL
         ELSE cutoff - CASE
              WHEN treatment_completion_date IS NULL                 THEN intake_date
              WHEN medical_records_received_date IS NULL             THEN treatment_completion_date
              WHEN demand_sent_date IS NULL                          THEN medical_records_received_date
              WHEN first_offer_date IS NULL                          THEN demand_sent_date
              WHEN settlement_accepted_date IS NULL                  THEN first_offer_date
              WHEN release_signed_and_sent_to_carrier_date IS NULL   THEN settlement_accepted_date
              WHEN settlement_check_received_date IS NULL            THEN release_signed_and_sent_to_carrier_date
              WHEN reductions_confirmed_date IS NULL                 THEN settlement_check_received_date
              ELSE                                                        ready_to_disburse_date
         END
    END                                                                             AS days_in_current_phase,

    -- Montos y honorarios ----------------------------------------------------------------
    settlement_amount,
    attorney_fee_pct,
    ROUND(settlement_amount * attorney_fee_pct, 2)                                  AS attorney_fee_amount,
    original_medical_bills_amount,
    final_reduced_bills_amount,
    original_liens_amount,
    final_reduced_liens_amount,

    -- Ahorro por reducciones: Medical Bills/LOPs vs Liens formales ------------------------
    (original_medical_bills_amount - final_reduced_bills_amount)                    AS bills_savings_amount,
    ROUND(1 - final_reduced_bills_amount / NULLIF(original_medical_bills_amount, 0), 4)
                                                                                    AS bills_savings_pct,
    (original_liens_amount - final_reduced_liens_amount)                            AS liens_savings_amount,
    ROUND(1 - final_reduced_liens_amount / NULLIF(original_liens_amount, 0), 4)     AS liens_savings_pct,   -- NULL si no hay liens

    -- Neto al cliente (honorario sobre el bruto; costos del caso no modelados) --------------
    CASE WHEN reductions_confirmed_date IS NOT NULL THEN
        ROUND(settlement_amount * (1 - attorney_fee_pct)
              - final_reduced_bills_amount - final_reduced_liens_amount, 2)
    END                                                                             AS net_to_client,
    CASE WHEN reductions_confirmed_date IS NOT NULL THEN
        ROUND(settlement_amount * (1 - attorney_fee_pct)
              - original_medical_bills_amount - original_liens_amount, 2)
    END                                                                             AS net_to_client_without_reductions
FROM phases;

COMMENT ON VIEW pi.v_case_phases IS
'1 fila por caso. 9 fases del camino crítico (suman EXACTAMENTE total_cycle_days en casos cerrados), '
'clasificación Controllable/External/Clinical/Strategic, ahorro por reducciones y neto al cliente. '
'Casos abiertos: fase actual y días en ella medidos contra pi.report_params.report_cutoff_date.';


-- -----------------------------------------------------------------------------
-- Vista de control: TODAS las columnas *_with_issue deben ser 0
-- -----------------------------------------------------------------------------
CREATE VIEW pi.v_reconciliation_check AS
SELECT
    count(*) FILTER (WHERE is_closed)                                                     AS closed_cases,
    count(*) FILTER (WHERE is_closed AND reconciliation_diff <> 0)                        AS cases_with_diff,
    COALESCE(sum(abs(reconciliation_diff)) FILTER (WHERE is_closed), 0)                   AS total_abs_diff_days,
    count(*) FILTER (WHERE is_closed AND (
          treatment_days < 1 OR records_wait_days < 1 OR demand_prep_days < 1
       OR offer_wait_days < 1 OR negotiation_days < 1 OR release_execution_days < 1
       OR carrier_check_wait_days < 1 OR disbursement_prep_days < 1
       OR reductions_blocking_days < 0))                                                  AS cases_with_bad_phase,
    count(*) FILTER (WHERE is_closed
       AND reductions_blocking_days > reductions_parallel_wait_days)                      AS cases_blocking_gt_parallel,
    count(*) FILTER (WHERE is_closed
       AND controllable_days + external_days + clinical_days + strategic_days
           <> total_cycle_days)                                                           AS cases_with_class_diff
FROM pi.v_case_phases;


-- -----------------------------------------------------------------------------
-- Auto-verificación: este script FALLA si la reconciliación no es 100%
-- -----------------------------------------------------------------------------
DO $$
DECLARE r pi.v_reconciliation_check%ROWTYPE;
BEGIN
    SELECT * INTO r FROM pi.v_reconciliation_check;
    IF r.closed_cases = 0 THEN
        RAISE EXCEPTION 'No hay casos cerrados: la tabla pi.pi_cases está vacía. Ejecuta sql/01_schema.sql y luego python python/01b_load_to_postgres.py';
    END IF;
    IF r.cases_with_diff <> 0 OR r.cases_with_bad_phase <> 0
       OR r.cases_blocking_gt_parallel <> 0 OR r.cases_with_class_diff <> 0 THEN
        RAISE EXCEPTION 'RECONCILIACIÓN FALLIDA: %', row_to_json(r);
    END IF;
    RAISE NOTICE 'Reconciliación OK: % casos cerrados, diferencia total = % días', r.closed_cases, r.total_abs_diff_days;
END $$;

-- Para auditar manualmente (debe devolver 0 filas):
-- SELECT case_id, total_cycle_days, phases_sum_days, reconciliation_diff
--   FROM pi.v_case_phases WHERE is_closed AND reconciliation_diff <> 0;