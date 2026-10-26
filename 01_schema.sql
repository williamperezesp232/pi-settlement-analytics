-- =============================================================================
-- 01_schema.sql
-- Proyecto : Legal Operations & Analytics - Personal Injury (Plaintiff-side)
-- Motor    : PostgreSQL 13+
-- Propósito: Crear el esquema `pi`, la tabla `pi.pi_cases` (con todas sus restricciones
--            de integridad) y la tabla de parámetros `pi.report_params`.
--
-- COMPATIBLE CON CUALQUIER CLIENTE (SQLTools, DBeaver, pgAdmin, psql):
--   * SQL puro: sin meta-comandos de psql ni rutas de archivos.
--   * La CARGA del CSV ya no vive aquí: la hace  python/01b_load_to_postgres.py
--
-- ORDEN DEL PROYECTO
--   python python/01_generate_dataset.py     -> data/pi_cases.csv
--   sql/01_schema.sql                        -> (este archivo) crea tablas vacías
--   python python/01b_load_to_postgres.py    -> carga + verifica el CSV
--   sql/01b_constraint_tests.sql             -> (opcional) prueba que los CHECK rechazan datos malos
--   sql/02_metrics_view.sql  ->  sql/03_kpi_views.sql  ->  python python/02_analysis.py
--
-- Es re-ejecutable: borra y recrea las tablas del esquema `pi` (y las vistas que dependan
-- de ellas; después de re-ejecutarlo hay que cargar de nuevo y re-ejecutar 02 y 03).
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 0. ESQUEMA
-- -----------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS pi;

DROP TABLE IF EXISTS pi.pi_cases      CASCADE;
DROP TABLE IF EXISTS pi.report_params CASCADE;


-- -----------------------------------------------------------------------------
-- 1. TABLA PRINCIPAL: pi.pi_cases  (1 fila = 1 caso)
-- -----------------------------------------------------------------------------
-- Convenciones:
--   * NULL en una fecha = el hito AÚN NO ocurre (caso abierto).
--   * Cada CHECK de fechas es "NULL-safe" y además impone la CASCADA de NULLs:
--     una fecha no puede existir si su hito previo es NULL.
--   * Solo 'intake_date' es obligatoria entre las 11 fechas.
-- -----------------------------------------------------------------------------
CREATE TABLE pi.pi_cases (
    -- Identificación -----------------------------------------------------------
    case_id            text          NOT NULL,
    injury_type        text          NOT NULL,
    insurance_company  text          NOT NULL,
    case_manager       text          NOT NULL,

    -- 11 fechas clave ----------------------------------------------------------
    intake_date                              date NOT NULL,
    treatment_completion_date                date,
    medical_records_received_date            date,
    demand_sent_date                         date,
    first_offer_date                         date,
    settlement_accepted_date                 date,
    release_sent_to_client_date              date,
    release_signed_and_sent_to_carrier_date  date,
    settlement_check_received_date           date,
    reductions_confirmed_date                date,   -- pista paralela (no depende del cheque)
    closing_statement_disbursement_date      date,

    -- Montos --------------------------------------------------------------------
    settlement_amount              numeric(12,2),
    attorney_fee_pct               numeric(3,2)  NOT NULL,
    original_medical_bills_amount  numeric(12,2),   -- Medical Bills / LOPs
    final_reduced_bills_amount     numeric(12,2),
    original_liens_amount          numeric(12,2),   -- Liens formales (Medicare, Medicaid, ERISA, hospital)
    final_reduced_liens_amount     numeric(12,2),

    -- ======================= RESTRICCIONES =====================================
    CONSTRAINT pk_pi_cases PRIMARY KEY (case_id),

    CONSTRAINT ck_case_id_format
        CHECK (case_id ~ '^PI-[0-9]{4}-[0-9]{4}$'),

    -- ---- A) Orden cronológico ESTRICTO de la cadena principal (+ cascada de NULLs)
    --      intake < treatment < records < demand < first_offer < accepted
    --             < release_sent < release_signed < check_received
    CONSTRAINT ck_dates_treatment CHECK (
        treatment_completion_date IS NULL
        OR treatment_completion_date > intake_date),

    CONSTRAINT ck_dates_records CHECK (
        medical_records_received_date IS NULL
        OR (treatment_completion_date IS NOT NULL
            AND medical_records_received_date > treatment_completion_date)),

    CONSTRAINT ck_dates_demand CHECK (
        demand_sent_date IS NULL
        OR (medical_records_received_date IS NOT NULL
            AND demand_sent_date > medical_records_received_date)),

    CONSTRAINT ck_dates_first_offer CHECK (
        first_offer_date IS NULL
        OR (demand_sent_date IS NOT NULL
            AND first_offer_date > demand_sent_date)),

    CONSTRAINT ck_dates_accepted CHECK (
        settlement_accepted_date IS NULL
        OR (first_offer_date IS NOT NULL
            AND settlement_accepted_date > first_offer_date)),

    CONSTRAINT ck_dates_release_sent CHECK (
        release_sent_to_client_date IS NULL
        OR (settlement_accepted_date IS NOT NULL
            AND release_sent_to_client_date > settlement_accepted_date)),

    CONSTRAINT ck_dates_release_signed CHECK (
        release_signed_and_sent_to_carrier_date IS NULL
        OR (release_sent_to_client_date IS NOT NULL
            AND release_signed_and_sent_to_carrier_date > release_sent_to_client_date)),

    CONSTRAINT ck_dates_check_received CHECK (
        settlement_check_received_date IS NULL
        OR (release_signed_and_sent_to_carrier_date IS NOT NULL
            AND settlement_check_received_date > release_signed_and_sent_to_carrier_date)),

    -- ---- B) Pista paralela: las reducciones solo se piden después del acuerdo
    --         (NO dependen de Release ni de Cheque)
    CONSTRAINT ck_dates_reductions CHECK (
        reductions_confirmed_date IS NULL
        OR (settlement_accepted_date IS NOT NULL
            AND reductions_confirmed_date > settlement_accepted_date)),

    -- ---- C) CAMINO CRÍTICO: el desembolso exige AMBOS (cheque Y reducciones)
    --         y debe ocurrir DESPUÉS del último de los dos.
    CONSTRAINT ck_dates_disbursement CHECK (
        closing_statement_disbursement_date IS NULL
        OR (settlement_check_received_date IS NOT NULL
            AND reductions_confirmed_date IS NOT NULL
            AND closing_statement_disbursement_date >
                GREATEST(settlement_check_received_date, reductions_confirmed_date))),

    -- ---- D) Honorarios: solo 33% (pre-suit) o 40% (litigation)
    CONSTRAINT ck_fee_pct
        CHECK (attorney_fee_pct IN (0.33, 0.40)),

    -- ---- E) Montos no negativos
    CONSTRAINT ck_amounts_non_negative CHECK (
        (settlement_amount             IS NULL OR settlement_amount             > 0)
        AND (original_medical_bills_amount IS NULL OR original_medical_bills_amount >= 0)
        AND (final_reduced_bills_amount    IS NULL OR final_reduced_bills_amount    >= 0)
        AND (original_liens_amount         IS NULL OR original_liens_amount         >= 0)
        AND (final_reduced_liens_amount    IS NULL OR final_reduced_liens_amount    >= 0)),

    -- ---- F) Una reducción nunca puede AUMENTAR la deuda
    CONSTRAINT ck_final_le_original CHECK (
        (final_reduced_bills_amount IS NULL
            OR final_reduced_bills_amount <= original_medical_bills_amount)
        AND (final_reduced_liens_amount IS NULL
            OR final_reduced_liens_amount <= original_liens_amount)),

    -- ---- G) REGLA DE NEGOCIO: Liens formales no se reducen más de 25%
    --         (comparado en centavos: final >= ROUND(original * 0.75, 2))
    CONSTRAINT ck_lien_reduction_cap CHECK (
        final_reduced_liens_amount IS NULL
        OR final_reduced_liens_amount >= ROUND(original_liens_amount * 0.75, 2)),

    -- ---- H) Un monto existe solo si el hito que lo revela ya ocurrió
    CONSTRAINT ck_settlement_amount_vs_accept CHECK (
        (settlement_accepted_date IS NULL) = (settlement_amount IS NULL)),

    CONSTRAINT ck_original_amounts_vs_records CHECK (
        (medical_records_received_date IS NULL) = (original_medical_bills_amount IS NULL)
        AND (medical_records_received_date IS NULL) = (original_liens_amount IS NULL)),

    CONSTRAINT ck_final_amounts_vs_reductions CHECK (
        (reductions_confirmed_date IS NULL) = (final_reduced_bills_amount IS NULL)
        AND (reductions_confirmed_date IS NULL) = (final_reduced_liens_amount IS NULL)),

    -- ---- I) Caso cerrado: el cliente debe recibir un neto positivo
    CONSTRAINT ck_net_to_client_positive CHECK (
        closing_statement_disbursement_date IS NULL
        OR (settlement_amount * (1 - attorney_fee_pct)
            - final_reduced_bills_amount - final_reduced_liens_amount) > 0)
);

COMMENT ON TABLE  pi.pi_cases IS 'Casos de Personal Injury (1 fila = 1 caso). NULL en fechas = hito aún no alcanzado (caso abierto).';
COMMENT ON COLUMN pi.pi_cases.reductions_confirmed_date IS 'Pista PARALELA al Release/Cheque. Puede bloquear el desembolso si llega después del cheque.';
COMMENT ON COLUMN pi.pi_cases.closing_statement_disbursement_date IS 'NULL = caso abierto. Exige cheque Y reducciones confirmadas (ck_dates_disbursement).';
COMMENT ON COLUMN pi.pi_cases.original_liens_amount IS 'Liens FORMALES (Medicare, Medicaid, ERISA, lien hospitalario). Reducción máx. 25% (ck_lien_reduction_cap).';
COMMENT ON COLUMN pi.pi_cases.original_medical_bills_amount IS 'Medical Bills / LOPs (quiroprácticos, imágenes, especialistas). Margen de negociación 30-40%+.';
COMMENT ON COLUMN pi.pi_cases.attorney_fee_pct IS '0.33 = pre-suit; 0.40 = litigation.';

-- Índices para los filtros/agrupaciones típicos de los dashboards
CREATE INDEX ix_pi_cases_manager   ON pi.pi_cases (case_manager);
CREATE INDEX ix_pi_cases_carrier   ON pi.pi_cases (insurance_company);
CREATE INDEX ix_pi_cases_injury    ON pi.pi_cases (injury_type);
CREATE INDEX ix_pi_cases_intake    ON pi.pi_cases (intake_date);
CREATE INDEX ix_pi_cases_open      ON pi.pi_cases (case_id)
    WHERE closing_statement_disbursement_date IS NULL;           -- casos abiertos


-- -----------------------------------------------------------------------------
-- 2. TABLA DE PARÁMETROS: pi.report_params  (clave/valor tipado)
-- -----------------------------------------------------------------------------
-- Una sola fuente de verdad para la fecha de corte y los umbrales de alerta.
-- Cada fila tiene EXACTAMENTE un valor: numérico (value_num) o fecha (value_date).
-- Las vistas leen directo:  (SELECT value_num FROM pi.report_params WHERE param_key = '<clave>').
-- -----------------------------------------------------------------------------
CREATE TABLE pi.report_params (
    param_key    text          NOT NULL,
    category     text          NOT NULL,
    value_num    numeric,
    value_date   date,
    unit         text,
    description  text          NOT NULL,

    CONSTRAINT pk_report_params PRIMARY KEY (param_key),
    CONSTRAINT ck_param_key_format CHECK (param_key ~ '^[a-z0-9_]+$'),
    CONSTRAINT ck_param_one_value  CHECK (num_nonnulls(value_num, value_date) = 1),
    CONSTRAINT ck_param_category   CHECK (category IN ('cutoff', 'business_rule', 'alert_threshold', 'alert_multiplier'))
);

INSERT INTO pi.report_params (param_key, category, value_num, value_date, unit, description) VALUES
    -- Fecha de corte (snapshot) ---------------------------------------------------
    ('report_cutoff_date',        'cutoff',        NULL, DATE '2026-10-13', 'date',
        'Fecha de corte del reporte: los casos abiertos se miden contra esta fecha (nunca CURRENT_DATE).'),

    -- Reglas de negocio de reducciones --------------------------------------------
    ('lien_reduction_cap_pct',    'business_rule', 0.25, NULL, 'fraction',
        'Tope de reducción de liens formales. El CHECK ck_lien_reduction_cap usa este mismo 25%.'),
    ('bills_reduction_target_pct', 'business_rule', 0.30, NULL, 'fraction',
        'Meta mínima de reducción de Medical Bills/LOPs (rango objetivo 30%-40%+).'),
    ('bills_reduction_alert_floor_pct', 'business_rule', 0.20, NULL, 'fraction',
        'Alerta: reducción de LOPs por debajo de este % = negociación débil, revisar el caso.'),

    -- Umbrales de alerta: días máximos en la FASE ACTUAL de un caso abierto -------
    -- (valores iniciales cercanos al P75-P90 observado; se recalibrarán en las vistas)
    ('alert_days_treatment',          'alert_threshold', 365, NULL, 'days', 'Treatment: tratamiento sin cerrar (riesgo de caso estancado).'),
    ('alert_days_records_wait',       'alert_threshold',  60, NULL, 'days', 'Records Wait: espera de expedientes médicos (EXTERNA).'),
    ('alert_days_demand_prep',        'alert_threshold',  21, NULL, 'days', 'Demand Prep: preparación del demand (CONTROLABLE).'),
    ('alert_days_offer_wait',         'alert_threshold',  60, NULL, 'days', 'Adjuster Offer Wait: espera de la primera oferta (EXTERNA).'),
    ('alert_days_negotiation',        'alert_threshold', 120, NULL, 'days', 'Strategic Negotiation: negociación activa (ESTRATÉGICA).'),
    ('alert_days_release_execution',  'alert_threshold',  14, NULL, 'days', 'Release Execution: envío, firma y retorno del Release (CONTROLABLE).'),
    ('alert_days_carrier_check_wait', 'alert_threshold',  21, NULL, 'days', 'Carrier Check Wait: cheque pendiente tras Release firmado (EXTERNA).'),
    ('alert_days_reductions_blocking','alert_threshold',  30, NULL, 'days', 'Reductions Blocking: días con cheque en mano esperando reducciones (EXTERNA).'),
    ('alert_days_disbursement_prep',  'alert_threshold',   7, NULL, 'days', 'Disbursement Prep: Closing Statement y pago final (CONTROLABLE).'),

    -- Severidad -------------------------------------------------------------------
    ('alert_critical_multiplier',  'alert_multiplier', 1.5, NULL, 'x',
        'Alerta ROJA cuando los días en la fase superan umbral x este multiplicador (ámbar = superar el umbral).'),
    ('alert_watch_pct',    'business_rule', 0.80, NULL, 'fraction',
        'Alertas: un caso pasa a WATCH (alerta temprana) al consumir este % del umbral de su fase actual.'),

    -- Scorecards ------------------------------------------------------------------
    ('scorecard_flag_pct', 'business_rule', 0.20, NULL, 'fraction',
        'Scorecards: se marca SLOWER/FASTER si la mediana se aleja más de este % de la mediana de la firma.'),
    ('min_sample_size',    'business_rule', 15,   NULL, 'cases',
        'Scorecards: con menos casos cerrados que este mínimo la comparación se marca "Low sample".');

-- -----------------------------------------------------------------------------
-- Fin: las tablas existen pero están VACÍAS hasta ejecutar el cargador de Python.
-- -----------------------------------------------------------------------------
SELECT 'Esquema pi creado. Siguiente paso: python python/01b_load_to_postgres.py' AS status;