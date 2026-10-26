#!/usr/bin/env python3
"""
02_analysis.py
==============
Proyecto: Legal Operations & Analytics - Personal Injury (Plaintiff-side, USA)

Lee las vistas de PostgreSQL (02_metrics_view.sql y 03_kpi_views.sql), VERIFICA que la
base esté íntegra y genera los gráficos estáticos del README / portafolio.

Salida:
    images/01_cycle_breakdown.png
    images/02_manager_scorecard.png
    images/03_carrier_scorecard.png
    images/04_reductions_blocking.png
    images/05_reduction_savings.png
    images/06_open_case_alerts.png
    outputs/key_findings.md          (hallazgos con cifras calculadas desde la BD)

Requisitos:
    pip install pandas numpy matplotlib "seaborn>=0.13" sqlalchemy pg8000

Driver: por defecto **pg8000** (Python puro: sin .pyd/.dll, no lo bloquea el Control de
Aplicaciones de Windows). Para usar otro: PI_DB_DRIVER=psycopg | psycopg2 (ver db_conn.py).

Conexión (variables de entorno; todas opcionales; la lógica vive en db_conn.py):
    DATABASE_URL  = postgresql://usuario:clave@host:5432/pi_analytics
    ó bien PGHOST (localhost), PGPORT (5432), PGUSER (postgres), PGPASSWORD (""),
           PGDATABASE (pi_analytics)

Uso (desde la raíz del proyecto):
    python python/02_analysis.py

Nota: los textos de los gráficos están en inglés (audiencia: firmas de PI en EE.UU.).
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import matplotlib

matplotlib.use("Agg")                       # sin ventana: solo guarda PNG
import matplotlib.pyplot as plt
import matplotlib.ticker as mtick
import numpy as np
import pandas as pd
import seaborn as sns
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from db_conn import DriverError, get_engine              # conexión compartida (driver sin binarios)

# ──────────────────────────────────────────────────────────────────────────────
# 1. CONFIGURACIÓN
# ──────────────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
FIG_DIR = ROOT / "images"
OUT_DIR = ROOT / "outputs"
DPI = 200

TYPE_COLORS = {"Clinical": "#7f8fa6", "External": "#e1883b",
               "Strategic": "#8e6bbf", "Controllable": "#2a9d8f"}
FLAG_COLORS = {"SLOWER than firm": "#d1495b", "FASTER than firm": "#2a9d8f", "In line": "#9aa5b1"}
ALERT_COLORS = {"RED": "#d1495b", "AMBER": "#f4a261", "WATCH": "#e9c46a", "ON TRACK": "#b8c4cc"}
GROUP_COLORS = {"With formal liens": "#c0392b", "LOPs only (no formal liens)": "#2a9d8f"}
BILLS_COLOR, LIENS_COLOR = "#2a9d8f", "#c0392b"

PHASE_ORDER = ["treatment", "records_wait", "demand_prep", "offer_wait", "negotiation",
               "release_execution", "carrier_check_wait", "reductions_blocking", "disbursement_prep"]


# ──────────────────────────────────────────────────────────────────────────────
# 2. CONEXIÓN Y CARGA
# ──────────────────────────────────────────────────────────────────────────────
def query(engine: Engine, sql: str) -> pd.DataFrame:
    """Ejecuta SQL; coerce_float convierte NUMERIC (Decimal) a float."""
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn, coerce_float=True)


def get_param(engine: Engine, key: str, default: float) -> float:
    df = query(engine, f"SELECT value_num FROM pi.report_params WHERE param_key = '{key}'")
    return float(df["value_num"].iloc[0]) if len(df) and pd.notna(df["value_num"].iloc[0]) else default


def check_database(engine: Engine) -> pd.Timestamp:
    """Falla rápido si la base no está lista o la reconciliación no es perfecta."""
    rec = query(engine, "SELECT * FROM pi.v_reconciliation_check").iloc[0]
    problems = [c for c in ("cases_with_diff", "cases_with_bad_phase",
                            "cases_blocking_gt_parallel", "cases_with_class_diff") if rec[c] != 0]
    if rec["closed_cases"] == 0 or problems:
        raise RuntimeError(f"Reconciliación fallida o sin datos: {rec.to_dict()}")

    n_open = int(query(engine, "SELECT count(*) AS n FROM pi.v_case_phases WHERE NOT is_closed")["n"][0])
    n_alerts = int(query(engine, "SELECT count(*) AS n FROM pi.v_open_case_alerts")["n"][0])
    if n_open != n_alerts:
        raise RuntimeError(f"v_open_case_alerts ({n_alerts}) no coincide con casos abiertos ({n_open})")

    cutoff = query(engine, "SELECT value_date FROM pi.report_params WHERE param_key = 'report_cutoff_date'")
    print(f"BD OK  |  {int(rec['closed_cases'])} cerrados  |  {n_open} abiertos  |  "
          f"reconciliación = {int(rec['total_abs_diff_days'])} días de diferencia")
    return pd.Timestamp(cutoff["value_date"].iloc[0])


# ──────────────────────────────────────────────────────────────────────────────
# 3. ESTILO Y UTILIDADES DE GRÁFICO
# ──────────────────────────────────────────────────────────────────────────────
def set_style() -> None:
    sns.set_theme(style="whitegrid", context="talk", font_scale=0.72)
    plt.rcParams.update({
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.edgecolor": "#444", "grid.color": "#e6e6e6",
        "axes.titleweight": "bold", "axes.titlesize": 12.5,
        "figure.facecolor": "white", "savefig.facecolor": "white",
        "text.parse_math": False,            # los "$" de los montos NO son fórmulas LaTeX
    })


def usd(x: float, _=None) -> str:
    if abs(x) >= 1e6:
        return f"${x / 1e6:.1f}M"
    if abs(x) >= 1e3:
        return f"${x / 1e3:.0f}K"
    return f"${x:,.0f}"


def headline(fig, title: str, subtitle: str, cutoff: pd.Timestamp) -> None:
    """Título con el insight + subtítulo + pie con la fuente."""
    fig.suptitle(title, x=0.012, y=0.985, ha="left", fontsize=16, fontweight="bold")
    fig.text(0.012, 0.985 - 0.42 / fig.get_figheight(), subtitle, ha="left", fontsize=11, color="#555")
    fig.text(0.012, 0.012, f"Synthetic dataset · 200 personal-injury cases · cutoff {cutoff:%Y-%m-%d}",
             ha="left", fontsize=8.5, color="#888")


def save(fig, name: str) -> Path:
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    path = FIG_DIR / name
    fig.savefig(path, dpi=DPI, bbox_inches="tight")
    plt.close(fig)
    print(f"   ✔ {path.relative_to(ROOT)}")
    return path


def pct(x: float, d: int = 0) -> str:
    return f"{x:.{d}%}"


# ──────────────────────────────────────────────────────────────────────────────
# 4. GRÁFICOS
# ──────────────────────────────────────────────────────────────────────────────
def fig_cycle_breakdown(phase: pd.DataFrame, ptype: pd.DataFrame, cutoff) -> Path:
    """¿Dónde se va el tiempo? 9 fases + reparto por tipo."""
    ctrl = ptype.set_index("phase_type").loc["Controllable"]
    ext = ptype.set_index("phase_type").loc["External"]
    total = phase["avg_days"].sum()

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14.5, 6.6), gridspec_kw={"width_ratios": [1.9, 1]})

    d = phase.sort_values("phase_order", ascending=False).reset_index(drop=True)
    ax1.barh(d["phase_name"], d["avg_days"], color=d["phase_type"].map(TYPE_COLORS), height=0.68)
    ax1.scatter(d["median_days"], d["phase_name"], marker="D", s=40, color="black", zorder=3)
    for i, r in d.iterrows():
        ax1.text(r["avg_days"] + 5, i, f"{r['avg_days']:.0f} d  ·  {r['pct_of_total_cycle']:.0%}",
                 va="center", fontsize=10)
    ax1.set_xlim(0, d["avg_days"].max() * 1.22)
    ax1.grid(axis="y", visible=False)
    ax1.set_xlabel("Average days (closed cases)")
    ax1.set_title("The 9 phases of the settlement-to-closing cycle", loc="left")
    ax1.legend(handles=[Patch(color=c, label=t) for t, c in TYPE_COLORS.items()]
                       + [Line2D([], [], marker="D", color="black", ls="", label="Median")],
               loc="lower right", frameon=True, fontsize=9.5)

    t = ptype.sort_values("pct_of_total_cycle", ascending=True)
    ax2.barh(t["phase_type"], t["pct_of_total_cycle"], color=t["phase_type"].map(TYPE_COLORS), height=0.6)
    for i, (v, days) in enumerate(zip(t["pct_of_total_cycle"], t["avg_days"])):
        ax2.text(v + 0.012, i, f"{v:.0%}  ({days:.0f} d)", va="center", fontsize=10.5, fontweight="bold")
    ax2.set_xlim(0, t["pct_of_total_cycle"].max() * 1.55)
    ax2.xaxis.set_major_formatter(mtick.PercentFormatter(1.0, decimals=0))
    ax2.set_title("Share of the cycle by phase type", loc="left")
    ax2.grid(axis="y", visible=False)

    headline(fig, f"Only {pct(ctrl['pct_of_total_cycle'])} of the case cycle is in the firm's direct control",
             f"Average cycle {total:.0f} days. Treatment drives {pct(phase.set_index('phase_key').loc['treatment', 'pct_of_total_cycle'])}; "
             f"external waits (records, carrier, check, reductions) add {pct(ext['pct_of_total_cycle'])}.", cutoff)
    fig.tight_layout(rect=[0, 0.03, 1, 0.905])
    return save(fig, "01_cycle_breakdown.png")


def fig_manager(mgr: pd.DataFrame, flag_pct: float, cutoff) -> Path:
    """Mediana de días en fases CONTROLABLES por case manager vs la firma."""
    m = mgr.sort_values("median_controllable_days", ascending=False).reset_index(drop=True)
    firm = m["firm_median_controllable_days"].iloc[0]
    y = np.arange(len(m))

    fig, ax = plt.subplots(figsize=(12.5, 5.8))
    ax.axvspan(firm * (1 - flag_pct), firm * (1 + flag_pct), color="#9aa5b1", alpha=0.13, lw=0)
    ax.axvline(firm, color="#333", ls="--", lw=1.4)
    bars = ax.barh(y, m["median_controllable_days"], color=m["performance_flag"].map(FLAG_COLORS), height=0.6)
    for bar, low in zip(bars, m["sample_flag"].eq("Low sample")):
        if low:
            bar.set_hatch("//")
            bar.set_edgecolor("white")
    ax.scatter(m["p90_controllable_days"], y, marker="|", s=320, color="black", linewidths=2, zorder=3)
    for i, r in m.iterrows():
        ax.text(r["p90_controllable_days"] + 1.2, i,
                f"median {r['median_controllable_days']:.0f} d  ({r['controllable_vs_firm_pct']:+.0%} vs firm)  ·  n={int(r['closed_cases'])}",
                va="center", fontsize=10)
    ax.set_yticks(y, labels=m["case_manager"])
    ax.set_xlim(0, m["p90_controllable_days"].max() * 1.62)
    ax.set_xlabel("Days in controllable phases (Demand Prep + Release Execution + Disbursement Prep)")
    ax.text(firm, len(m) - 0.45, f"Firm median {firm:.0f} d", ha="center", fontsize=9.5, color="#333")
    ax.grid(axis="y", visible=False)
    ax.legend(handles=[Patch(color=c, label=l) for l, c in FLAG_COLORS.items()]
                       + [Line2D([], [], marker="|", color="black", ls="", markersize=12, label="P90")],
              loc="upper center", bbox_to_anchor=(0.5, -0.16), ncol=4, frameon=False, fontsize=10)

    slow = m[m["performance_flag"] == "SLOWER than firm"].sort_values("controllable_vs_firm_pct", ascending=False)
    title = (f"{slow.iloc[0]['case_manager']} takes {slow.iloc[0]['controllable_vs_firm_pct']:+.0%} longer than the firm median on controllable phases"
             if len(slow) else f"No case manager deviates more than ±{flag_pct:.0%} from the firm median")
    headline(fig, title,
             "Controllable work only: the firm decides how fast these steps move. Shaded band = ±"
             f"{flag_pct:.0%} of firm median.", cutoff)
    fig.tight_layout(rect=[0, 0.03, 1, 0.905])
    return save(fig, "02_manager_scorecard.png")


def fig_carrier(car: pd.DataFrame, flag_pct: float, cutoff) -> Path:
    """Espera de primera oferta y de cheque por aseguradora."""
    c = car.sort_values("median_offer_wait_days", ascending=False).reset_index(drop=True)
    y = np.arange(len(c))
    labels = [f"{n}{'*' if s == 'Low sample' else ''}  (n={int(k)})"
              for n, s, k in zip(c["insurance_company"], c["sample_flag"], c["closed_cases"])]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14.5, 6.4), sharey=True)
    panels = [(ax1, "median_offer_wait_days", "p90_offer_wait_days", "firm_median_offer_wait_days",
               "offer_wait_flag", "Adjuster Offer Wait (days after demand)"),
              (ax2, "median_check_wait_days", "p90_check_wait_days", "firm_median_check_wait_days",
               "check_wait_flag", "Carrier Check Wait (days after signed release)")]
    for ax, med, p90, firm, flag, title in panels:
        f = c[firm].iloc[0]
        ax.axvline(f, color="#333", ls="--", lw=1.3)
        ax.axvspan(f * (1 - flag_pct), f * (1 + flag_pct), color="#9aa5b1", alpha=0.13, lw=0)
        bars = ax.barh(y, c[med], color=c[flag].map(FLAG_COLORS), height=0.62)
        for bar, low in zip(bars, c["sample_flag"].eq("Low sample")):
            if low:
                bar.set_hatch("//")
                bar.set_edgecolor("white")
        ax.scatter(c[p90], y, marker="|", s=260, color="black", linewidths=1.8, zorder=3)
        for i, r in c.iterrows():
            ax.text(r[p90] + c[p90].max() * 0.02, i, f"median {r[med]:.0f} d  (P90 {r[p90]:.0f})",
                    va="center", fontsize=9.5)
        ax.set_xlim(0, c[p90].max() * 1.5)
        ax.grid(axis="y", visible=False)
        ax.set_title(title, loc="left")
        ax.text(f, len(c) - 0.35, f"Firm median {f:.0f} d", ha="center", fontsize=9, color="#333",
                bbox=dict(fc="white", ec="none", pad=1.5))
    ax1.set_yticks(y, labels=labels)
    fig.legend(handles=[Patch(color=col, label=l) for l, col in FLAG_COLORS.items()]
                        + [Line2D([], [], marker="|", color="black", ls="", markersize=12, label="P90")],
               loc="lower center", bbox_to_anchor=(0.5, 0.0), ncol=4, frameon=False, fontsize=10)

    slow = c[(c["offer_wait_flag"] == "SLOWER than firm") | (c["check_wait_flag"] == "SLOWER than firm")]
    names = ", ".join(slow["insurance_company"].tolist()) if len(slow) else "none"
    headline(fig, f"Carriers flagged slower than the firm median: {names}",
             f"Median days per carrier (bar) and P90 (tick). * = fewer than 15 closed cases (hatched): read with caution. "
             f"Litigation mix differs by carrier ({c['pct_litigation'].min():.0%}–{c['pct_litigation'].max():.0%}).", cutoff)
    fig.tight_layout(rect=[0, 0.07, 1, 0.9])
    return save(fig, "03_carrier_scorecard.png")


def fig_blocking(blk: pd.DataFrame, closed: pd.DataFrame, cutoff) -> Path:
    """Camino crítico: ¿cuánto bloquean las reducciones el desembolso?"""
    ov = blk[blk["dimension"] == "1_Overall"].iloc[0]
    lien = blk[blk["dimension"] == "2_Formal liens"].set_index("dimension_value")
    order = ["With formal liens", "LOPs only (no formal liens)"]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6.2), gridspec_kw={"width_ratios": [1, 1.15]})

    rows = [("All closed cases", ov["pct_blocked"], ov["blocked_cases"], ov["closed_cases"], "#7f8fa6")]
    for g in order:
        r = lien.loc[g]
        rows.append((g, r["pct_blocked"], r["blocked_cases"], r["closed_cases"], GROUP_COLORS[g]))
    lab = [r[0] for r in rows][::-1]
    ax1.barh(lab, [r[1] for r in rows][::-1], color=[r[4] for r in rows][::-1], height=0.55)
    for i, r in enumerate(rows[::-1]):
        ax1.text(r[1] + 0.015, i, f"{r[1]:.0%}  ({int(r[2])}/{int(r[3])})", va="center", fontsize=11, fontweight="bold")
    ax1.set_xlim(0, 1.25)
    ax1.xaxis.set_major_formatter(mtick.PercentFormatter(1.0, decimals=0))
    ax1.set_xticks(np.arange(0, 1.01, 0.2))
    ax1.set_title("% of cases where reductions delayed disbursement", loc="left")
    ax1.grid(axis="y", visible=False)

    cl = closed.copy()
    cl["group"] = np.where(cl["original_liens_amount"] > 0, order[0], order[1])
    sns.boxplot(data=cl, x="group", y="reductions_blocking_days", order=order, color="white",
                fliersize=0, linewidth=1.4, ax=ax2)
    sns.stripplot(data=cl, x="group", y="reductions_blocking_days", order=order, hue="group",
                  palette=GROUP_COLORS, size=5, alpha=0.65, jitter=0.22, legend=False, ax=ax2)
    ax2.set_ylabel("Days the check sat waiting on reductions")
    ax2.set_title("Blocking days per closed case", loc="left")
    for i, g in enumerate(order):
        r = lien.loc[g]
        ax2.text(i, cl["reductions_blocking_days"].max() * 1.1,
                 f"avg {r['avg_blocking_days_when_blocked']:.0f} d when blocked", ha="center", fontsize=10)
    ax2.set_ylim(-5, cl["reductions_blocking_days"].max() * 1.18)
    ax2.set_xlabel("0 days = reductions were confirmed before the check arrived", fontsize=9.5, color="#555")

    share = lien.loc[order[0], "pct_of_all_blocking_days"]
    headline(fig, f"Formal liens generate {share:.0%} of all days that checks sat waiting on reductions",
             f"Reductions were the critical path in {int(ov['blocked_cases'])} of {int(ov['closed_cases'])} closed cases "
             f"({ov['pct_blocked']:.0%}), averaging {ov['avg_blocking_days_when_blocked']:.0f} days when they blocked.", cutoff)
    fig.tight_layout(rect=[0, 0.03, 1, 0.905])
    return save(fig, "04_reductions_blocking.png")


def fig_savings(sav: pd.DataFrame, per_case: pd.DataFrame, lien_cap: float, cutoff) -> Path:
    """Ahorro de Liens formales (tope 25%) vs Medical Bills/LOPs."""
    ov = sav[sav["dimension"] == "1_Overall"].iloc[0]
    inj = sav[sav["dimension"] == "2_Injury type"].sort_values("bills_savings_pct", ascending=False)

    long = pd.concat([
        pd.DataFrame({"type": "Medical Bills / LOPs", "savings": per_case["bills_savings_pct"]}),
        pd.DataFrame({"type": "Formal liens", "savings": per_case["liens_savings_pct"]}),
    ]).dropna()
    order = ["Medical Bills / LOPs", "Formal liens"]
    pal = {"Medical Bills / LOPs": BILLS_COLOR, "Formal liens": LIENS_COLOR}

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14.5, 6.4), gridspec_kw={"width_ratios": [1, 1.5]})

    sns.boxplot(data=long, x="type", y="savings", order=order, color="white", fliersize=0, linewidth=1.4, ax=ax1)
    sns.stripplot(data=long, x="type", y="savings", order=order, hue="type", palette=pal,
                  size=4.5, alpha=0.55, jitter=0.25, legend=False, ax=ax1)
    ax1.axhline(lien_cap, color=LIENS_COLOR, ls="--", lw=1.4)
    ax1.text(1.45, lien_cap + 0.01, f"{lien_cap:.0%} cap on formal liens", color=LIENS_COLOR, fontsize=9.5, ha="right")
    ax1.yaxis.set_major_formatter(mtick.PercentFormatter(1.0, decimals=0))
    ax1.set_ylabel("Reduction per case (% of original)")
    ax1.set_xlabel("")
    ax1.set_title("Per-case reduction achieved", loc="left")
    for i, v in enumerate([ov["bills_savings_pct"], ov["liens_savings_pct"]]):
        ax1.text(i, -0.075, f"$-weighted {v:.1%}", ha="center", fontsize=10.5, fontweight="bold")
    ax1.set_ylim(-0.1, max(0.75, long["savings"].max() * 1.05))
    ax1.set_yticks(np.arange(0, ax1.get_ylim()[1], 0.1))

    x = np.arange(len(inj))
    w = 0.38
    ax2.bar(x - w / 2, inj["bills_savings_pct"], w, color=BILLS_COLOR, label="Medical Bills / LOPs")
    ax2.bar(x + w / 2, inj["liens_savings_pct"].fillna(0), w, color=LIENS_COLOR, label="Formal liens")
    for xi, b, l in zip(x, inj["bills_savings_pct"], inj["liens_savings_pct"]):
        ax2.text(xi - w / 2, b + 0.008, f"{b:.0%}", ha="center", fontsize=9)
        if pd.notna(l):
            ax2.text(xi + w / 2, l + 0.008, f"{l:.0%}", ha="center", fontsize=9)
    ax2.axhline(lien_cap, color=LIENS_COLOR, ls="--", lw=1)
    ax2.set_xlim(-0.6, len(inj) + 0.55)
    ax2.text(len(inj) + 0.5, lien_cap + 0.008, f"{lien_cap:.0%} lien cap", color=LIENS_COLOR, fontsize=9, ha="right")
    ax2.set_xticks(x, labels=[textwrap.fill(s.replace("/", " / "), 13) for s in inj["dimension_value"]], fontsize=9)
    ax2.yaxis.set_major_formatter(mtick.PercentFormatter(1.0, decimals=0))
    ax2.set_title("$-weighted savings by injury type", loc="left")
    ax2.legend(frameon=True, fontsize=9.5)
    ax2.grid(axis="x", visible=False)

    headline(fig, f"LOPs are negotiated down {ov['bills_savings_pct']:.0%} on average; formal liens only {ov['liens_savings_pct']:.0%}",
             f"Reductions raise the client's net recovery by {ov['client_net_uplift_pct']:.0%} "
             f"({usd(ov['total_savings_amount'])} saved across {int(ov['cases_with_reductions'])} cases). "
             f"Max lien reduction observed: {per_case['liens_savings_pct'].max():.1%} (cap {lien_cap:.0%}).", cutoff)
    fig.tight_layout(rect=[0, 0.03, 1, 0.9])
    return save(fig, "05_reduction_savings.png")


def fig_alerts(alerts: pd.DataFrame, cutoff) -> Path:
    """Cartera abierta: top casos por dinero en juego + pipeline por fase."""
    # Top 15 por dinero, pero TODO caso con alerta (RED/AMBER/WATCH) entra siempre al gráfico
    ranked = alerts.sort_values("priority_rank")
    flagged = ranked[ranked["alert_level"] != "ON TRACK"]
    rest = ranked[ranked["alert_level"] == "ON TRACK"].head(max(0, 15 - len(flagged)))
    top = pd.concat([flagged, rest]).sort_values("priority_rank").reset_index(drop=True)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15, 7.2), gridspec_kw={"width_ratios": [1.6, 1]})

    yy = np.arange(len(top))[::-1]
    ax1.barh(yy, top["money_at_stake"], color=top["alert_level"].map(ALERT_COLORS), height=0.68)
    for y, r in zip(yy, top.itertuples()):
        star = "*" if str(r.amount_basis).startswith("Estimated") else ""
        ax1.text(r.money_at_stake + top["money_at_stake"].max() * 0.01, y,
                 f"{usd(r.money_at_stake)}{star} · {r.phase_name} {r.days_in_current_phase}/{r.threshold_days} d",
                 va="center", fontsize=9.3)
    ax1.set_yticks(yy, labels=[f"{r.case_id}  ({r.case_manager.split()[0]})" for r in top.itertuples()], fontsize=9.3)
    ax1.set_xlim(0, top["money_at_stake"].max() * 1.62)
    ax1.xaxis.set_major_formatter(mtick.FuncFormatter(usd))
    ax1.set_title("Open cases by money at stake (all flagged cases included)", loc="left")
    ax1.legend(handles=[Patch(color=c, label=l) for l, c in ALERT_COLORS.items()],
               loc="lower right", frameon=True, fontsize=9.5, title="Alert level")
    ax1.grid(axis="y", visible=False)

    g = (alerts.groupby("current_phase")
               .agg(cases=("case_id", "count"), money=("money_at_stake", "sum"), ptype=("phase_type", "first"))
               .reindex([p for p in PHASE_ORDER if p in set(alerts["current_phase"])]))
    names = alerts.drop_duplicates("current_phase").set_index("current_phase")["phase_name"]
    yy2 = np.arange(len(g))[::-1]
    ax2.barh(yy2, g["cases"], color=g["ptype"].map(TYPE_COLORS), height=0.62)
    for y, (ph, r) in zip(yy2, g.iterrows()):
        ax2.text(r["cases"] + 0.2, y, f"{int(r['cases'])}  ·  {usd(r['money'])}", va="center", fontsize=9.8)
    ax2.set_yticks(yy2, labels=[names[p] for p in g.index], fontsize=9.5)
    ax2.set_xlim(0, g["cases"].max() * 1.5)
    ax2.set_xlabel("Open cases")
    ax2.xaxis.set_major_locator(mtick.MaxNLocator(integer=True))
    ax2.set_title("Where the open pipeline sits today", loc="left")
    ax2.grid(axis="y", visible=False)
    ax2.legend(handles=[Patch(color=c, label=t) for t, c in TYPE_COLORS.items()],
               loc="lower right", frameon=True, fontsize=9, title="Phase type")

    n_flag = int(alerts["alert_level"].isin(["RED", "AMBER", "WATCH"]).sum())
    money_flag = alerts.loc[alerts["alert_level"].isin(["RED", "AMBER", "WATCH"]), "money_at_stake"].sum()
    headline(fig, f"{len(alerts)} open cases · {usd(alerts['money_at_stake'].sum())} at stake · "
                  f"{n_flag} need attention ({usd(money_flag)})",
             "Bar label: money at stake · current phase, days in phase / alert threshold. "
             "* = estimated amount (no settlement accepted yet).", cutoff)
    fig.tight_layout(rect=[0, 0.03, 1, 0.905])
    return save(fig, "06_open_case_alerts.png")


# ──────────────────────────────────────────────────────────────────────────────
# 5. HALLAZGOS (cifras calculadas, no escritas a mano)
# ──────────────────────────────────────────────────────────────────────────────
def write_findings(phase, ptype, mgr, car, blk, sav, alerts, cutoff, flag_pct) -> Path:
    t = ptype.set_index("phase_type")
    ov_b = blk[blk["dimension"] == "1_Overall"].iloc[0]
    lien = blk[blk["dimension"] == "2_Formal liens"].set_index("dimension_value")
    drv = blk[blk["dimension"] == "3_Critical path driver"].set_index("dimension_value")
    ov_s = sav[sav["dimension"] == "1_Overall"].iloc[0]
    slow_m = mgr[mgr["performance_flag"] == "SLOWER than firm"]
    slow_c = car[(car["offer_wait_flag"] == "SLOWER than firm") | (car["check_wait_flag"] == "SLOWER than firm")]
    flagged = alerts[alerts["alert_level"].isin(["RED", "AMBER", "WATCH"])]

    L = [f"# Key findings (data cutoff {cutoff:%Y-%m-%d})", ""]
    L += ["## Cycle time",
          f"- Average cycle: **{phase['avg_days'].sum():.0f} days**. Treatment = {t.loc['Clinical', 'pct_of_total_cycle']:.0%}, "
          f"External waits = {t.loc['External', 'pct_of_total_cycle']:.0%}, Strategic negotiation = {t.loc['Strategic', 'pct_of_total_cycle']:.0%}, "
          f"**Controllable = {t.loc['Controllable', 'pct_of_total_cycle']:.0%}** ({t.loc['Controllable', 'avg_days']:.0f} days).", ""]
    L += ["## Reductions on the critical path",
          f"- Reductions blocked disbursement in **{int(ov_b['blocked_cases'])} of {int(ov_b['closed_cases'])}** closed cases "
          f"({ov_b['pct_blocked']:.0%}), averaging {ov_b['avg_blocking_days_when_blocked']:.1f} days when blocked.",
          f"- With formal liens: {lien.loc['With formal liens', 'pct_blocked']:.0%} blocked vs "
          f"{lien.loc['LOPs only (no formal liens)', 'pct_blocked']:.0%} for LOPs-only cases; formal liens account for "
          f"**{lien.loc['With formal liens', 'pct_of_all_blocking_days']:.0%}** of all blocking days.",
          f"- Critical-path driver: Reductions in {int(drv.loc['Reductions', 'closed_cases'])} cases, "
          f"Carrier Check in {int(drv.loc['Carrier Check', 'closed_cases'])}.", ""]
    L += ["## Reduction savings",
          f"- Medical Bills / LOPs: **{ov_s['bills_savings_pct']:.1%}** ($-weighted). Formal liens: **{ov_s['liens_savings_pct']:.1%}** "
          f"(max observed {ov_s['liens_max_savings_pct']:.1%}, {int(ov_s['liens_over_cap_cases'])} cases over the cap).",
          f"- Client net recovery rises **{ov_s['client_net_uplift_pct']:.1%}** thanks to reductions "
          f"({usd(ov_s['total_savings_amount'])} total savings).", ""]
    L += ["## Operational scorecards",
          "- Managers slower than firm (> +" + f"{flag_pct:.0%}" + " on controllable days): "
          + (", ".join(f"{r.case_manager} ({r.controllable_vs_firm_pct:+.0%})" for r in slow_m.itertuples()) or "none") + ".",
          "- Carriers slower than firm (offer or check): "
          + (", ".join(f"{r.insurance_company}{' (low sample)' if r.sample_flag == 'Low sample' else ''}" for r in slow_c.itertuples()) or "none") + ".",
          ""]
    L += ["## Open cases",
          f"- {len(alerts)} open cases, {usd(alerts['money_at_stake'].sum())} at stake "
          f"({int((alerts['amount_basis'] == 'Actual settlement').sum())} with an accepted settlement; the rest estimated).",
          "- Alerts: " + ", ".join(f"{k}={v}" for k, v in alerts["alert_level"].value_counts().items())
          + f". Cases flagged: {len(flagged)} ({usd(flagged['money_at_stake'].sum())})."]
    for r in flagged.sort_values("priority_rank").itertuples():
        L.append(f"  - #{r.priority_rank} {r.case_id} ({r.case_manager}, {r.insurance_company}): {r.alert_level} · {r.phase_name} "
                 f"{r.days_in_current_phase}/{r.threshold_days} d · {usd(r.money_at_stake)}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "key_findings.md"
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"   ✔ {path.relative_to(ROOT)}")
    return path


# ──────────────────────────────────────────────────────────────────────────────
# 6. MAIN
# ──────────────────────────────────────────────────────────────────────────────
def main() -> None:
    np.random.seed(42)          # semilla fija: el "jitter" de los puntos (stripplot) es aleatorio y, sin
    set_style()                 # ella, los PNG cambian en cada ejecución (ruido en el control de versiones)
    try:
        engine = get_engine()
        cutoff = check_database(engine)
        flag_pct = get_param(engine, "scorecard_flag_pct", 0.20)
        lien_cap = get_param(engine, "lien_reduction_cap_pct", 0.25)

        phase = query(engine, "SELECT * FROM pi.v_phase_summary ORDER BY phase_order")
        ptype = query(engine, "SELECT * FROM pi.v_phase_type_summary")
        mgr = query(engine, "SELECT * FROM pi.v_manager_scorecard")
        car = query(engine, "SELECT * FROM pi.v_carrier_scorecard")
        blk = query(engine, "SELECT * FROM pi.v_reductions_blocking")
        sav = query(engine, "SELECT * FROM pi.v_reduction_savings")
        alerts = query(engine, "SELECT * FROM pi.v_open_case_alerts ORDER BY priority_rank")
        closed = query(engine, "SELECT * FROM pi.v_case_phases WHERE is_closed")
        reduced = query(engine, "SELECT * FROM pi.v_case_phases WHERE reductions_confirmed_date IS NOT NULL")
    except DriverError as e:
        sys.exit(str(e))
    except SQLAlchemyError as e:
        sys.exit("No se pudo consultar PostgreSQL. Revisa la conexión (DATABASE_URL o PGHOST/PGPORT/PGUSER/"
                 f"PGPASSWORD/PGDATABASE) y que 01_schema.sql, el cargador y 02/03 .sql estén ejecutados.\n"
                 f"Detalle: {str(e).splitlines()[0]}")
    except RuntimeError as e:
        sys.exit(f"Verificación fallida: {e}")

    print("\nGenerando gráficos...")
    fig_cycle_breakdown(phase, ptype, cutoff)
    fig_manager(mgr, flag_pct, cutoff)
    fig_carrier(car, flag_pct, cutoff)
    fig_blocking(blk, closed, cutoff)
    fig_savings(sav, reduced, lien_cap, cutoff)
    fig_alerts(alerts, cutoff)

    print("\nEscribiendo hallazgos...")
    path = write_findings(phase, ptype, mgr, car, blk, sav, alerts, cutoff, flag_pct)
    print("\n" + path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()