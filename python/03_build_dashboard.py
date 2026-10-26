#!/usr/bin/env python3
"""
03_build_dashboard.py
=====================
Proyecto: Legal Operations & Analytics - Personal Injury (Plaintiff-side, USA)

Genera dashboard/index.html: un dashboard interactivo AUTOCONTENIDO (un solo archivo HTML, sin
servidor, sin internet, sin librerías externas) con la vista de alertas en vivo de los casos
abiertos. Se abre con doble clic en cualquier navegador.

PYTHON PURO, SIN BINARIOS
-------------------------
Este script solo usa la biblioteca estándar + pg8000 (driver 100% Python). NO importa
SQLAlchemy, pandas, numpy, pyarrow ni Streamlit, es decir, nada con extensiones compiladas
(.pyd / Cython) que una directiva de Control de Aplicaciones de Windows pueda bloquear.

    pip install pg8000

Qué hace
  1. Se conecta a PostgreSQL y verifica la base (reconciliación = 0; alertas = casos abiertos).
  2. Lee las vistas del proyecto:  pi.v_open_case_alerts, pi.v_case_phases, pi.v_phase_summary,
     pi.v_manager_scorecard, pi.v_carrier_scorecard y pi.report_params.
  3. Incrusta los datos como JSON en dashboard/template.html  ->  dashboard/index.html.
     Los filtros, el orden, los umbrales "what-if", el detalle del caso y la exportación a CSV
     funcionan en el navegador (JavaScript), sin volver a consultar la base.

El HTML es una FOTO (snapshot) de la base en el momento de generarlo: para refrescar, vuelve a
ejecutar este script.

Conexión (variables de entorno; todas opcionales):
    DATABASE_URL  = postgresql://usuario:clave@host:5432/pi_analytics
    ó bien PGHOST (localhost), PGPORT (5432), PGUSER (postgres), PGPASSWORD (""),
           PGDATABASE (pi_analytics)

Uso (desde la raíz del proyecto):
    python python/03_build_dashboard.py
    python python/03_build_dashboard.py --out docs/index.html     # p. ej. para GitHub Pages
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote, urlparse

try:
    import pg8000.dbapi as pg
except ImportError:
    sys.exit("Falta el driver pg8000 (Python puro). Instálalo con:  pip install pg8000")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TEMPLATE = ROOT / "dashboard" / "template.html"
DEFAULT_OUT = ROOT / "dashboard" / "index.html"
PLACEHOLDER = "/*__DATA__*/null"

PHASE_KEYS = ["treatment", "records_wait", "demand_prep", "offer_wait", "negotiation",
              "release_execution", "carrier_check_wait", "reductions_blocking", "disbursement_prep"]
PARAM_KEYS = ("report_cutoff_date", "alert_watch_pct", "alert_critical_multiplier")


# ──────────────────────────────────────────────────────────────────────────────
# Conexión y consultas (sin SQLAlchemy)
# ──────────────────────────────────────────────────────────────────────────────
def connect():
    """Conexión pg8000 a partir de DATABASE_URL o de las variables PG*."""
    raw = os.getenv("DATABASE_URL")
    if raw:
        u = urlparse(raw.replace("postgresql+pg8000://", "postgresql://", 1))
        cfg = dict(user=unquote(u.username or "postgres"), password=unquote(u.password) if u.password else None,
                   host=u.hostname or "localhost", port=u.port or 5432, database=(u.path or "/pi_analytics")[1:])
    else:
        cfg = dict(user=os.getenv("PGUSER", "postgres"), password=os.getenv("PGPASSWORD") or None,
                   host=os.getenv("PGHOST", "localhost"), port=int(os.getenv("PGPORT", "5432")),
                   database=os.getenv("PGDATABASE", "pi_analytics"))
    try:
        return pg.connect(**cfg)
    except Exception as e:  # noqa: BLE001  (mensaje claro en vez de traceback)
        sys.exit("No se pudo conectar a PostgreSQL. Revisa DATABASE_URL o PGHOST/PGPORT/PGUSER/PGPASSWORD/"
                 f"PGDATABASE.\nDetalle: {' '.join(str(e).split())[:300]}")


def fetch(cur, sql: str) -> list[dict]:
    cur.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def to_json_value(o):
    """Decimal -> float; date/datetime -> ISO; el resto lo maneja json."""
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError(f"Tipo no serializable: {type(o).__name__}")


def build_payload(cur) -> dict:
    # 1) Verificación previa: falla rápido si la base no está lista
    rec = fetch(cur, "SELECT * FROM pi.v_reconciliation_check")[0]
    bad = [c for c in ("cases_with_diff", "cases_with_bad_phase", "cases_blocking_gt_parallel",
                       "cases_with_class_diff") if rec[c] != 0]
    if rec["closed_cases"] == 0 or bad:
        sys.exit(f"Reconciliación fallida o sin datos: {rec}. Ejecuta sql/01_schema.sql, "
                 "python/01b_load_to_postgres.py y sql/02 y 03.")

    alerts = fetch(cur, "SELECT * FROM pi.v_open_case_alerts ORDER BY priority_rank")
    n_open = fetch(cur, "SELECT count(*) AS n FROM pi.v_case_phases WHERE NOT is_closed")[0]["n"]
    if len(alerts) != n_open:
        sys.exit(f"v_open_case_alerts ({len(alerts)}) no coincide con los casos abiertos ({n_open}).")

    # 2) Parámetros
    params = {r["param_key"]: r for r in fetch(
        cur, "SELECT param_key, value_num, value_date FROM pi.report_params WHERE param_key IN "
             "(" + ", ".join(f"'{k}'" for k in PARAM_KEYS) + ")")}
    if "report_cutoff_date" not in params or params["report_cutoff_date"]["value_date"] is None:
        sys.exit("Falta report_cutoff_date en pi.report_params")
    num = lambda k, d: float(params[k]["value_num"]) if k in params and params[k]["value_num"] is not None else d  # noqa: E731

    # 3) Tiempos por fase de cada caso abierto (para el gráfico de detalle del caso)
    cols = ", ".join(f"{k}_days" for k in PHASE_KEYS)
    detail = {r["case_id"]: {k: v for k, v in r.items() if k != "case_id"}
              for r in fetch(cur, f"SELECT case_id, {cols} FROM pi.v_case_phases WHERE NOT is_closed")}

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "params": {"cutoff": params["report_cutoff_date"]["value_date"],
                   "watch": num("alert_watch_pct", 0.80), "red": num("alert_critical_multiplier", 1.5)},
        "alerts": alerts,
        "detail": detail,
        "phase_summary": fetch(cur, "SELECT phase_key, phase_name, phase_type, avg_days, median_days, p90_days "
                                    "FROM pi.v_phase_summary ORDER BY phase_order"),
        "managers": fetch(cur, "SELECT * FROM pi.v_manager_scorecard"),
        "carriers": fetch(cur, "SELECT * FROM pi.v_carrier_scorecard"),
        "closed_cases": int(rec["closed_cases"]),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Genera el dashboard HTML autocontenido desde PostgreSQL")
    ap.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    if not args.template.exists():
        sys.exit(f"No existe la plantilla {args.template}")
    html = args.template.read_text(encoding="utf-8")
    if PLACEHOLDER not in html:
        sys.exit(f"La plantilla no contiene el marcador {PLACEHOLDER}")

    con = connect()
    try:
        payload = build_payload(con.cursor())
    finally:
        con.close()

    # "</" -> "<\/" evita que un dato cierre la etiqueta <script> por accidente
    data = json.dumps(payload, default=to_json_value, ensure_ascii=False,
                      separators=(",", ":")).replace("</", "<\\/")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html.replace(PLACEHOLDER, data, 1), encoding="utf-8")

    levels = {}
    for a in payload["alerts"]:
        levels[a["alert_level"]] = levels.get(a["alert_level"], 0) + 1
    size_kb = args.out.stat().st_size / 1024
    print(f"Dashboard generado: {args.out}  ({size_kb:.0f} KB, autocontenido)")
    print(f"  {len(payload['alerts'])} casos abiertos | alertas: {levels} | corte {payload['params']['cutoff']}")
    print("  Ábrelo con doble clic en el navegador. Para refrescar los datos, vuelve a ejecutar este script.")


if __name__ == "__main__":
    main()