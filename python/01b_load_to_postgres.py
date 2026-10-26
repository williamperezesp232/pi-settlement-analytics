#!/usr/bin/env python3
"""
01b_load_to_postgres.py
=======================
Proyecto: Legal Operations & Analytics - Personal Injury (Plaintiff-side, USA)

Carga data/pi_cases.csv en la tabla pi.pi_cases de PostgreSQL usando pandas (to_sql).
Reemplaza al antiguo \\copy de 01_schema_and_load.sql, que no funcionaba desde SQLTools
(los comandos \\copy/\\set son de la terminal psql y COPY de servidor no ve tus archivos).

Qué hace (todo en UNA transacción: si algo falla, no queda nada a medias):
  1. Lee y valida el CSV (columnas exactas, tipos, fechas).
  2. TRUNCATE de pi.pi_cases  ->  el script es re-ejecutable.
  3. Inserta con df.to_sql(if_exists="append")  ->  respeta la tabla y TODOS sus CHECK.
     (NUNCA usar if_exists="replace": borraría las restricciones de integridad.)
  4. Verifica contra el CSV: conteos, NULLs por columna, fechas <= fecha de corte.

Requisitos:
    pip install pandas sqlalchemy pg8000
    Haber ejecutado antes sql/01_schema.sql (crea las tablas vacías).

Driver: por defecto **pg8000** (Python puro: sin .pyd/.dll, no lo bloquea el Control de
Aplicaciones de Windows). Para usar otro: PI_DB_DRIVER=psycopg | psycopg2 (ver db_conn.py).

Conexión (variables de entorno; todas opcionales; la lógica vive en db_conn.py):
    DATABASE_URL  = postgresql://usuario:clave@host:5432/pi_analytics
    ó bien PGHOST (localhost), PGPORT (5432), PGUSER (postgres), PGPASSWORD (""),
           PGDATABASE (pi_analytics)

Uso (desde la raíz del proyecto):
    python python/01b_load_to_postgres.py
    python python/01b_load_to_postgres.py --csv otra/ruta/pi_cases.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
from pandas.errors import DatabaseError as PandasDatabaseError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from db_conn import DriverError, get_engine, pg_error_info    # conexión compartida (driver sin binarios)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV = ROOT / "data" / "pi_cases.csv"
SCHEMA, TABLE = "pi", "pi_cases"

# Columnas EXACTAS y en orden (mismo diccionario que 01_generate_dataset.py y 01_schema.sql)
DATE_COLS = [
    "intake_date", "treatment_completion_date", "medical_records_received_date",
    "demand_sent_date", "first_offer_date", "settlement_accepted_date",
    "release_sent_to_client_date", "release_signed_and_sent_to_carrier_date",
    "settlement_check_received_date", "reductions_confirmed_date",
    "closing_statement_disbursement_date",
]
MONEY_COLS = [
    "settlement_amount", "attorney_fee_pct", "original_medical_bills_amount",
    "final_reduced_bills_amount", "original_liens_amount", "final_reduced_liens_amount",
]
TEXT_COLS = ["case_id", "injury_type", "insurance_company", "case_manager"]
ALL_COLS = TEXT_COLS + DATE_COLS + MONEY_COLS


# ──────────────────────────────────────────────────────────────────────────────
def short(text_: str, n: int = 400) -> str:
    text_ = " ".join(str(text_).split())
    return text_ if len(text_) <= n else text_[: n - 1] + "…"


def read_csv(path: Path) -> pd.DataFrame:
    """Lee el CSV y lo deja con los tipos que espera PostgreSQL."""
    if not path.exists():
        sys.exit(f"No existe {path}. Ejecuta primero: python python/01_generate_dataset.py")

    df = pd.read_csv(path)
    if list(df.columns) != ALL_COLS:
        faltan = [c for c in ALL_COLS if c not in df.columns]
        sobran = [c for c in df.columns if c not in ALL_COLS]
        sys.exit(f"El CSV no coincide con el diccionario de columnas.\n  Faltan: {faltan}\n  Sobran: {sobran}")

    for col in DATE_COLS:                                   # 'YYYY-MM-DD' -> date (vacío -> NULL)
        df[col] = pd.to_datetime(df[col], format="%Y-%m-%d", errors="raise").dt.date
    for col in MONEY_COLS:
        df[col] = pd.to_numeric(df[col], errors="raise")    # vacío -> NaN -> NULL
    if df["case_id"].duplicated().any():
        sys.exit("El CSV tiene case_id duplicados.")
    return df


def null_counts_csv(df: pd.DataFrame) -> dict[str, int]:
    return {c: int(df[c].isna().sum()) for c in DATE_COLS + MONEY_COLS}


def verify(conn, df: pd.DataFrame) -> dict:
    """Compara lo que quedó en la BD contra el CSV. Lanza RuntimeError si algo no calza."""
    total, closed = conn.execute(text(
        f"SELECT count(*), count(closing_statement_disbursement_date) FROM {SCHEMA}.{TABLE}")).one()
    exp_total = len(df)
    exp_closed = int(df["closing_statement_disbursement_date"].notna().sum())
    if (total, closed) != (exp_total, exp_closed):
        raise RuntimeError(f"Conteos distintos: BD={total}/{closed} vs CSV={exp_total}/{exp_closed} (total/cerrados)")

    # NULLs por columna: la BD debe tener exactamente los mismos huecos que el CSV
    select = ", ".join(f"count(*) FILTER (WHERE {c} IS NULL) AS {c}" for c in DATE_COLS + MONEY_COLS)
    db_nulls = dict(conn.execute(text(f"SELECT {select} FROM {SCHEMA}.{TABLE}")).mappings().one())
    csv_nulls = null_counts_csv(df)
    diffs = {c: (db_nulls[c], csv_nulls[c]) for c in csv_nulls if int(db_nulls[c]) != csv_nulls[c]}
    if diffs:
        raise RuntimeError(f"NULLs por columna no coinciden (BD, CSV): {diffs}")

    # Ninguna fecha puede ser posterior a la fecha de corte de pi.report_params
    cutoff = conn.execute(text(
        f"SELECT value_date FROM {SCHEMA}.report_params WHERE param_key = 'report_cutoff_date'")).scalar()
    if cutoff is None:
        raise RuntimeError("Falta report_cutoff_date en pi.report_params (¿se ejecutó 01_schema.sql?)")
    greatest = ", ".join(DATE_COLS)
    future = conn.execute(text(
        f"SELECT count(*) FROM {SCHEMA}.{TABLE} WHERE GREATEST({greatest}) > :c"), {"c": cutoff}).scalar()
    if future:
        raise RuntimeError(f"{future} casos tienen fechas posteriores al corte {cutoff}")

    return {"total": total, "closed": closed, "open": total - closed, "cutoff": cutoff}


def main() -> None:
    parser = argparse.ArgumentParser(description="Carga data/pi_cases.csv en pi.pi_cases")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="ruta del CSV (default: data/pi_cases.csv)")
    args = parser.parse_args()

    df = read_csv(args.csv)
    print(f"CSV leído: {len(df)} filas, {len(df.columns)} columnas ({args.csv})")

    try:
        engine = get_engine()
    except DriverError as e:
        sys.exit(str(e))
    try:
        with engine.begin() as conn:                         # una transacción: commit al salir sin error
            exists = conn.execute(text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = :s AND table_name = :t"), {"s": SCHEMA, "t": TABLE}).scalar()
            if not exists:
                sys.exit(f"No existe {SCHEMA}.{TABLE}. Ejecuta primero sql/01_schema.sql")

            conn.execute(text(f"TRUNCATE {SCHEMA}.{TABLE}"))  # re-ejecutable; conserva tabla y CHECK
            df.to_sql(TABLE, conn, schema=SCHEMA, if_exists="append", index=False,
                      method="multi", chunksize=100)
            result = verify(conn, df)                        # si falla -> excepción -> ROLLBACK
    except RuntimeError as e:
        sys.exit(f"Verificación fallida; se revirtió la carga. {e}")
    except (PandasDatabaseError, SQLAlchemyError) as e:
        info = pg_error_info(e)
        if info and info["code"].startswith("23"):                    # clase 23 = violación de integridad
            sys.exit(f"PostgreSQL rechazó los datos: restricción '{info['constraint']}' violada. "
                     f"No se cargó nada (se revirtió la transacción).\n"
                     f"Fila problemática: {short(info['detail'] or info['message'] or '')}")
        if info:                                                      # otro error del servidor (SQL, permisos…)
            sys.exit(f"PostgreSQL devolvió un error ({info['code']}): {short(info['message'] or '')}")
        sys.exit("No se pudo conectar a PostgreSQL. Revisa DATABASE_URL o PGHOST/PGPORT/PGUSER/"
                 f"PGPASSWORD/PGDATABASE.\nDetalle: {short(e)}")

    print(f"Carga OK: {result['total']} casos ({result['closed']} cerrados / {result['open']} abiertos), "
          f"fecha de corte {result['cutoff']}")
    print("Verificado: conteos, NULLs por columna y fechas <= corte coinciden con el CSV.")
    print("Siguiente paso: ejecutar sql/02_metrics_view.sql y luego sql/03_kpi_views.sql")


if __name__ == "__main__":
    main()