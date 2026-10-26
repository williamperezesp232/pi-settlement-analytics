#!/usr/bin/env python3
"""
01_generate_dataset.py
======================
Proyecto: Legal Operations & Analytics - Personal Injury (Plaintiff-side, USA)

Genera un dataset sintético de 200 casos (164 cerrados + 36 abiertos) con:
  * 9 fases de tiempo (Treatment -> Disbursement Prep), incluyendo las
    subfases post-acuerdo con su camino crítico (Release -> Check || Reductions).
  * Segregación de deuda médica: Liens formales (tope 25%) vs Medical Bills/LOPs.
  * 36 casos abiertos con NULLs reales en fechas, montos finales e hitos futuros.

Modelo de tiempos (offsets en días desde intake):

    intake ─► treatment ─► records ─► demand ─► offer ─► negotiation ─► ACCEPTED
                                                                           │
                              ┌────────────────────────────────────────────┤
                              │  Track A (controlable + externa)           │  Track B (externa, paralela)
                              │  release_sent ─► release_signed ─► CHECK   │  REDUCTIONS_CONFIRMED
                              └──────────────────────┬─────────────────────┘
                                                     ▼
                                 ready = max(CHECK, REDUCTIONS_CONFIRMED)
                                                     ▼
                                          Disbursement Prep ─► DISBURSEMENT

  Las 9 fases del camino crítico suman EXACTAMENTE el ciclo total:
      treatment + records + demand + offer_wait + negotiation
    + release_execution + carrier_check_wait + reductions_blocking
    + disbursement_prep  ==  (disbursement_date - intake_date)

  donde reductions_blocking = max(0, reductions_confirmed - check_received).

Uso (desde la raíz del proyecto):
    pip install pandas numpy faker
    python python/01_generate_dataset.py

Salida:
    data/pi_cases.csv   (NULL = celda vacía)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from faker import Faker

# ──────────────────────────────────────────────────────────────────────────────
# 1. CONFIGURACIÓN GLOBAL
# ──────────────────────────────────────────────────────────────────────────────
SEED = 42
N_TOTAL = 200
N_OPEN = 36
N_CLOSED = N_TOTAL - N_OPEN                      # 164

CUTOFF_DATE = date(2026, 10, 13)                 # fecha de corte (snapshot)
EARLIEST_INTAKE = date(2023, 1, 1)

FEE_PRE_SUIT = 0.33
FEE_LITIGATION = 0.40

LIEN_REDUCTION_CAP = 0.25                        # liens formales: rara vez > 25%
BILLS_REDUCTION_CAP = 0.60                       # LOPs: techo de sanidad
MAX_DEBT_TO_SETTLEMENT = 0.45                    # (bills + liens) <= 45% del settlement

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = ROOT / "data" / "pi_cases.csv"

# Fases del camino crítico, en orden. (key, nombre, tipo)
# Tipos:  Controllable = la firma controla el tiempo
#         External     = depende de terceros (aseguradora, proveedores, billing)
#         Clinical / Strategic = no se miden como "eficiencia" del equipo
PHASES: list[tuple[str, str, str]] = [
    ("treatment",          "Treatment",              "Clinical"),
    ("records_wait",       "Records Wait",           "External"),
    ("demand_prep",        "Demand Prep",            "Controllable"),
    ("offer_wait",         "Adjuster Offer Wait",    "External"),
    ("negotiation",        "Strategic Negotiation",  "Strategic"),
    ("release_execution",  "Release Execution",      "Controllable"),
    ("carrier_check_wait", "Carrier Check Wait",     "External"),
    ("reductions_blocking", "Reductions Blocking",   "External"),
    ("disbursement_prep",  "Disbursement Prep",      "Controllable"),
]
PHASE_KEYS = [p[0] for p in PHASES]

# Distribución planeada de los 36 casos abiertos según su fase ACTUAL (en el
# mismo orden que PHASES). Garantiza que cada tipo de NULL quede ejercitado.
OPEN_STAGE_PLAN = [11, 4, 3, 5, 5, 3, 2, 2, 1]
assert sum(OPEN_STAGE_PLAN) == N_OPEN

# Candidatos por caso abierto para el muestreo sesgado por duración (ver generate_dataset).
N_OPEN_CANDIDATES = 80

DATE_COLS = [
    "intake_date",
    "treatment_completion_date",
    "medical_records_received_date",
    "demand_sent_date",
    "first_offer_date",
    "settlement_accepted_date",
    "release_sent_to_client_date",
    "release_signed_and_sent_to_carrier_date",
    "settlement_check_received_date",
    "reductions_confirmed_date",
    "closing_statement_disbursement_date",
]
MONEY_COLS = [
    "settlement_amount",
    "attorney_fee_pct",
    "original_medical_bills_amount",
    "final_reduced_bills_amount",
    "original_liens_amount",
    "final_reduced_liens_amount",
]
ID_COLS = ["case_id", "injury_type", "insurance_company", "case_manager"]
ALL_COLS = ID_COLS + DATE_COLS + MONEY_COLS


# ──────────────────────────────────────────────────────────────────────────────
# 2. PERFILES DE NEGOCIO (parámetros que crean cuellos de botella reales)
# ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class InjuryProfile:
    name: str
    weight: float             # frecuencia relativa en la cartera
    treat_median: int         # días de tratamiento (mediana)
    bills_median: float       # LOPs / medical bills originales (mediana, USD)
    settle_mult: float        # múltiplo settlement / deuda médica total
    lien_prob: float          # prob. de tener liens formales
    litigation_prob: float    # prob. de estar en litigio (fee 40%)


INJURIES = [
    InjuryProfile("Auto - Soft Tissue",         0.30, 120,  11_000, 2.9, 0.25, 0.08),
    InjuryProfile("Auto - Spine/Injections",    0.17, 270,  38_000, 3.0, 0.40, 0.25),
    InjuryProfile("Auto - Fracture/Surgery",    0.12, 330,  95_000, 2.7, 0.65, 0.38),
    InjuryProfile("Motorcycle Accident",        0.08, 300,  85_000, 2.8, 0.60, 0.35),
    InjuryProfile("Pedestrian/Bicycle",         0.06, 260,  70_000, 2.9, 0.60, 0.30),
    InjuryProfile("Slip and Fall",              0.12, 210,  32_000, 3.2, 0.35, 0.28),
    InjuryProfile("Dog Bite",                   0.05, 120,  14_000, 3.0, 0.30, 0.10),
    InjuryProfile("Truck/Commercial Vehicle",   0.06, 300, 110_000, 3.0, 0.60, 0.42),
]


@dataclass(frozen=True)
class CarrierProfile:
    name: str
    weight: float
    offer_mult: float         # multiplicador de espera de primera oferta
    neg_mult: float           # multiplicador de negociación
    check_mult: float         # multiplicador de emisión del cheque


CARRIERS = [
    CarrierProfile("State Farm",    0.20, 1.00, 1.00, 1.00),
    CarrierProfile("GEICO",         0.16, 1.10, 1.10, 0.90),
    CarrierProfile("Progressive",   0.14, 0.90, 0.90, 0.80),
    CarrierProfile("Allstate",      0.13, 1.25, 1.25, 1.20),
    CarrierProfile("Liberty Mutual", 0.09, 1.30, 1.30, 1.10),
    CarrierProfile("USAA",          0.06, 0.90, 0.95, 0.90),
    CarrierProfile("Farmers",       0.10, 1.15, 1.15, 1.30),   # cheques lentos
    CarrierProfile("Nationwide",    0.07, 1.00, 1.00, 1.00),
    CarrierProfile("Travelers",     0.05, 1.05, 1.10, 1.00),
]


@dataclass(frozen=True)
class ManagerProfile:
    speed: float              # <1 = más rápido; >1 = más lento (fases controlables)
    reduction_skill: float    # ajuste aditivo a % de reducción de LOPs
    weight: float


# El 5º manager es el "cuello de botella" interno (speed 1.35).
MANAGER_PROFILES = [
    ManagerProfile(0.85, +0.03, 0.20),
    ManagerProfile(0.95,  0.00, 0.22),
    ManagerProfile(1.00,  0.00, 0.20),
    ManagerProfile(1.05, -0.01, 0.18),
    ManagerProfile(1.35, -0.03, 0.12),
    ManagerProfile(1.10, +0.01, 0.08),
]


# ──────────────────────────────────────────────────────────────────────────────
# 3. UTILIDADES ALEATORIAS
# ──────────────────────────────────────────────────────────────────────────────
def lognormal_days(rng: np.random.Generator, median: float, sigma: float,
                   minimum: int = 1, maximum: int | None = None) -> int:
    """Duración en días ~ lognormal (cola larga, como en la operación real)."""
    value = int(round(rng.lognormal(mean=math.log(median), sigma=sigma)))
    value = max(minimum, value)
    return min(value, maximum) if maximum else value


def weighted_choice(rng: np.random.Generator, items: list, weights: list[float]):
    p = np.array(weights, dtype=float)
    return items[rng.choice(len(items), p=p / p.sum())]


# ──────────────────────────────────────────────────────────────────────────────
# 4. GENERACIÓN DE UN CASO "COMPLETO" (luego se enmascara si está abierto)
# ──────────────────────────────────────────────────────────────────────────────
def draw_financials(rng, inj: InjuryProfile, is_litigation: bool) -> dict:
    """Montos originales y settlement. La deuda total nunca supera el 45% del settlement."""
    bills = round(max(1_500.0, rng.lognormal(math.log(inj.bills_median), 0.45)), 2)

    has_liens = rng.random() < inj.lien_prob
    liens = round(bills * rng.uniform(0.25, 0.90), 2) if has_liens else 0.0

    debt = bills + liens
    settlement = debt * rng.lognormal(math.log(inj.settle_mult), 0.25)
    if is_litigation:
        settlement *= 1.25
    settlement = max(settlement, debt / MAX_DEBT_TO_SETTLEMENT, 5_000.0)
    settlement = min(math.ceil(settlement / 50) * 50, 1_500_000)

    return {"bills": bills, "liens": liens, "has_liens": has_liens,
            "settlement": float(settlement)}


def ceil_cents(value: float, original: float) -> float:
    """Redondea hacia ARRIBA a centavos (nunca rompe el tope) sin exceder el monto original."""
    return min(original, math.ceil(value * 100 - 1e-6) / 100)


def draw_reductions(rng, bills: float, liens: float, skill: float) -> tuple[float, float]:
    """
    Montos finales tras reducciones.
      - Liens formales: 25% de los casos sin reducción; el resto 0-25% (tope duro).
      - LOPs/Bills: ~N(35%, 7%) + skill del manager (+4% si facturas > $50k).
    Se usa ceil a centavos para que redondear nunca rompa los topes.
    """
    if liens > 0:
        pct = 0.0 if rng.random() < 0.25 else 0.25 * rng.beta(2.0, 2.2)
        pct = min(pct, LIEN_REDUCTION_CAP)
        final_liens = ceil_cents(liens * (1 - pct), liens)
    else:
        final_liens = 0.0

    pct_b = rng.normal(0.35 + skill + (0.04 if bills > 50_000 else 0.0), 0.07)
    pct_b = float(np.clip(pct_b, 0.10, 0.55))
    final_bills = ceil_cents(bills * (1 - pct_b), bills)
    return final_bills, final_liens


def draw_durations(rng, inj, carrier, mgr: ManagerProfile,
                   is_litigation: bool, has_liens: bool) -> dict[str, int]:
    """Duraciones base (días) de cada tramo. Incluye outliers realistas."""
    lit = is_litigation
    speed = mgr.speed

    treatment = lognormal_days(rng, inj.treat_median * (1.10 if lit else 1.0), 0.30, 30)

    records = lognormal_days(rng, 35, 0.50, 7)
    if rng.random() < 0.06:                                   # proveedor "perdido"
        records = int(records * rng.uniform(2.0, 3.0))

    demand = lognormal_days(rng, 14 * speed, 0.40, 3)
    offer = lognormal_days(rng, 38 * carrier.offer_mult * (1.3 if lit else 1.0), 0.40, 14)
    negotiation = lognormal_days(rng, 45 * carrier.neg_mult * (2.0 if lit else 1.0), 0.60, 5)

    rel_send = lognormal_days(rng, 2.5 * speed, 0.50, 1, 15)   # firma prepara y envía
    rel_sign = lognormal_days(rng, 7 * math.sqrt(speed), 0.55, 1)  # cliente firma/notariza/devuelve
    if rng.random() < 0.05:                                    # cliente ilocalizable
        rel_sign += int(rng.integers(15, 41))

    check = lognormal_days(rng, 14 * carrier.check_mult, 0.45, 5)
    if rng.random() < 0.05:                                    # cheque "perdido" por carrier
        check += int(rng.integers(20, 46))

    # Reducciones (paralelas): terminan cuando TODAS las reducciones están confirmadas.
    bills_track = lognormal_days(rng, 22 * math.sqrt(speed), 0.55, 7)
    lien_track = lognormal_days(rng, 38 * math.sqrt(speed), 0.50, 10) if has_liens else 0
    reductions = max(bills_track, lien_track)

    disb = lognormal_days(rng, 6 * speed, 0.45, 2)

    return {"treatment": treatment, "records": records, "demand": demand,
            "offer": offer, "negotiation": negotiation, "rel_send": rel_send,
            "rel_sign": rel_sign, "check": check, "reductions": reductions,
            "disb": disb}


def build_offsets(d: dict[str, int]) -> dict[str, int]:
    """Convierte duraciones en offsets (días desde intake) de cada hito."""
    o = {"intake": 0}
    o["treatment_completion"] = o["intake"] + d["treatment"]
    o["records_received"] = o["treatment_completion"] + d["records"]
    o["demand_sent"] = o["records_received"] + d["demand"]
    o["first_offer"] = o["demand_sent"] + d["offer"]
    o["settlement_accepted"] = o["first_offer"] + d["negotiation"]
    o["release_sent"] = o["settlement_accepted"] + d["rel_send"]
    o["release_signed"] = o["release_sent"] + d["rel_sign"]
    o["check_received"] = o["release_signed"] + d["check"]
    o["reductions_confirmed"] = o["settlement_accepted"] + d["reductions"]
    o["ready_to_disburse"] = max(o["check_received"], o["reductions_confirmed"])
    o["disbursement"] = o["ready_to_disburse"] + d["disb"]
    return o


def phase_days_from_offsets(o: dict[str, int]) -> list[int]:
    """Las 9 fases del camino crítico (mismo orden que PHASES). Suman o['disbursement']."""
    return [
        o["treatment_completion"] - o["intake"],
        o["records_received"] - o["treatment_completion"],
        o["demand_sent"] - o["records_received"],
        o["first_offer"] - o["demand_sent"],
        o["settlement_accepted"] - o["first_offer"],
        o["release_signed"] - o["settlement_accepted"],
        o["check_received"] - o["release_signed"],
        o["ready_to_disburse"] - o["check_received"],       # reductions_blocking
        o["disbursement"] - o["ready_to_disburse"],
    ]


def draw_case(rng, carriers_pool, injuries_pool, managers: list[tuple[str, ManagerProfile]]) -> dict:
    """Genera un caso completo (como si estuviera cerrado) con offsets y finanzas."""
    inj = weighted_choice(rng, INJURIES, [i.weight for i in INJURIES])
    carrier = weighted_choice(rng, CARRIERS, [c.weight for c in CARRIERS])
    mgr_name, mgr = weighted_choice(rng, managers, [m.weight for _, m in managers])

    is_lit = rng.random() < inj.litigation_prob
    fin = draw_financials(rng, inj, is_lit)
    final_bills, final_liens = draw_reductions(rng, fin["bills"], fin["liens"], mgr.reduction_skill)
    dur = draw_durations(rng, inj, carrier, mgr, is_lit, fin["has_liens"])
    offsets = build_offsets(dur)

    return {
        "injury_type": inj.name,
        "insurance_company": carrier.name,
        "case_manager": mgr_name,
        "attorney_fee_pct": FEE_LITIGATION if is_lit else FEE_PRE_SUIT,
        "settlement_amount": fin["settlement"],
        "original_medical_bills_amount": fin["bills"],
        "final_reduced_bills_amount": final_bills,
        "original_liens_amount": fin["liens"],
        "final_reduced_liens_amount": final_liens,
        "offsets": offsets,
        "phase_days": phase_days_from_offsets(offsets),
    }


# ──────────────────────────────────────────────────────────────────────────────
# 5. ENSAMBLE DEL DATASET (casos cerrados + abiertos con NULLs)
# ──────────────────────────────────────────────────────────────────────────────
OFFSET_TO_COL = {
    "intake": "intake_date",
    "treatment_completion": "treatment_completion_date",
    "records_received": "medical_records_received_date",
    "demand_sent": "demand_sent_date",
    "first_offer": "first_offer_date",
    "settlement_accepted": "settlement_accepted_date",
    "release_sent": "release_sent_to_client_date",
    "release_signed": "release_signed_and_sent_to_carrier_date",
    "check_received": "settlement_check_received_date",
    "reductions_confirmed": "reductions_confirmed_date",
    "disbursement": "closing_statement_disbursement_date",
}


def materialize(case: dict, intake: date, is_open: bool) -> dict:
    """Convierte offsets a fechas reales y aplica el enmascarado de NULLs si está abierto."""
    row = {
        "injury_type": case["injury_type"],
        "insurance_company": case["insurance_company"],
        "case_manager": case["case_manager"],
        "attorney_fee_pct": case["attorney_fee_pct"],
    }
    for off_key, col in OFFSET_TO_COL.items():
        d = intake + timedelta(days=case["offsets"][off_key])
        row[col] = d if d <= CUTOFF_DATE else None       # hito futuro -> NULL

    # Montos: solo existen si el hito que los revela ya ocurrió.
    reached = lambda col: row[col] is not None
    has_bills = reached("medical_records_received_date")
    has_settle = reached("settlement_accepted_date")
    has_final = reached("reductions_confirmed_date")

    row["original_medical_bills_amount"] = case["original_medical_bills_amount"] if has_bills else None
    row["original_liens_amount"] = case["original_liens_amount"] if has_bills else None
    row["settlement_amount"] = case["settlement_amount"] if has_settle else None
    row["final_reduced_bills_amount"] = case["final_reduced_bills_amount"] if has_final else None
    row["final_reduced_liens_amount"] = case["final_reduced_liens_amount"] if has_final else None

    if not is_open:
        assert all(row[c] is not None for c in DATE_COLS), "Caso cerrado con NULL inesperado"
    return row


def generate_dataset(seed: int = SEED) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    Faker.seed(seed)
    fake = Faker("en_US")

    managers = [(fake.name(), prof) for prof in MANAGER_PROFILES]
    rows: list[dict] = []

    # ── Casos CERRADOS: intake aleatorio tal que el desembolso ocurra <= corte
    while len(rows) < N_CLOSED:
        case = draw_case(rng, None, None, managers)
        total_days = case["offsets"]["disbursement"]
        latest_intake = CUTOFF_DATE - timedelta(days=total_days)
        span = (latest_intake - EARLIEST_INTAKE).days
        if span < 0:
            continue                                     # ciclo demasiado largo, re-sortear
        intake = EARLIEST_INTAKE + timedelta(days=int(rng.integers(0, span + 1)))
        rows.append(materialize(case, intake, is_open=False))

    # ── Casos ABIERTOS: el corte cae DENTRO de la fase planeada (fase actual conocida).
    #    MUESTREO SESGADO POR DURACIÓN (paradoja de la inspección): en una foto del
    #    inventario, la probabilidad de encontrar un caso en una fase es proporcional a
    #    cuánto dura esa fase en ese caso. Los casos con una fase larga (expediente perdido,
    #    cheque extraviado, cliente ilocalizable) están sobrerrepresentados, tal como en una
    #    cartera real. Se logra eligiendo entre N candidatos con peso = duración en la fase.
    #    (Los casos CERRADOS no cambian: se generan antes con la misma secuencia aleatoria.)
    stage_list = [i for i, n in enumerate(OPEN_STAGE_PLAN) for _ in range(n)]
    rng.shuffle(stage_list)
    for stage_idx in stage_list:
        while True:
            candidates = [draw_case(rng, None, None, managers) for _ in range(N_OPEN_CANDIDATES)]
            weights = np.array([c["phase_days"][stage_idx] for c in candidates], dtype=float)
            if weights.sum() > 0:                        # al menos un candidato con fase >= 1 día
                break
        case = candidates[int(rng.choice(len(candidates), p=weights / weights.sum()))]
        bounds = np.cumsum([0] + case["phase_days"])
        start, end = int(bounds[stage_idx]), int(bounds[stage_idx + 1])
        offset_today = int(rng.integers(start, end))     # día de hoy dentro de la fase
        intake = CUTOFF_DATE - timedelta(days=offset_today)
        rows.append(materialize(case, intake, is_open=True))

    df = pd.DataFrame(rows)
    df = df.sort_values("intake_date", kind="stable").reset_index(drop=True)

    # case_id estilo PI-2024-0007 (secuencia por año de intake)
    years = pd.to_datetime(df["intake_date"]).dt.year
    seq = df.groupby(years).cumcount() + 1
    df["case_id"] = [f"PI-{y}-{s:04d}" for y, s in zip(years, seq)]

    return df[ALL_COLS]


# ──────────────────────────────────────────────────────────────────────────────
# 6. VALIDACIONES ESTRICTAS
# ──────────────────────────────────────────────────────────────────────────────
def compute_phase_days(df: pd.DataFrame) -> pd.DataFrame:
    """Calcula las 9 fases (días) para casos CERRADOS a partir de las fechas."""
    d = {c: pd.to_datetime(df[c]) for c in DATE_COLS}
    ready = pd.concat([d["settlement_check_received_date"],
                       d["reductions_confirmed_date"]], axis=1).max(axis=1)
    out = pd.DataFrame({
        "treatment": (d["treatment_completion_date"] - d["intake_date"]).dt.days,
        "records_wait": (d["medical_records_received_date"] - d["treatment_completion_date"]).dt.days,
        "demand_prep": (d["demand_sent_date"] - d["medical_records_received_date"]).dt.days,
        "offer_wait": (d["first_offer_date"] - d["demand_sent_date"]).dt.days,
        "negotiation": (d["settlement_accepted_date"] - d["first_offer_date"]).dt.days,
        "release_execution": (d["release_signed_and_sent_to_carrier_date"] - d["settlement_accepted_date"]).dt.days,
        "carrier_check_wait": (d["settlement_check_received_date"] - d["release_signed_and_sent_to_carrier_date"]).dt.days,
        "reductions_blocking": (ready - d["settlement_check_received_date"]).dt.days,
        "disbursement_prep": (d["closing_statement_disbursement_date"] - ready).dt.days,
    })
    out["reductions_parallel_wait"] = (d["reductions_confirmed_date"] - d["settlement_accepted_date"]).dt.days
    out["total_cycle_days"] = (d["closing_statement_disbursement_date"] - d["intake_date"]).dt.days
    return out


def infer_current_stage(row: pd.Series) -> str:
    """Fase actual de un caso abierto, deducida únicamente del patrón de NULLs."""
    if pd.isna(row["treatment_completion_date"]):
        return "treatment"
    if pd.isna(row["medical_records_received_date"]):
        return "records_wait"
    if pd.isna(row["demand_sent_date"]):
        return "demand_prep"
    if pd.isna(row["first_offer_date"]):
        return "offer_wait"
    if pd.isna(row["settlement_accepted_date"]):
        return "negotiation"
    if pd.isna(row["release_signed_and_sent_to_carrier_date"]):
        return "release_execution"
    if pd.isna(row["settlement_check_received_date"]):
        return "carrier_check_wait"
    if pd.isna(row["reductions_confirmed_date"]):
        return "reductions_blocking"
    return "disbursement_prep"


def validate(df: pd.DataFrame) -> None:
    errors: list[str] = []

    def require(ok_mask: pd.Series, msg: str) -> None:
        bad = df.loc[~ok_mask, "case_id"].tolist()
        if bad:
            errors.append(f"{msg} -> {len(bad)} casos, ej: {bad[:5]}")

    # ── Estructura
    if list(df.columns) != ALL_COLS:
        errors.append("Columnas u orden no coinciden con el diccionario.")
    if len(df) != N_TOTAL:
        errors.append(f"Se esperaban {N_TOTAL} filas, hay {len(df)}.")
    if df["case_id"].duplicated().any():
        errors.append("case_id duplicados.")

    dt = {c: pd.to_datetime(df[c]) for c in DATE_COLS}
    closed = dt["closing_statement_disbursement_date"].notna()
    n_closed, n_open = int(closed.sum()), int((~closed).sum())
    if (n_closed, n_open) != (N_CLOSED, N_OPEN):
        errors.append(f"Cerrados/abiertos = {n_closed}/{n_open}, esperado {N_CLOSED}/{N_OPEN}.")

    # ── Ninguna fecha posterior al corte
    cutoff = pd.Timestamp(CUTOFF_DATE)
    for c in DATE_COLS:
        require(dt[c].isna() | (dt[c] <= cutoff), f"{c} posterior al corte {CUTOFF_DATE}")

    # ── Orden cronológico estricto de la cadena principal + cascada de NULLs
    chain = ["intake_date", "treatment_completion_date", "medical_records_received_date",
             "demand_sent_date", "first_offer_date", "settlement_accepted_date",
             "release_sent_to_client_date", "release_signed_and_sent_to_carrier_date",
             "settlement_check_received_date"]
    require(dt[chain[0]].notna(), "intake_date NULL")
    for a, b in zip(chain, chain[1:]):
        both = dt[a].notna() & dt[b].notna()
        require(~both | (dt[a] < dt[b]), f"Orden cronológico violado: {a} >= {b}")
        require(dt[a].notna() | dt[b].isna(), f"NULL en cascada violado: {a} es NULL pero {b} no")

    acc, red = dt["settlement_accepted_date"], dt["reductions_confirmed_date"]
    chk, dsb = dt["settlement_check_received_date"], dt["closing_statement_disbursement_date"]
    require(red.isna() | (acc.notna() & (red > acc)), "reductions_confirmed debe ser > settlement_accepted")
    require(dsb.isna() | (chk.notna() & red.notna()), "Desembolso sin cheque o sin reducciones confirmadas")
    ready = pd.concat([chk, red], axis=1).max(axis=1)
    require(dsb.isna() | (dsb > ready), "Desembolso debe ser > max(cheque, reducciones)")

    # ── Consistencia NULL de montos con los hitos
    nn = lambda c: df[c].notna()
    require(nn("settlement_amount") == acc.notna(), "settlement_amount NULL no calza con settlement_accepted_date")
    for c in ["original_medical_bills_amount", "original_liens_amount"]:
        require(nn(c) == dt["medical_records_received_date"].notna(), f"{c} NULL no calza con records_received")
    for c in ["final_reduced_bills_amount", "final_reduced_liens_amount"]:
        require(nn(c) == red.notna(), f"{c} NULL no calza con reductions_confirmed_date")
    require(df["attorney_fee_pct"].isin([FEE_PRE_SUIT, FEE_LITIGATION]), "attorney_fee_pct fuera de {0.33, 0.40}")

    # ── Reglas de reducción (solo donde ya hay monto final)
    f = df[df["final_reduced_bills_amount"].notna()]
    f_idx = f.index
    lien_ok = f["final_reduced_liens_amount"] <= f["original_liens_amount"] + 1e-9
    lien_cap = f["final_reduced_liens_amount"] >= f["original_liens_amount"] * (1 - LIEN_REDUCTION_CAP) - 1e-6
    bill_ok = f["final_reduced_bills_amount"] <= f["original_medical_bills_amount"] + 1e-9
    bill_cap = f["final_reduced_bills_amount"] >= f["original_medical_bills_amount"] * (1 - BILLS_REDUCTION_CAP) - 1e-6
    for mask, msg in [(lien_ok, "final_liens > original_liens"),
                      (lien_cap, f"Reducción de liens > {LIEN_REDUCTION_CAP:.0%}"),
                      (bill_ok, "final_bills > original_bills"),
                      (bill_cap, f"Reducción de bills > {BILLS_REDUCTION_CAP:.0%}")]:
        bad = f.loc[~mask, "case_id"].tolist()
        if bad:
            errors.append(f"{msg} -> {len(bad)} casos, ej: {bad[:5]}")
    zero_liens = f[f["original_liens_amount"] == 0]
    if (zero_liens["final_reduced_liens_amount"] != 0).any():
        errors.append("Casos sin liens con final_reduced_liens_amount != 0")

    # ── Economía del caso cerrado: el cliente debe recibir neto positivo
    c = df[closed]
    net = (c["settlement_amount"] * (1 - c["attorney_fee_pct"])
           - c["final_reduced_bills_amount"] - c["final_reduced_liens_amount"])
    if (net <= 0).any():
        errors.append(f"Net to client <= 0 en {(net <= 0).sum()} casos cerrados.")
    debt_ratio = (c["original_medical_bills_amount"] + c["original_liens_amount"]) / c["settlement_amount"]
    if (debt_ratio > MAX_DEBT_TO_SETTLEMENT + 1e-9).any():
        errors.append("Deuda original > 45% del settlement en algún caso.")

    # ── Cuadre matemático de las 9 fases vs ciclo total (solo cerrados)
    ph = compute_phase_days(c)
    phase_cols = PHASE_KEYS
    if (ph[phase_cols].sum(axis=1) != ph["total_cycle_days"]).any():
        errors.append("La suma de las 9 fases NO calza con el ciclo total.")
    non_neg = [k for k in phase_cols if k != "reductions_blocking"]
    if (ph[non_neg] < 1).any().any():
        errors.append("Alguna fase (no paralela) tiene duración < 1 día.")
    if (ph["reductions_blocking"] < 0).any():
        errors.append("reductions_blocking negativo.")
    if (ph["reductions_blocking"] > ph["reductions_parallel_wait"]).any():
        errors.append("reductions_blocking > espera paralela total (imposible).")

    # ── Fase actual de los abiertos coincide con el plan
    o = df[~closed]
    actual = o.apply(infer_current_stage, axis=1).value_counts().to_dict()
    expected = dict(zip(PHASE_KEYS, OPEN_STAGE_PLAN))
    for k, n in expected.items():
        if actual.get(k, 0) != n:
            errors.append(f"Casos abiertos en '{k}': {actual.get(k, 0)} vs plan {n}.")

    # ── Regla de negocio: LOPs se negocian más que liens formales
    lien_pct = (1 - f.loc[f["original_liens_amount"] > 0, "final_reduced_liens_amount"]
                / f.loc[f["original_liens_amount"] > 0, "original_liens_amount"])
    bill_pct = 1 - f["final_reduced_bills_amount"] / f["original_medical_bills_amount"]
    if not bill_pct.mean() > lien_pct.mean():
        errors.append("El % de reducción promedio de LOPs no supera al de liens formales.")

    if errors:
        raise ValueError("VALIDACIÓN FALLIDA:\n  - " + "\n  - ".join(errors))


# ──────────────────────────────────────────────────────────────────────────────
# 7. REPORTE DE CALIDAD (sanity check legible)
# ──────────────────────────────────────────────────────────────────────────────
def print_report(df: pd.DataFrame) -> None:
    closed = df["closing_statement_disbursement_date"].notna()
    c, o = df[closed], df[~closed]
    ph = compute_phase_days(c)
    type_of = {k: t for k, _, t in PHASES}
    name_of = {k: n for k, n, _ in PHASES}

    print("\n" + "=" * 72)
    print(f"DATASET OK  |  {len(df)} casos  |  {len(c)} cerrados  |  {len(o)} abiertos  |  corte {CUTOFF_DATE}")
    print("=" * 72)

    print("\n[1] Ciclo y fases (casos cerrados, días)")
    print(f"    {'Fase':<24}{'Tipo':<14}{'Media':>8}{'Mediana':>9}{'P90':>7}")
    for k in PHASE_KEYS:
        s = ph[k]
        print(f"    {name_of[k]:<24}{type_of[k]:<14}{s.mean():>8.1f}{s.median():>9.0f}{s.quantile(.9):>7.0f}")
    print(f"    {'TOTAL CICLO':<38}{ph['total_cycle_days'].mean():>8.1f}"
          f"{ph['total_cycle_days'].median():>9.0f}{ph['total_cycle_days'].quantile(.9):>7.0f}")

    by_type = {t: sum(ph[k].mean() for k in PHASE_KEYS if type_of[k] == t)
               for t in ["Clinical", "Strategic", "External", "Controllable"]}
    print("\n[2] Reparto del ciclo medio por tipo de fase")
    for t, v in by_type.items():
        print(f"    {t:<13}{v:>7.1f} días  ({v / ph['total_cycle_days'].mean():.0%})")

    blocked = (ph["reductions_blocking"] > 0)
    print("\n[3] Camino crítico post-acuerdo")
    print(f"    Casos donde Reducciones bloqueó el desembolso: {blocked.sum()}/{len(ph)} ({blocked.mean():.0%})")
    print(f"    Días de bloqueo promedio (solo bloqueados):    {ph.loc[blocked, 'reductions_blocking'].mean():.1f}")

    f = df[df["final_reduced_bills_amount"].notna()]
    fl = f[f["original_liens_amount"] > 0]
    lien_save = 1 - fl["final_reduced_liens_amount"].sum() / fl["original_liens_amount"].sum()
    bill_save = 1 - f["final_reduced_bills_amount"].sum() / f["original_medical_bills_amount"].sum()
    print("\n[4] Ahorro por reducciones (ponderado por $)")
    print(f"    Liens formales : {lien_save:.1%}  (tope duro {LIEN_REDUCTION_CAP:.0%}; "
          f"{(fl['original_liens_amount'] > 0).sum()} casos con liens)")
    print(f"    Bills / LOPs   : {bill_save:.1%}")

    print("\n[5] Casos abiertos por fase actual (inferida desde NULLs)")
    stages = o.apply(infer_current_stage, axis=1).value_counts()
    for k in PHASE_KEYS:
        print(f"    {name_of[k]:<24}{stages.get(k, 0):>3}")

    print("\n[6] Mix de cartera")
    print(f"    Litigio (fee 40%): {(df['attorney_fee_pct'] == FEE_LITIGATION).mean():.0%}")
    print(f"    Settlement mediano (casos con acuerdo): ${df['settlement_amount'].median():,.0f}")
    print(f"    NULLs por columna (abiertos): "
          f"{ {k: int(v) for k, v in o.isna().sum().items() if v} }")


# ──────────────────────────────────────────────────────────────────────────────
# 8. MAIN
# ──────────────────────────────────────────────────────────────────────────────
def main() -> None:
    df = generate_dataset(SEED)
    validate(df)                                     # lanza ValueError si algo falla

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    for col in DATE_COLS:
        out[col] = pd.to_datetime(out[col]).dt.strftime("%Y-%m-%d")
    out.to_csv(OUTPUT_PATH, index=False, na_rep="", float_format="%.2f")

    print_report(df)
    print(f"\nArchivo guardado en: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()