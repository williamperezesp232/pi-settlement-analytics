"""
01_generate_dataset.py
Synthetic dataset generator for "Personal Injury Settlement & Bottleneck Analytics".

Produces data/pi_cases.csv (200 cases) with realistic PI case-lifecycle behavior:
  - Policy-limit clustering on settlements (15/25, 25/50, 50/100, 100/300 ... BI limits)
  - Pre-suit (33%) vs. litigated (40%) contingency fee tiers
  - Liens (health insurer / Medicare-Medicaid / hospital / MedPay / provider LOP) with
    negotiated reductions; some liens are non-negotiable (Medicare / self-funded ERISA)
  - External waits (medical records, adjuster response) vs. internal controllable steps
  - Right-censoring: cases still open as of AS_OF_DATE have NULL future milestones

All names, amounts and carrier timing differences are FICTIONAL.
Run:  python python/01_generate_dataset.py
"""
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

SEED = 61
N_CASES = 200
AS_OF_DATE = date(2026, 10, 13)                       # "today" for open-case aging
INTAKE_START, INTAKE_END = date(2024, 3, 1), date(2026, 3, 15)
OUT = Path(__file__).resolve().parents[1] / "data" / "pi_cases.csv"

rng = np.random.default_rng(SEED)

FIRST = ["James", "Maria", "Robert", "Linda", "Michael", "Patricia", "David", "Jennifer", "Carlos",
         "Sofia", "Daniel", "Ashley", "Jose", "Brittany", "Anthony", "Keisha", "Brian", "Monica",
         "Kevin", "Tiffany", "Luis", "Rachel", "Marcus", "Angela", "Eric", "Nicole", "Andre",
         "Stephanie", "Victor", "Lauren"]
LAST = ["Johnson", "Garcia", "Williams", "Martinez", "Brown", "Rodriguez", "Davis", "Hernandez",
        "Miller", "Lopez", "Wilson", "Gonzalez", "Moore", "Perez", "Taylor", "Sanchez", "Thomas",
        "Ramirez", "Jackson", "Torres", "White", "Flores", "Harris", "Rivera", "Clark", "Nguyen",
        "Lewis", "Diaz", "Walker", "Reyes"]

INJURY_TYPES = ["Motor Vehicle Accident", "Slip & Fall", "Dog Bite", "Pedestrian"]
INJURY_P = [0.55, 0.20, 0.08, 0.17]

MANAGERS = {
    "Jennifer Ortiz":   dict(w=0.24, prep=1.00, close=1.00, lien=0.00),
    "Marcus Bell":      dict(w=0.20, prep=1.40, close=1.45, lien=-0.05),   # slowest internally
    "Priya Nair":       dict(w=0.20, prep=0.80, close=0.85, lien=0.05),    # fastest / best lien results
    "Daniel Kowalski":  dict(w=0.21, prep=1.10, close=1.15, lien=0.00),
    "Alicia Reyes":     dict(w=0.15, prep=0.90, close=0.90, lien=0.03),
}

CARRIER_MEDIAN_WAIT = {"State Farm": 30, "GEICO": 24, "Progressive": 28, "Allstate": 35, "Liberty Mutual": 41}

CARRIER_MIX = {
    "Motor Vehicle Accident": {"State Farm": .26, "GEICO": .24, "Progressive": .20, "Allstate": .18, "Liberty Mutual": .12},
    "Pedestrian":             {"State Farm": .26, "GEICO": .24, "Progressive": .20, "Allstate": .18, "Liberty Mutual": .12},
    "Slip & Fall":            {"State Farm": .20, "Progressive": .10, "Allstate": .30, "Liberty Mutual": .40},
    "Dog Bite":               {"State Farm": .40, "Allstate": .30, "Liberty Mutual": .30},
}

LIMITS = {
    "Motor Vehicle Accident": ([15_000, 25_000, 50_000, 100_000, 250_000, 500_000], [.10, .30, .25, .20, .10, .05]),
    "Pedestrian":             ([15_000, 25_000, 50_000, 100_000, 250_000, 500_000], [.10, .30, .25, .20, .10, .05]),
    "Slip & Fall":            ([100_000, 300_000, 500_000, 1_000_000], [.20, .40, .30, .10]),
    "Dog Bite":               ([100_000, 300_000, 500_000], [.40, .40, .20]),
}
BILLS_MEDIAN_SIGMA = { 
    "Motor Vehicle Accident": (22_000, 0.75), "Pedestrian": (48_000, 0.70),
    "Slip & Fall": (28_000, 0.80), "Dog Bite": (9_000, 0.70),
}
TREATMENT_MEAN_DAYS = {"Motor Vehicle Accident": 150, "Pedestrian": 200, "Slip & Fall": 120, "Dog Bite": 70}
LITIGATION_P = {"Motor Vehicle Accident": 0.15, "Pedestrian": 0.20, "Slip & Fall": 0.30, "Dog Bite": 0.22}


def lognorm(median: float, sigma: float) -> float:
    return float(rng.lognormal(np.log(median), sigma))


def make_names(n: int) -> list[str]:
    names: set[str] = set()
    while len(names) < n:
        names.add(f"{rng.choice(FIRST)} {rng.choice(LAST)}")
    return sorted(names, key=lambda _: rng.random())



def simulate_case() -> dict:
    inj = str(rng.choice(INJURY_TYPES, p=INJURY_P))
    mgr_names = list(MANAGERS)
    mgr = str(rng.choice(mgr_names, p=[MANAGERS[m]["w"] for m in mgr_names]))
    m = MANAGERS[mgr]
    mix = CARRIER_MIX[inj]
    carrier = str(rng.choice(list(mix), p=list(mix.values())))
    litigated = rng.random() < LITIGATION_P[inj]

    intake = INTAKE_START + timedelta(days=int(rng.integers(0, (INTAKE_END - INTAKE_START).days + 1)))

   
    med, sig = BILLS_MEDIAN_SIGMA[inj]
    bills = round(float(np.clip(lognorm(med, sig), 2_500, 350_000)), 2)

    limit_vals, limit_p = LIMITS[inj]
    limit = int(rng.choice(limit_vals, p=limit_p))
    target = bills * float(np.clip(lognorm(1.9, 0.35), 0.9, 5.0))
    if target >= limit * 0.95:                        
        settlement = float(limit) if rng.random() < 0.85 else round(limit * rng.uniform(0.80, 0.98) / 250) * 250
    else:
        settlement = max(3_000.0, round(target / 250) * 250)
    settlement = float(min(settlement, limit))

    fee_pct = 0.40 if litigated else 0.33

    
    if rng.random() < 0.08:
        orig_liens = final_liens = 0.0
    else:
        non_negotiable = rng.random() < 0.15           # Medicare / self-funded ERISA: little room
        orig_liens = bills * float(np.clip(lognorm(0.30, 0.5), 0.05, 0.85))
        orig_liens = min(orig_liens, (0.30 if non_negotiable else 0.80) * settlement)
        orig_liens = round(max(orig_liens, 250.0), 2)
        if non_negotiable:
            r = float(rng.uniform(0.0, 0.10))
        else:
            r = float(np.clip(rng.beta(4, 6.5) + m["lien"], 0.05, 0.75))
        r = max(r, 1 - 0.35 * settlement / orig_liens)  # keep client net recovery meaningful
        r = min(r, 0.90)
        final_liens = round(orig_liens * (1 - r), 2)

  
    treat_days = int(max(21, rng.gamma(4, TREATMENT_MEAN_DAYS[inj] / 4)))
    rec_wait = lognorm(22, 0.55)
    if rng.random() < 0.15:                            # problem provider / HIM backlog / subpoena needed
        rec_wait += rng.uniform(30, 80)
    rec_wait = int(np.clip(rec_wait, 5, 150))
    prep = int(np.clip(lognorm(14, 0.5) * m["prep"] * (1 + bills / 250_000), 4, 70))
    adj_wait = lognorm(CARRIER_MEDIAN_WAIT[carrier], 0.45)
    if rng.random() < 0.12:                            # adjuster reassignment / supervisor review / "slow-walk"
        adj_wait += rng.uniform(25, 60)
    adj_wait = int(np.clip(adj_wait, 10, 150))
    if litigated:
        neg = int(np.clip(120 + rng.gamma(3, 60), 120, 420))
    else:
        neg = int(np.clip(lognorm(28, 0.6), 5, 120))
    closing = int(np.clip(lognorm(30, 0.35) * m["close"] + orig_liens / 2_500, 14, 150))

    d_treat = intake + timedelta(days=treat_days)
    d_rec = d_treat + timedelta(days=rec_wait)
    d_dem = d_rec + timedelta(days=prep)
    d_off = d_dem + timedelta(days=adj_wait)
    d_acc = d_off + timedelta(days=neg)
    d_clo = d_acc + timedelta(days=closing)

    
    def cut(d: date):
        return d if d <= AS_OF_DATE else None

    d_treat, d_rec, d_dem, d_off, d_acc, d_clo = map(cut, (d_treat, d_rec, d_dem, d_off, d_acc, d_clo))

    return dict(
        injury_type=inj, case_manager=mgr, insurance_company=carrier,
        total_medical_bills=bills if d_rec else None,
        settlement_amount=settlement if d_acc else None,
        attorney_fee_pct=fee_pct,
        original_liens_amount=orig_liens if d_rec else None,
        final_reduced_liens_amount=final_liens if d_clo else None,
        intake_date=intake, treatment_completion_date=d_treat,
        medical_records_received_date=d_rec, demand_sent_date=d_dem,
        first_offer_date=d_off, settlement_accepted_date=d_acc, closing_statement_date=d_clo,
    )


def main() -> None:
    df = pd.DataFrame([simulate_case() for _ in range(N_CASES)])
    df = df.sort_values("intake_date").reset_index(drop=True)
    df["seq"] = df.groupby(pd.to_datetime(df["intake_date"]).dt.year).cumcount() + 1
    df["case_id"] = [f"PI-{pd.Timestamp(d).year}-{s:03d}" for d, s in zip(df["intake_date"], df["seq"])]
    df["client_name"] = make_names(N_CASES)

    cols = ["case_id", "client_name", "injury_type", "case_manager", "insurance_company",
            "total_medical_bills", "settlement_amount", "attorney_fee_pct",
            "original_liens_amount", "final_reduced_liens_amount",
            "intake_date", "treatment_completion_date", "medical_records_received_date",
            "demand_sent_date", "first_offer_date", "settlement_accepted_date", "closing_statement_date"]
    df = df[cols]

    
    assert df["case_id"].is_unique and len(df) == N_CASES
    date_cols = cols[10:]
    d = df[date_cols].apply(pd.to_datetime)
    for a, b in zip(date_cols[:-1], date_cols[1:]):
        bad = (d[a] > d[b]).sum()
        assert bad == 0, f"date order violated {a} > {b}"
    assert (df["final_reduced_liens_amount"].fillna(0) <= df["original_liens_amount"].fillna(1e12)).all()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT, index=False)
    print(f"Wrote {len(df)} rows -> {OUT}")
    print("Open cases:", int(df["closing_statement_date"].isna().sum()))


if __name__ == "__main__":
    main()
