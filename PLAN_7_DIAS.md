# Plan de ejecución de 7 días — Personal Injury Settlement & Bottleneck Analytics

Tiempo estimado: 2–3 horas por día. El dataset, el esquema SQL, la vista de métricas y el pipeline de Python **ya están construidos y validados**; tu trabajo es ejecutarlos, entenderlos a fondo, visualizar y documentar.

Regla de oro del proyecto: cada hallazgo debe poder defenderse con tu experiencia real. Tus 4 años como Case Manager y Settlement Coordinator son el diferenciador; las métricas son la prueba.

---

## Día 1 — Entorno, repo y comprensión del dataset

- [ ] Instalar PostgreSQL 14+ (y DBeaver o pgAdmin) y Python 3.10+.
- [ ] Crear el repo en GitHub: `pi-settlement-analytics`. Copiar la carpeta del proyecto.
- [ ] `pip install -r requirements.txt` (idealmente dentro de un `venv`).
- [ ] Ejecutar `python python/01_generate_dataset.py` y abrir `data/pi_cases.csv`.
- [ ] Revisar que entiendes cada columna (sección *Data dictionary* del README) y por qué hay `NULL` en los casos abiertos.
- [ ] Primer commit: `chore: initial dataset and project structure`.

**Entregable:** repo con dataset y README base.

## Día 2 — PostgreSQL: esquema, carga y validación

- [ ] `createdb pi_analytics`
- [ ] Desde la raíz del repo: `psql -d pi_analytics -f sql/01_schema_and_load.sql`
  (usa `\copy` con ruta relativa, por eso se ejecuta desde la raíz).
- [ ] Verificar: 200 filas, 34 casos abiertos. Probar que los `CHECK` funcionan: intenta insertar una fila con `closing_statement_date < intake_date` y confirma que la rechaza.
- [ ] Ejecutar `sql/02_metrics_view.sql` y consultar: `SELECT current_stage, COUNT(*) FROM pi.v_case_metrics GROUP BY 1 ORDER BY 1;`
- [ ] Escribir en tus palabras (Notion) qué hace cada bloque de la vista: fases, controlable vs. externo, liens, fee, estancamiento.
- [ ] Commit: `feat(sql): schema with chronological constraints and metrics view`.

**Entregable:** base de datos funcional + vista `pi.v_case_metrics`.

## Día 3 — SQL analítico (KPIs)

- [ ] Ejecutar una por una las 9 consultas de `sql/03_kpi_queries.sql` y anotar la respuesta de negocio de cada una (no solo el número).
- [ ] Añadir 2 consultas propias, por ejemplo: porcentaje de casos litigados (fee 40%) por aseguradora, y *aging* de casos abiertos por etapa.
- [ ] Revisar `EXPLAIN` de la consulta del scorecard por case manager y confirmar que el índice `idx_cases_manager` tiene sentido.
- [ ] Commit: `feat(sql): KPI queries and bottleneck analysis`.

**Entregable:** 9+ consultas documentadas con su interpretación.

## Día 4 — Python: métricas, reporte Excel y gráficos

- [ ] Ejecutar `python python/02_analyze.py`. Confirmar que los números de consola coinciden con SQL (5 alertas activas, 37.1% de reducción de liens, $2.34M en fees).
- [ ] Abrir `reports/PI_Executive_Report.xlsx`. Las hojas de resumen son fórmulas vivas sobre la pestaña `CaseData`: cambia un valor en `CaseData` y observa cómo se recalcula todo.
- [ ] Revisar `reports/charts/*.png`.
- [ ] Opcional: leer directamente desde PostgreSQL en vez del CSV:
  ```python
  from sqlalchemy import create_engine
  engine = create_engine("postgresql+psycopg2://user:pass@localhost/pi_analytics")
  df = pd.read_sql("SELECT * FROM pi.v_case_metrics", engine)
  ```
- [ ] Commit: `feat(python): metrics pipeline, Excel executive report and charts`.

**Entregable:** `PI_Executive_Report.xlsx` + 3 gráficos.

## Día 5 — Power BI: dashboard ejecutivo

Fuente: `data/pi_cases_enriched.csv` (o conexión directa a `pi.v_case_metrics`). Verifica que las columnas de fecha estén tipadas como *Date*.

- [ ] **Página 1 — Executive Overview:** tarjetas (Total cases, Open, Avg cycle days, % Controllable, Lien Savings $, Attorney Fees $), barra apilada de días por fase, tendencia mensual de cierres.
- [ ] **Página 2 — Bottlenecks:** barras *Controllable vs. External* por case manager y por aseguradora; matriz de estancamiento por etapa.
- [ ] **Página 3 — Action List:** tabla filtrada por `active_stagnation_alert = TRUE`, con case_id, case manager, etapa y días sin avance, con formato condicional (rojo > 90 días).
- [ ] Medidas DAX sugeridas:
  ```
  Avg Cycle Days      = AVERAGE(pi_cases_enriched[total_cycle_days])
  Controllable % =
      DIVIDE(SUM(pi_cases_enriched[controllable_days]),
             SUM(pi_cases_enriched[total_cycle_days]))
  Lien Reduction % =
      DIVIDE(SUM(pi_cases_enriched[lien_reduction_savings]),
             CALCULATE(SUM(pi_cases_enriched[original_liens_amount]),
                       pi_cases_enriched[case_status] = "Closed"))
  Active Alerts       = CALCULATE(COUNTROWS(pi_cases_enriched),
                                  pi_cases_enriched[active_stagnation_alert] = TRUE())
  ```
- [ ] Segmentadores: injury_type, case_manager, insurance_company, año de intake.
- [ ] Exportar capturas a `reports/dashboard/` y guardar el `.pbix`.
- [ ] Commit: `feat(bi): Power BI executive dashboard`.

**Entregable:** `.pbix` de 3 páginas + capturas.

## Día 6 — Insights y recomendaciones operativas

Convierte los números en decisiones, como lo haría un Settlement Coordinator con su director de operaciones:

- [ ] **Cuello de botella interno:** el cierre (settlement a closing statement) tarda el doble que la preparación de la demanda. Propón acciones: checklist de release y W-9, solicitud de lien finals en paralelo al acuerdo, seguimiento semanal de cheques.
- [ ] **Brecha entre case managers:** 26 días de diferencia en tiempo controlable. Plantea capacitación cruzada y estandarización, no castigo.
- [ ] **Espera externa:** protocolo de follow-up a los 30 días de la demanda y a los 21 días de la solicitud de records; escalamiento a supervisor del adjuster antes de los 45 días.
- [ ] **Lista de acción:** las 5 alertas activas, ordenadas por días sin avance, con la próxima acción concreta para cada una.
- [ ] Estimar el impacto: si el tiempo controlable del equipo se acerca al del mejor case manager, ¿cuántos días por caso y cuánto *fee revenue* se adelanta?
- [ ] Redactar 1 página de *Executive Summary* (problema, método, 3 hallazgos, 3 recomendaciones).

**Entregable:** `reports/executive_summary.md` (o PDF).

## Día 7 — Documentación y publicación (GitHub + Notion)

**GitHub**
- [ ] Pulir `README.md`: añadir las capturas del dashboard y los 3 gráficos; mantener la nota de que los datos son sintéticos.
- [ ] Añadir `.gitignore` (`__pycache__/`, `.venv/`, `.DS_Store`, `~$*.xlsx`) y una licencia (MIT).
- [ ] Verificar que alguien puede clonar el repo y reproducirlo siguiendo solo el *Quick start*.
- [ ] Release/tag `v1.0`; fijar el repo en tu perfil.

**Notion** — crea una página tipo *case study* con esta estructura:
1. Contexto: por qué un firm de PI necesita medir cuellos de botella.
2. Preguntas de negocio.
3. Decisiones de diseño (por qué `Controllable_Days` excluye tratamiento y espera de records; historical vs. active stagnation; fee sobre gross).
4. Arquitectura del pipeline (diagrama).
5. Hallazgos y recomendaciones (con gráficos).
6. Qué haría con datos reales (costos del caso, fecha de litigio, tipo de lien, monto de demanda).
7. Enlaces: repo, dashboard, reporte Excel.

**Presentación del proyecto**
- [ ] Preparar un pitch de 2 minutos: *"Analicé 200 casos sintéticos con la estructura real de un firm de PI y encontré que solo el 16% del ciclo es controlable por el equipo; dentro de eso, el cierre tarda el doble que la demanda..."*
- [ ] Agregar a tu CV/LinkedIn 2–3 bullets con métricas (ej.: *"Built a PostgreSQL + Python + Power BI pipeline that attributes each day of the PI case lifecycle to firm-controllable vs. external waits and flags stagnant cases"*).

**Entregable:** repo público, página Notion y bullets de CV listos.

---

## Checklist final

- [ ] Datos sintéticos claramente identificados como tales.
- [ ] Mismos números en SQL, Python, Excel y Power BI.
- [ ] Terminología correcta: demand package, MMI, LOP, policy limits, lien reduction, closing/settlement statement, disbursement, pre-suit vs. litigation.
- [ ] Todo reproducible desde el README.
