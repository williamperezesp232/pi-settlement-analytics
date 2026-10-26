-- =============================================================================
-- 03_kpi_views.sql
-- Proyecto : Legal Operations & Analytics - Personal Injury (Plaintiff-side)
-- Requiere : 01_schema_and_load.sql  y  02_metrics_view.sql
--
-- Vistas que crea (todas leen de pi.v_case_phases y pi.report_params):
--   1. pi.v_phase_summary        -> media / mediana / P90 de cada una de las 9 fases
--   2. pi.v_phase_type_summary   -> reparto del ciclo: Controllable / External / Clinical / Strategic
--   3. pi.v_open_case_alerts     -> los 36 casos abiertos: semáforo + dinero en juego
--   4. pi.v_manager_scorecard    -> desempeño por case manager
--   5. pi.v_carrier_scorecard    -> desempeño por aseguradora
--   6. pi.v_reductions_blocking  -> análisis del camino crítico (reducciones vs cheque)
--   7. pi.v_reduction_savings    -> ahorro de Liens formales vs Medical Bills/LOPs
--
-- CONVENCIONES
--   * Todos los porcentajes son FRACCIONES (0.3512 = 35.12%). Formatea en el BI.
--   * Medianas/P90 calculadas con percentile_cont sobre casos CERRADOS.
--   * Fecha de corte siempre desde pi.report_params (nunca CURRENT_DATE).
--   * Re-ejecutable: hace DROP VIEW IF EXISTS y recrea.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 0. PARÁMETROS: este archivo lee pi.report_params DIRECTAMENTE (sin funciones)
-- -----------------------------------------------------------------------------
-- Cada vista usa subconsultas del tipo:
--     (SELECT value_num FROM pi.report_params WHERE param_key = '<clave>')
-- Claves OBLIGATORIA:
--     report_cutoff_date (value_date)       -> fecha de corte
-- Claves OPCIONALES (si no existen en la tabla, se usa el valor por defecto indicado):
--     alert_days_<fase>   (9 umbrales)      -> defaults: 365/60/21/60/120/14/21/30/7
--     alert_critical_multiplier             -> 1.5
--     alert_watch_pct                       -> 0.80
--     scorecard_flag_pct                    -> 0.20
--     min_sample_size                       -> 15
--     lien_reduction_cap_pct                -> 0.25
-- Para sobreescribir un default, inserta la fila en tu tabla, por ejemplo:
--     INSERT INTO pi.report_params (param_key, value_num) VALUES ('scorecard_flag_pct', 0.15);
-- (ajusta las columnas al esquema de TU tabla report_params)

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pi.report_params
                    WHERE param_key = 'report_cutoff_date' AND value_date IS NOT NULL) THEN
        RAISE EXCEPTION 'Falta la fila report_cutoff_date (value_date) en pi.report_params';
    END IF;
END $$;

DROP VIEW IF EXISTS pi.v_reduction_savings;
DROP VIEW IF EXISTS pi.v_reductions_blocking;
DROP VIEW IF EXISTS pi.v_carrier_scorecard;
DROP VIEW IF EXISTS pi.v_manager_scorecard;
DROP VIEW IF EXISTS pi.v_open_case_alerts;
DROP VIEW IF EXISTS pi.v_phase_type_summary;
DROP VIEW IF EXISTS pi.v_phase_summary;


-- =============================================================================
-- 1. pi.v_phase_summary : las 9 fases (casos cerrados)
-- =============================================================================
CREATE VIEW pi.v_phase_summary AS
WITH closed AS (
    SELECT * FROM pi.v_case_phases WHERE is_closed
),
total AS (
    SELECT avg(total_cycle_days) AS avg_total_cycle FROM closed
)
SELECT
    ph.phase_order,
    ph.phase_key,
    ph.phase_name,
    ph.phase_type,
    count(*)                                                                  AS closed_cases,
    ROUND(avg(ph.days), 1)                                                    AS avg_days,
    ROUND((percentile_cont(0.5) WITHIN GROUP (ORDER BY ph.days))::numeric, 1) AS median_days,
    ROUND((percentile_cont(0.9) WITHIN GROUP (ORDER BY ph.days))::numeric, 1) AS p90_days,
    max(ph.days)                                                              AS max_days,
    ROUND(avg(ph.days) / t.avg_total_cycle, 4)                                AS pct_of_total_cycle   -- suma 100% por la reconciliación
FROM closed c
CROSS JOIN total t
CROSS JOIN LATERAL (VALUES
    (1, 'treatment',           'Treatment',              'Clinical',     c.treatment_days),
    (2, 'records_wait',        'Records Wait',           'External',     c.records_wait_days),
    (3, 'demand_prep',         'Demand Prep',            'Controllable', c.demand_prep_days),
    (4, 'offer_wait',          'Adjuster Offer Wait',    'External',     c.offer_wait_days),
    (5, 'negotiation',         'Strategic Negotiation',  'Strategic',    c.negotiation_days),
    (6, 'release_execution',   'Release Execution',      'Controllable', c.release_execution_days),
    (7, 'carrier_check_wait',  'Carrier Check Wait',     'External',     c.carrier_check_wait_days),
    (8, 'reductions_blocking', 'Reductions Blocking',    'External',     c.reductions_blocking_days),
    (9, 'disbursement_prep',   'Disbursement Prep',      'Controllable', c.disbursement_prep_days)
) AS ph(phase_order, phase_key, phase_name, phase_type, days)
GROUP BY ph.phase_order, ph.phase_key, ph.phase_name, ph.phase_type, t.avg_total_cycle
ORDER BY ph.phase_order;

COMMENT ON VIEW pi.v_phase_summary IS
'Media/mediana/P90 de cada fase (casos cerrados). Reductions Blocking = días que el cheque esperó a las reducciones (0 si no bloquearon).';


-- =============================================================================
-- 2. pi.v_phase_type_summary : ¿en qué parte del ciclo se va el tiempo?
-- =============================================================================
CREATE VIEW pi.v_phase_type_summary AS
WITH closed AS (
    SELECT * FROM pi.v_case_phases WHERE is_closed
)
SELECT
    t.phase_type,
    count(*)                                                                    AS closed_cases,
    ROUND(avg(t.days), 1)                                                       AS avg_days,
    ROUND((percentile_cont(0.5) WITHIN GROUP (ORDER BY t.days))::numeric, 1)    AS median_days,
    ROUND((percentile_cont(0.9) WITHIN GROUP (ORDER BY t.days))::numeric, 1)    AS p90_days,
    ROUND(avg(t.days) / (SELECT avg(total_cycle_days) FROM closed), 4)          AS pct_of_total_cycle
FROM closed c
CROSS JOIN LATERAL (VALUES
    ('Controllable', c.controllable_days),
    ('External',     c.external_days),
    ('Clinical',     c.clinical_days),
    ('Strategic',    c.strategic_days)
) AS t(phase_type, days)
GROUP BY t.phase_type
ORDER BY avg(t.days) DESC;

COMMENT ON VIEW pi.v_phase_type_summary IS
'Controllable = Demand Prep + Release Execution + Disbursement Prep. External = Records + Offer Wait + Carrier Check + Reductions Blocking. Los 4 tipos suman 100% del ciclo.';


-- =============================================================================
-- 3. pi.v_open_case_alerts : alertas de los casos abiertos, ordenadas por $ en juego
-- =============================================================================
-- Dinero en juego (money_at_stake), en orden de confianza:
--   1) 'Actual settlement'        : el acuerdo ya fue aceptado -> settlement_amount real.
--   2) 'Estimated (bills x mult)' : hay bills/liens originales -> (bills+liens) x mediana
--                                   del múltiplo settlement/deuda del mismo injury_type.
--   3) 'Estimated (injury median)': aún sin bills -> mediana de settlement del injury_type.
-- Semáforo: días en la fase actual vs umbral de pi.report_params (alert_days_<fase>)
--   WATCH = consumió >= alert_watch_pct (80%) del umbral (alerta temprana)
--   AMBER = supera el umbral ; RED = supera umbral x alert_critical_multiplier.
-- =============================================================================
CREATE VIEW pi.v_open_case_alerts AS
WITH params AS (
    SELECT
        (SELECT value_date FROM pi.report_params WHERE param_key = 'report_cutoff_date')                    AS cutoff,
        COALESCE((SELECT value_num FROM pi.report_params WHERE param_key = 'alert_critical_multiplier'), 1.5) AS red_mult,
        COALESCE((SELECT value_num FROM pi.report_params WHERE param_key = 'alert_watch_pct'), 0.80)         AS watch_pct
),
thresholds AS (   -- umbral por fase: valor de report_params; si falta la fila, el default
    SELECT d.phase_key,
           COALESCE(rp.value_num, d.default_days)::int AS threshold_days
      FROM (VALUES ('treatment', 365), ('records_wait', 60), ('demand_prep', 21),
                   ('offer_wait', 60), ('negotiation', 120), ('release_execution', 14),
                   ('carrier_check_wait', 21), ('reductions_blocking', 30),
                   ('disbursement_prep', 7)) AS d(phase_key, default_days)
      LEFT JOIN pi.report_params rp ON rp.param_key = 'alert_days_' || d.phase_key
),
injury_ref AS (
    SELECT injury_type,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY settlement_amount)                        AS median_settlement,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY settlement_amount
               / NULLIF(original_medical_bills_amount + original_liens_amount, 0))               AS median_multiple
      FROM pi.v_case_phases
     WHERE is_closed
     GROUP BY injury_type
),
open_cases AS (
    SELECT * FROM pi.v_case_phases WHERE NOT is_closed
),
scored AS (
    SELECT
        o.case_id, o.injury_type, o.insurance_company, o.case_manager, o.case_stage,
        o.attorney_fee_pct, o.intake_date, o.days_open,
        o.current_phase, o.days_in_current_phase,
        p.cutoff, p.red_mult, p.watch_pct,
        t.threshold_days,
        CASE o.current_phase
            WHEN 'treatment'           THEN 'Treatment'
            WHEN 'records_wait'        THEN 'Records Wait'
            WHEN 'demand_prep'         THEN 'Demand Prep'
            WHEN 'offer_wait'          THEN 'Adjuster Offer Wait'
            WHEN 'negotiation'         THEN 'Strategic Negotiation'
            WHEN 'release_execution'   THEN 'Release Execution'
            WHEN 'carrier_check_wait'  THEN 'Carrier Check Wait'
            WHEN 'reductions_blocking' THEN 'Reductions Blocking'
            WHEN 'disbursement_prep'   THEN 'Disbursement Prep'
        END AS phase_name,
        CASE o.current_phase
            WHEN 'treatment'           THEN 'Clinical'
            WHEN 'negotiation'         THEN 'Strategic'
            WHEN 'demand_prep'         THEN 'Controllable'
            WHEN 'release_execution'   THEN 'Controllable'
            WHEN 'disbursement_prep'   THEN 'Controllable'
            ELSE 'External'
        END AS phase_type,
        CASE o.current_phase
            WHEN 'treatment'           THEN 'Check-in con el cliente y proveedores; confirmar fecha estimada de alta/MMI.'
            WHEN 'records_wait'        THEN 'Escalar solicitudes de expedientes y facturas pendientes con cada proveedor.'
            WHEN 'demand_prep'         THEN 'Priorizar el demand package; revisar carga de trabajo del manager.'
            WHEN 'offer_wait'          THEN 'Follow-up al adjuster; escalar al supervisor del carrier si supera el plazo.'
            WHEN 'negotiation'         THEN 'Revisar estrategia con el abogado: counter, mediación o evaluar litigio.'
            WHEN 'release_execution'   THEN 'Contactar al cliente para firma/notarización; reenviar el Release hoy.'
            WHEN 'carrier_check_wait'  THEN 'Confirmar recepción del Release y fecha de emisión del cheque con el adjuster.'
            WHEN 'reductions_blocking' THEN 'Cheque en mano: escalar reducciones pendientes con billing/lienholders.'
            WHEN 'disbursement_prep'   THEN 'Cerrar Closing Statement y emitir pago al cliente.'
        END AS suggested_action,
        -- Dinero en juego -------------------------------------------------------------
        COALESCE(
            o.settlement_amount,
            (o.original_medical_bills_amount + o.original_liens_amount) * r.median_multiple,
            r.median_settlement
        )::numeric AS money_raw,
        CASE
            WHEN o.settlement_amount IS NOT NULL                    THEN 'Actual settlement'
            WHEN o.original_medical_bills_amount IS NOT NULL        THEN 'Estimated (bills x mult)'
            ELSE                                                         'Estimated (injury median)'
        END AS amount_basis
    FROM open_cases o
    CROSS JOIN params p
    JOIN thresholds t       ON t.phase_key = o.current_phase
    LEFT JOIN injury_ref r  ON r.injury_type = o.injury_type
)
SELECT
    ROW_NUMBER() OVER (ORDER BY ROUND(money_raw, 2) DESC,
                                GREATEST(0, days_in_current_phase - threshold_days) DESC,
                                case_id)                                       AS priority_rank,
    case_id,
    case_manager,
    insurance_company,
    injury_type,
    case_stage,
    ROUND(money_raw, 2)                                                        AS money_at_stake,
    amount_basis,
    ROUND(money_raw * attorney_fee_pct, 2)                                     AS firm_fee_at_stake,
    current_phase,
    phase_name,
    phase_type,
    (cutoff - days_in_current_phase)                                           AS phase_start_date,
    days_in_current_phase,
    threshold_days,
    GREATEST(0, days_in_current_phase - threshold_days)                        AS days_over_threshold,
    ROUND(days_in_current_phase::numeric / threshold_days, 2)                  AS pct_of_threshold_used,
    CASE
        WHEN days_in_current_phase > threshold_days * red_mult THEN 'RED'
        WHEN days_in_current_phase > threshold_days            THEN 'AMBER'
        WHEN days_in_current_phase >= threshold_days * watch_pct THEN 'WATCH'
        ELSE                                                        'ON TRACK'
    END                                                                        AS alert_level,
    days_open,
    intake_date,
    suggested_action
FROM scored
ORDER BY priority_rank;

COMMENT ON VIEW pi.v_open_case_alerts IS
'Los casos abiertos a la fecha de corte, ordenados por dinero en juego (priority_rank=1 = más dinero). '
'amount_basis indica si el monto es real o estimado. alert_level compara días en la fase actual vs umbral de report_params.';


-- =============================================================================
-- 4. pi.v_manager_scorecard : desempeño por case manager
-- =============================================================================
-- Métrica central: mediana de días en fases CONTROLABLES (Demand Prep + Release
-- Execution + Disbursement Prep) vs la mediana de la firma.
-- =============================================================================
CREATE VIEW pi.v_manager_scorecard AS
WITH cfg AS (
    SELECT COALESCE((SELECT value_num FROM pi.report_params WHERE param_key = 'scorecard_flag_pct'), 0.20) AS flag_pct,
           COALESCE((SELECT value_num FROM pi.report_params WHERE param_key = 'min_sample_size'), 15)       AS min_sample
),
closed AS (
    SELECT * FROM pi.v_case_phases WHERE is_closed
),
firm AS (
    SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY controllable_days) AS firm_median_controllable
      FROM closed
),
mgr AS (
    SELECT
        case_manager,
        count(*)                                                                    AS closed_cases,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY demand_prep_days)               AS median_demand_prep_days,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY release_execution_days)         AS median_release_execution_days,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY disbursement_prep_days)         AS median_disbursement_prep_days,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY controllable_days)              AS median_controllable_days,
        percentile_cont(0.9) WITHIN GROUP (ORDER BY controllable_days)              AS p90_controllable_days,
        avg(total_cycle_days)                                                       AS avg_total_cycle_days,
        1 - sum(final_reduced_bills_amount) / NULLIF(sum(original_medical_bills_amount), 0)
                                                                                    AS bills_savings_pct,
        1 - sum(final_reduced_liens_amount) / NULLIF(sum(original_liens_amount), 0) AS liens_savings_pct
    FROM closed
    GROUP BY case_manager
),
open_load AS (
    SELECT case_manager,
           count(*)                                       AS open_cases,
           count(*) FILTER (WHERE alert_level = 'RED')    AS open_red_alerts,
           count(*) FILTER (WHERE alert_level = 'AMBER')  AS open_amber_alerts,
           count(*) FILTER (WHERE alert_level = 'WATCH')  AS open_watch_alerts,
           sum(money_at_stake)                            AS open_money_at_stake
      FROM pi.v_open_case_alerts
     GROUP BY case_manager
)
SELECT
    RANK() OVER (ORDER BY m.median_controllable_days, m.case_manager)               AS rank_fastest,
    m.case_manager,
    m.closed_cases,
    ROUND(m.median_demand_prep_days::numeric, 1)                                    AS median_demand_prep_days,
    ROUND(m.median_release_execution_days::numeric, 1)                              AS median_release_execution_days,
    ROUND(m.median_disbursement_prep_days::numeric, 1)                              AS median_disbursement_prep_days,
    ROUND(m.median_controllable_days::numeric, 1)                                   AS median_controllable_days,
    ROUND(m.p90_controllable_days::numeric, 1)                                      AS p90_controllable_days,
    ROUND(f.firm_median_controllable::numeric, 1)                                   AS firm_median_controllable_days,
    ROUND((m.median_controllable_days / NULLIF(f.firm_median_controllable, 0) - 1)::numeric, 4)
                                                                                    AS controllable_vs_firm_pct,
    CASE
        WHEN m.median_controllable_days > f.firm_median_controllable * (1 + cfg.flag_pct) THEN 'SLOWER than firm'
        WHEN m.median_controllable_days < f.firm_median_controllable * (1 - cfg.flag_pct) THEN 'FASTER than firm'
        ELSE 'In line'
    END                                                                             AS performance_flag,
    CASE WHEN m.closed_cases < cfg.min_sample THEN 'Low sample' ELSE 'OK' END
                                                                                    AS sample_flag,
    ROUND(m.avg_total_cycle_days, 1)                                                AS avg_total_cycle_days,
    ROUND(m.bills_savings_pct, 4)                                                   AS bills_savings_pct,
    ROUND(m.liens_savings_pct, 4)                                                   AS liens_savings_pct,
    COALESCE(o.open_cases, 0)                                                       AS open_cases,
    COALESCE(o.open_red_alerts, 0)                                                  AS open_red_alerts,
    COALESCE(o.open_amber_alerts, 0)                                                AS open_amber_alerts,
    COALESCE(o.open_watch_alerts, 0)                                                AS open_watch_alerts,
    COALESCE(o.open_money_at_stake, 0)                                              AS open_money_at_stake
FROM mgr m
CROSS JOIN firm f
CROSS JOIN cfg
LEFT JOIN open_load o USING (case_manager)
ORDER BY rank_fastest;

COMMENT ON VIEW pi.v_manager_scorecard IS
'Por case manager: mediana de días CONTROLABLES vs la firma, ahorro en reducciones y carga abierta con alertas. '
'Aviso: el mix de casos (litigio/lesiones) puede influir; revisar sample_flag.';


-- =============================================================================
-- 5. pi.v_carrier_scorecard : desempeño por aseguradora
-- =============================================================================
-- Fases EXTERNAS atribuibles al carrier: Adjuster Offer Wait y Carrier Check Wait.
-- Negotiation se muestra como contexto, pero también depende de la estrategia del abogado.
-- pct_litigation ayuda a ver si el mix de casos explica diferencias.
-- =============================================================================
CREATE VIEW pi.v_carrier_scorecard AS
WITH cfg AS (
    SELECT COALESCE((SELECT value_num FROM pi.report_params WHERE param_key = 'scorecard_flag_pct'), 0.20)          AS flag_pct,
           COALESCE((SELECT value_num FROM pi.report_params WHERE param_key = 'min_sample_size'), 15)                AS min_sample,
           COALESCE((SELECT value_num FROM pi.report_params WHERE param_key = 'alert_days_carrier_check_wait'), 21)  AS check_threshold
),
closed AS (
    SELECT * FROM pi.v_case_phases WHERE is_closed
),
firm AS (
    SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY offer_wait_days)         AS firm_median_offer_wait,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY carrier_check_wait_days) AS firm_median_check_wait
      FROM closed
),
car AS (
    SELECT
        insurance_company,
        count(*)                                                                    AS closed_cases,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY offer_wait_days)                AS median_offer_wait_days,
        percentile_cont(0.9) WITHIN GROUP (ORDER BY offer_wait_days)                AS p90_offer_wait_days,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY negotiation_days)               AS median_negotiation_days,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY carrier_check_wait_days)        AS median_check_wait_days,
        percentile_cont(0.9) WITHIN GROUP (ORDER BY carrier_check_wait_days)        AS p90_check_wait_days,
        avg((carrier_check_wait_days > cfg.check_threshold)::int)
                                                                                    AS pct_checks_over_threshold,
        avg((case_stage = 'Litigation')::int)                                       AS pct_litigation,
        avg(settlement_amount)                                                      AS avg_settlement_amount,
        sum(settlement_amount)                                                      AS total_settlement_amount
    FROM closed
    CROSS JOIN cfg
    GROUP BY insurance_company
),
open_load AS (
    SELECT insurance_company,
           count(*)                                       AS open_cases,
           count(*) FILTER (WHERE alert_level = 'RED')    AS open_red_alerts,
           count(*) FILTER (WHERE alert_level = 'AMBER')  AS open_amber_alerts,
           count(*) FILTER (WHERE alert_level = 'WATCH')  AS open_watch_alerts
      FROM pi.v_open_case_alerts
     GROUP BY insurance_company
)
SELECT
    c.insurance_company,
    c.closed_cases,
    ROUND(c.median_offer_wait_days::numeric, 1)                                     AS median_offer_wait_days,
    ROUND(c.p90_offer_wait_days::numeric, 1)                                        AS p90_offer_wait_days,
    ROUND(f.firm_median_offer_wait::numeric, 1)                                     AS firm_median_offer_wait_days,
    ROUND((c.median_offer_wait_days / NULLIF(f.firm_median_offer_wait, 0) - 1)::numeric, 4)
                                                                                    AS offer_wait_vs_firm_pct,
    CASE
        WHEN c.median_offer_wait_days > f.firm_median_offer_wait * (1 + cfg.flag_pct) THEN 'SLOWER than firm'
        WHEN c.median_offer_wait_days < f.firm_median_offer_wait * (1 - cfg.flag_pct) THEN 'FASTER than firm'
        ELSE 'In line'
    END                                                                             AS offer_wait_flag,
    ROUND(c.median_negotiation_days::numeric, 1)                                    AS median_negotiation_days,
    ROUND(c.median_check_wait_days::numeric, 1)                                     AS median_check_wait_days,
    ROUND(c.p90_check_wait_days::numeric, 1)                                        AS p90_check_wait_days,
    ROUND(f.firm_median_check_wait::numeric, 1)                                     AS firm_median_check_wait_days,
    ROUND((c.median_check_wait_days / NULLIF(f.firm_median_check_wait, 0) - 1)::numeric, 4)
                                                                                    AS check_wait_vs_firm_pct,
    CASE
        WHEN c.median_check_wait_days > f.firm_median_check_wait * (1 + cfg.flag_pct) THEN 'SLOWER than firm'
        WHEN c.median_check_wait_days < f.firm_median_check_wait * (1 - cfg.flag_pct) THEN 'FASTER than firm'
        ELSE 'In line'
    END                                                                             AS check_wait_flag,
    ROUND(c.pct_checks_over_threshold::numeric, 4)                                  AS pct_checks_over_threshold,
    ROUND(c.pct_litigation::numeric, 4)                                             AS pct_litigation,
    CASE WHEN c.closed_cases < cfg.min_sample THEN 'Low sample' ELSE 'OK' END
                                                                                    AS sample_flag,
    ROUND(c.avg_settlement_amount, 2)                                               AS avg_settlement_amount,
    ROUND(c.total_settlement_amount, 2)                                             AS total_settlement_amount,
    COALESCE(o.open_cases, 0)                                                       AS open_cases,
    COALESCE(o.open_red_alerts, 0)                                                  AS open_red_alerts,
    COALESCE(o.open_amber_alerts, 0)                                                AS open_amber_alerts,
    COALESCE(o.open_watch_alerts, 0)                                                AS open_watch_alerts
FROM car c
CROSS JOIN firm f
CROSS JOIN cfg
LEFT JOIN open_load o USING (insurance_company)
ORDER BY c.total_settlement_amount DESC;

COMMENT ON VIEW pi.v_carrier_scorecard IS
'Por aseguradora: espera de primera oferta y de cheque (medianas, P90) vs la firma. Ordenado por volumen de settlement. '
'pct_litigation permite detectar si el mix de casos explica la diferencia.';


-- =============================================================================
-- 6. pi.v_reductions_blocking : camino crítico post-acuerdo
-- =============================================================================
-- Un caso está "bloqueado" cuando las reducciones se confirman DESPUÉS de que llegó el
-- cheque: el dinero está en mano pero no se puede desembolsar.
-- blocking_days = GREATEST(0, reductions_confirmed - check_received).
-- Formato largo: una fila por (dimension, valor). Filtra por 'dimension' en el BI.
-- LIMITACIÓN: no existe la fecha "reducciones solicitadas", por lo que se mide el
-- bloqueo, pero no se puede simular cuántos días se ganarían iniciando antes.
-- =============================================================================
CREATE VIEW pi.v_reductions_blocking AS
WITH closed AS (
    SELECT * FROM pi.v_case_phases WHERE is_closed
)
SELECT
    d.dimension,
    d.dimension_value,
    count(*)                                                                         AS closed_cases,
    ROUND(count(*)::numeric / SUM(count(*)) OVER (PARTITION BY d.dimension), 4)      AS pct_of_dimension_cases,
    count(*) FILTER (WHERE c.reductions_blocking_days > 0)                           AS blocked_cases,
    ROUND(avg((c.reductions_blocking_days > 0)::int)::numeric, 4)                    AS pct_blocked,
    ROUND(avg(c.reductions_blocking_days), 1)                                        AS avg_blocking_days_all_cases,
    ROUND(avg(c.reductions_blocking_days) FILTER (WHERE c.reductions_blocking_days > 0), 1)
                                                                                     AS avg_blocking_days_when_blocked,
    ROUND((percentile_cont(0.9) WITHIN GROUP (ORDER BY c.reductions_blocking_days)
           FILTER (WHERE c.reductions_blocking_days > 0))::numeric, 1)               AS p90_blocking_days_when_blocked,
    ROUND((percentile_cont(0.5) WITHIN GROUP (ORDER BY c.reductions_parallel_wait_days))::numeric, 1)
                                                                                     AS median_reductions_total_wait_days,
    SUM(c.reductions_blocking_days)                                                  AS total_blocking_days,
    ROUND(SUM(c.reductions_blocking_days)::numeric
          / NULLIF(SUM(SUM(c.reductions_blocking_days)) OVER (PARTITION BY d.dimension), 0), 4)
                                                                                     AS pct_of_all_blocking_days,
    ROUND(SUM(c.reductions_blocking_days)::numeric / NULLIF(SUM(c.total_cycle_days), 0), 4)
                                                                                     AS pct_of_cycle_days_lost
FROM closed c
CROSS JOIN LATERAL (VALUES
    ('1_Overall',            'All closed cases'),
    ('2_Formal liens',       CASE WHEN c.original_liens_amount > 0
                                  THEN 'With formal liens' ELSE 'LOPs only (no formal liens)' END),
    ('3_Critical path driver', c.critical_path_driver),
    ('4_Injury type',        c.injury_type),
    ('5_Insurance carrier',  c.insurance_company),
    ('6_Case manager',       c.case_manager),
    ('7_Case stage',         c.case_stage)
) AS d(dimension, dimension_value)
GROUP BY d.dimension, d.dimension_value
ORDER BY d.dimension, total_blocking_days DESC NULLS LAST, d.dimension_value;

COMMENT ON VIEW pi.v_reductions_blocking IS
'Bloqueo del desembolso por reducciones (formato largo: dimension/valor). pct_blocked = % de casos con cheque esperando reducciones. '
'pct_of_all_blocking_days = peso del segmento dentro del total de días de bloqueo de su dimensión.';


-- =============================================================================
-- 7. pi.v_reduction_savings : Liens formales vs Medical Bills/LOPs
-- =============================================================================
-- Base: casos con reducciones YA confirmadas (incluye algunos abiertos que esperan
-- desembolso). Porcentajes PONDERADOS por dólares (1 - suma_final / suma_original).
-- Regla de negocio: liens formales con tope de 25% (liens_over_cap_cases debe ser 0).
-- =============================================================================
CREATE VIEW pi.v_reduction_savings AS
WITH cfg AS (
    SELECT COALESCE((SELECT value_num FROM pi.report_params WHERE param_key = 'lien_reduction_cap_pct'), 0.25) AS lien_cap
),
base AS (
    SELECT * FROM pi.v_case_phases WHERE reductions_confirmed_date IS NOT NULL
)
SELECT
    d.dimension,
    d.dimension_value,
    count(*)                                                                         AS cases_with_reductions,
    count(*) FILTER (WHERE b.original_liens_amount > 0)                              AS cases_with_formal_liens,

    -- Medical Bills / LOPs --------------------------------------------------------------
    SUM(b.original_medical_bills_amount)                                             AS bills_original_total,
    SUM(b.final_reduced_bills_amount)                                                AS bills_final_total,
    ROUND(1 - SUM(b.final_reduced_bills_amount) / NULLIF(SUM(b.original_medical_bills_amount), 0), 4)
                                                                                     AS bills_savings_pct,
    ROUND(avg(b.bills_savings_pct), 4)                                               AS bills_avg_savings_pct_per_case,

    -- Liens formales (solo casos que tienen liens) ------------------------------------------
    SUM(b.original_liens_amount)                                                     AS liens_original_total,
    SUM(b.final_reduced_liens_amount)                                                AS liens_final_total,
    ROUND(1 - SUM(b.final_reduced_liens_amount) / NULLIF(SUM(b.original_liens_amount), 0), 4)
                                                                                     AS liens_savings_pct,
    ROUND(avg(b.liens_savings_pct), 4)                                               AS liens_avg_savings_pct_per_case,
    ROUND(max(b.liens_savings_pct), 4)                                               AS liens_max_savings_pct,
    count(*) FILTER (WHERE b.liens_savings_pct > cfg.lien_cap + 0.0001)
                                                                                     AS liens_over_cap_cases,

    -- Brecha LOPs vs Liens (puntos porcentuales, en fracción) -------------------------------
    ROUND((1 - SUM(b.final_reduced_bills_amount) / NULLIF(SUM(b.original_medical_bills_amount), 0))
        - (1 - SUM(b.final_reduced_liens_amount) / NULLIF(SUM(b.original_liens_amount), 0)), 4)
                                                                                     AS savings_gap_bills_minus_liens,

    -- Impacto en el cliente ---------------------------------------------------------------
    SUM(b.bills_savings_amount) + SUM(b.liens_savings_amount)                        AS total_savings_amount,
    SUM(b.net_to_client_without_reductions)                                          AS net_to_client_without_reductions,
    SUM(b.net_to_client)                                                             AS net_to_client,
    ROUND(SUM(b.net_to_client) / NULLIF(SUM(b.net_to_client_without_reductions), 0) - 1, 4)
                                                                                     AS client_net_uplift_pct
FROM base b
CROSS JOIN cfg
CROSS JOIN LATERAL (VALUES
    ('1_Overall',     'All cases with confirmed reductions'),
    ('2_Injury type', b.injury_type),
    ('3_Case manager', b.case_manager),
    ('4_Case stage',  b.case_stage)
) AS d(dimension, dimension_value)
GROUP BY d.dimension, d.dimension_value
ORDER BY d.dimension, total_savings_amount DESC NULLS LAST, d.dimension_value;

COMMENT ON VIEW pi.v_reduction_savings IS
'Ahorro por reducciones: Liens formales (tope 25%) vs Medical Bills/LOPs (30-40%+). '
'Porcentajes ponderados por $; client_net_uplift_pct = cuánto sube el neto del cliente gracias a las reducciones.';


-- =============================================================================
-- AUTO-VERIFICACIÓN: el script falla si algo no cuadra
-- =============================================================================
DO $$
DECLARE
    v_alerts        int;
    v_open_cases    int;
    v_pct_sum       numeric;
    v_type_sum      numeric;
    v_over_cap      int;
    v_bad_dims      int;
BEGIN
    SELECT count(*) INTO v_alerts     FROM pi.v_open_case_alerts;
    SELECT count(*) INTO v_open_cases FROM pi.pi_cases WHERE closing_statement_disbursement_date IS NULL;
    IF v_alerts <> v_open_cases THEN
        RAISE EXCEPTION 'v_open_case_alerts tiene % filas y hay % casos abiertos', v_alerts, v_open_cases;
    END IF;

    SELECT SUM(pct_of_total_cycle) INTO v_pct_sum  FROM pi.v_phase_summary;
    SELECT SUM(pct_of_total_cycle) INTO v_type_sum FROM pi.v_phase_type_summary;
    IF abs(v_pct_sum - 1) > 0.001 OR abs(v_type_sum - 1) > 0.001 THEN
        RAISE EXCEPTION 'Las fases no suman 100%% del ciclo: fases=%, tipos=%', v_pct_sum, v_type_sum;
    END IF;

    SELECT COALESCE(SUM(liens_over_cap_cases), 0) INTO v_over_cap
      FROM pi.v_reduction_savings WHERE dimension = '1_Overall';
    IF v_over_cap > 0 THEN
        RAISE EXCEPTION '% casos con reducción de liens > tope', v_over_cap;
    END IF;

    -- Cada dimensión de bloqueo debe sumar los 164 casos cerrados
    SELECT count(*) INTO v_bad_dims FROM (
        SELECT dimension FROM pi.v_reductions_blocking
         GROUP BY dimension HAVING SUM(closed_cases) <> (SELECT closed_cases FROM pi.v_reconciliation_check)
    ) x;
    IF v_bad_dims > 0 THEN
        RAISE EXCEPTION 'Alguna dimensión de v_reductions_blocking no suma los casos cerrados';
    END IF;

    RAISE NOTICE 'KPI views OK: % alertas, fases=100%% del ciclo, 0 liens sobre tope, dimensiones cuadran.', v_alerts;
END $$;