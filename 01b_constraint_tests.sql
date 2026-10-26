-- =============================================================================
-- 01b_constraint_tests.sql   (OPCIONAL)
-- Proyecto : Legal Operations & Analytics - Personal Injury (Plaintiff-side)
-- Requiere : sql/01_schema.sql  y  python/01b_load_to_postgres.py (tabla cargada)
--
-- Comprueba que las restricciones de pi.pi_cases RECHAZAN datos inválidos.
-- Cada prueba clona un caso cerrado real, lo "rompe" y exige que falle con el CHECK
-- esperado. Nada se persiste: los inserts fallan por diseño.
--
-- Compatible con SQLTools/DBeaver/pgAdmin: es UN solo bloque DO (sin funciones temporales
-- ni meta-comandos). Si todo pasa, la última consulta muestra 'OK'; si algo falla, el
-- cliente muestra el error con la prueba que no se comportó como se esperaba.
-- =============================================================================

DO $$
DECLARE
    r     pi.pi_cases%ROWTYPE;
    t     record;
    v_con text;
    n_ok  int := 0;
    n_exp int := 0;
BEGIN
    SELECT * INTO r
      FROM pi.pi_cases
     WHERE closing_statement_disbursement_date IS NOT NULL
       AND original_liens_amount > 1000
     ORDER BY case_id
     LIMIT 1;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'No hay casos cerrados con liens: carga primero el dataset (python python/01b_load_to_postgres.py)';
    END IF;
    r.case_id := 'PI-9999-0001';

    FOR t IN
        SELECT * FROM (VALUES
            -- 1) Reducción de liens de 30% (> tope 25%)
            ('lien reducido 30%', 'ck_lien_reduction_cap',
                jsonb_build_object('final_reduced_liens_amount', ROUND(r.original_liens_amount * 0.70, 2))),

            -- 2) Desembolso SIN reducciones confirmadas (el cheque sí existe)
            ('desembolso sin reducciones', 'ck_dates_disbursement',
                jsonb_build_object('reductions_confirmed_date', NULL,
                                   'final_reduced_bills_amount', NULL,
                                   'final_reduced_liens_amount', NULL)),

            -- 3) Desembolso SIN cheque recibido
            ('desembolso sin cheque', 'ck_dates_disbursement',
                jsonb_build_object('settlement_check_received_date', NULL)),

            -- 4) Desembolso el mismo día que la última pista (debe ser estrictamente posterior)
            ('desembolso <= max(cheque, reducciones)', 'ck_dates_disbursement',
                jsonb_build_object('closing_statement_disbursement_date',
                    GREATEST(r.settlement_check_received_date, r.reductions_confirmed_date))),

            -- 5) Fechas fuera de orden: Release firmado ANTES de ser enviado
            ('release firmado antes de enviado', 'ck_dates_release_signed',
                jsonb_build_object('release_signed_and_sent_to_carrier_date',
                    r.release_sent_to_client_date - 1)),

            -- 6) Cascada de NULLs: fecha de demand sin records recibidos
            ('demand sin records', 'ck_dates_demand',
                jsonb_build_object('medical_records_received_date', NULL,
                                   'original_medical_bills_amount', NULL,
                                   'original_liens_amount', NULL)),

            -- 7) Honorarios fuera de {0.33, 0.40}
            ('fee 35%', 'ck_fee_pct',
                jsonb_build_object('attorney_fee_pct', 0.35))
        ) AS v(label, expected, patch)
    LOOP
        n_exp := n_exp + 1;
        BEGIN
            INSERT INTO pi.pi_cases SELECT (jsonb_populate_record(r, t.patch)).*;
            RAISE EXCEPTION 'PRUEBA FALLIDA: [%] fue ACEPTADO y debía rechazarse', t.label
                  USING ERRCODE = 'P0002';
        EXCEPTION WHEN check_violation THEN
            GET STACKED DIAGNOSTICS v_con = CONSTRAINT_NAME;
            IF v_con IS DISTINCT FROM t.expected THEN
                RAISE EXCEPTION 'PRUEBA FALLIDA: [%] rechazado por "%" pero se esperaba "%"',
                                t.label, v_con, t.expected;
            END IF;
            n_ok := n_ok + 1;
        END;
    END LOOP;

    IF n_ok <> n_exp THEN
        RAISE EXCEPTION 'Solo % de % pruebas pasaron', n_ok, n_exp;
    END IF;
    RAISE NOTICE 'Constraint tests OK: % de % restricciones críticas rechazan datos inválidos.', n_ok, n_exp;
END $$;

SELECT 'OK: las 7 restricciones críticas rechazan datos inválidos' AS constraint_tests;