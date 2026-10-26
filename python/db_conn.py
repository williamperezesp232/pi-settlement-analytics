"""
db_conn.py
==========
Conexión a PostgreSQL COMPARTIDA por 01b_load_to_postgres.py y 02_analysis.py.

Por qué existe
--------------
En Windows, una directiva de Control de Aplicaciones (App Control / WDAC) puede bloquear las
extensiones compiladas (.pyd) de drivers como psycopg2. Por eso el driver por defecto es
**pg8000**: es 100% Python (wheel py3-none-any; sin .pyd, sin .dll, sin libpq).

Driver (variable de entorno PI_DB_DRIVER; por defecto "pg8000"):
    pg8000     pip install pg8000                  -> Python puro, sin binarios (RECOMENDADO en Windows)
    psycopg    pip install "psycopg[binary]"       -> psycopg v3; incluye un .pyd propio (puede bloquearse igual)
    psycopg2   pip install psycopg2-binary         -> el que Windows bloqueó

Conexión (variables de entorno; todas opcionales):
    DATABASE_URL  = postgresql://usuario:clave@host:5432/pi_analytics
                    (si no indicas "+driver", se usa el de PI_DB_DRIVER; nunca psycopg2 por omisión)
    ó bien PGHOST (localhost), PGPORT (5432), PGUSER (postgres), PGPASSWORD (""),
           PGDATABASE (pi_analytics)
"""

from __future__ import annotations

import importlib.util
import os

from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Engine, make_url

DEFAULT_DRIVER = "pg8000"
INSTALL_HINT = {
    "pg8000": "pip install pg8000",
    "psycopg": 'pip install "psycopg[binary]"',
    "psycopg2": "pip install psycopg2-binary",
}


class DriverError(RuntimeError):
    """El driver no está instalado, no existe o el sistema bloqueó su carga."""


def get_engine() -> Engine:
    """Crea el Engine de SQLAlchemy con el driver elegido (por defecto, pg8000)."""
    wanted = os.getenv("PI_DB_DRIVER", DEFAULT_DRIVER).strip().lower()
    if wanted not in INSTALL_HINT:
        raise DriverError(f"PI_DB_DRIVER='{wanted}' no es válido. Opciones: {', '.join(INSTALL_HINT)}")

    raw_url = os.getenv("DATABASE_URL")
    if raw_url:
        url = make_url(raw_url)
        if url.drivername in ("postgresql", "postgres"):      # sin "+driver": SQLAlchemy usaría psycopg2
            url = url.set(drivername=f"postgresql+{wanted}")
    else:
        url = URL.create(
            f"postgresql+{wanted}",
            username=os.getenv("PGUSER", "postgres"),
            password=os.getenv("PGPASSWORD") or None,
            host=os.getenv("PGHOST", "localhost"),
            port=int(os.getenv("PGPORT", "5432")),
            database=os.getenv("PGDATABASE", "pi_analytics"),
        )

    driver = url.drivername.split("+", 1)[1] if "+" in url.drivername else wanted
    if importlib.util.find_spec(driver) is None:
        raise DriverError(f"El driver '{driver}' no está instalado. Instálalo con:  "
                          f"{INSTALL_HINT.get(driver, 'pip install ' + driver)}")
    try:
        return create_engine(url)                             # aquí SQLAlchemy importa el driver
    except (ImportError, OSError) as e:                       # p. ej. "DLL load failed ... bloqueado"
        raise DriverError(
            f"No se pudo cargar el driver '{driver}' (¿bloqueado por Windows?): {e}\n"
            f"Usa un driver sin binarios:  pip install pg8000   y   PI_DB_DRIVER=pg8000") from e


def pg_error_info(exc: BaseException | None) -> dict | None:
    """
    Extrae del error el detalle ORIGINAL de PostgreSQL, sea cual sea el driver:
      * psycopg2 : exc.pgcode  + exc.diag.constraint_name / message_detail
      * psycopg 3: exc.sqlstate + exc.diag.constraint_name / message_detail
      * pg8000   : exc.args[0] es un dict con campos 'C' (código), 'n' (constraint), 'D' (detalle)
    Recorre la cadena de excepciones (pandas y SQLAlchemy envuelven el error original).
    Devuelve {"code", "constraint", "detail", "message"} o None si no es un error del servidor
    (por ejemplo, un fallo de conexión).
    """
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        code = getattr(exc, "pgcode", None) or getattr(exc, "sqlstate", None)
        if code:
            diag = getattr(exc, "diag", None)
            return {"code": str(code),
                    "constraint": getattr(diag, "constraint_name", None),
                    "detail": getattr(diag, "message_detail", None),
                    "message": getattr(diag, "message_primary", None) or str(exc)}
        first = exc.args[0] if getattr(exc, "args", None) else None
        if isinstance(first, dict) and "C" in first:           # pg8000
            return {"code": str(first["C"]), "constraint": first.get("n"),
                    "detail": first.get("D"), "message": first.get("M")}
        exc = getattr(exc, "orig", None) or exc.__cause__ or exc.__context__
    return None