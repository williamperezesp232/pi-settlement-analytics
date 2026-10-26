#!/usr/bin/env python3
"""
app.py  -  PI Legal Ops: Live Case Alerts (Streamlit)
=====================================================
Dashboard interactivo sobre las vistas de PostgreSQL del proyecto. Enfocado en la
VISTA DE ALERTAS: qué casos abiertos necesitan atención hoy, ordenados por dinero en juego.

Lee de la base (no del CSV):
    pi.v_open_case_alerts   alertas de los casos abiertos        (03_kpi_views.sql)
    pi.v_case_phases        detalle por caso, 9 fases            (02_metrics_view.sql)
    pi.v_phase_summary      medianas de la firma por fase        (03_kpi_views.sql)
    pi.v_manager_scorecard  / pi.v_carrier_scorecard             (03_kpi_views.sql)
    pi.report_params        fecha de corte y parámetros          (01_schema.sql)

Interactividad: filtros (manager, carrier, lesión, etapa, fase, nivel de alerta, tipo de monto,
monto mínimo), orden, escenario "what-if" de umbrales, detalle del caso al seleccionar una fila
y exportación a CSV.

Ejecutar (desde la raíz del proyecto):
    pip install -r requirements.txt
    streamlit run app.py

Conexión: mismas variables de entorno que el resto del proyecto (ver python/db_conn.py):
    DATABASE_URL, o PGHOST / PGPORT / PGUSER / PGPASSWORD / PGDATABASE.   Driver: pg8000.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st
from sqlalchemy import text

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "python"))
from db_conn import DriverError, get_engine  # noqa: E402

st.set_page_config(page_title="PI Legal Ops · Live Alerts", page_icon="⚖️", layout="wide")

# ──────────────────────────────────────────────────────────────────────────────
# 1. CONSTANTES
# ──────────────────────────────────────────────────────────────────────────────
CACHE_SECONDS = 60
LEVELS = ["RED", "AMBER", "WATCH", "ON TRACK"]
LEVEL_ICON = {"RED": "🔴", "AMBER": "🟠", "WATCH": "🟡", "ON TRACK": "🟢"}
LEVEL_COLOR = {"RED": "#d1495b", "AMBER": "#f4a261", "WATCH": "#e9c46a", "ON TRACK": "#b8c4cc"}
LEVEL_SCALE = alt.Scale(domain=LEVELS, range=[LEVEL_COLOR[x] for x in LEVELS])

PHASES = [  # (key, nombre, tipo)
    ("treatment", "Treatment", "Clinical"),
    ("records_wait", "Records Wait", "External"),
    ("demand_prep", "Demand Prep", "Controllable"),
    ("offer_wait", "Adjuster Offer Wait", "External"),
    ("negotiation", "Strategic Negotiation", "Strategic"),
    ("release_execution", "Release Execution", "Controllable"),
    ("carrier_check_wait", "Carrier Check Wait", "External"),
    ("reductions_blocking", "Reductions Blocking", "External"),
    ("disbursement_prep", "Disbursement Prep", "Controllable"),
]
PHASE_KEYS = [p[0] for p in PHASES]
PHASE_NAME = {k: n for k, n, _ in PHASES}
PHASE_NAMES = [n for _, n, _ in PHASES]

ACTIONS = {
    "treatment": "Check in with the client and providers; confirm the expected discharge / MMI date.",
    "records_wait": "Escalate outstanding record and billing requests provider by provider.",
    "demand_prep": "Prioritize the demand package; review the case manager's workload.",
    "offer_wait": "Follow up with the adjuster; escalate to the carrier's supervisor if past the window.",
    "negotiation": "Review strategy with the attorney: counter, mediation, or evaluate filing suit.",
    "release_execution": "Call the client to complete signing / notarization; resend the release today.",
    "carrier_check_wait": "Confirm the carrier received the signed release and get a check issue date.",
    "reductions_blocking": "The check is in hand: escalate pending reductions with billing departments and lienholders.",
    "disbursement_prep": "Finalize the closing statement and issue the client's payment.",
}
SORT_OPTIONS = ["Money at stake", "Severity, then money", "Days over threshold"]
BASIS_OPTIONS = ["All", "Accepted settlement only", "Estimated only"]


def usd(x: float) -> str:
    if abs(x) >= 1e6:
        return f"${x / 1e6:.1f}M"
    if abs(x) >= 1e3:
        return f"${x / 1e3:.0f}K"
    return f"${x:,.0f}"


# ──────────────────────────────────────────────────────────────────────────────
# 2. ACCESO A DATOS (con caché; el botón Refresh la limpia)
# ──────────────────────────────────────────────────────────────────────────────
@st.cache_resource(show_spinner=False)
def db_engine():
    return get_engine()


def run_query(sql: str, params: dict | None = None) -> pd.DataFrame:
    with db_engine().connect() as conn:
        return pd.read_sql(text(sql), conn, params=params, coerce_float=True)


@st.cache_data(ttl=CACHE_SECONDS, show_spinner="Reading alerts from PostgreSQL…")
def load_alerts() -> tuple[pd.DataFrame, datetime]:
    df = run_query("SELECT * FROM pi.v_open_case_alerts ORDER BY priority_rank")
    for c in ("phase_start_date", "intake_date"):
        df[c] = pd.to_datetime(df[c])
    return df, datetime.now()


@st.cache_data(ttl=CACHE_SECONDS)
def load_params() -> dict:
    p = run_query("SELECT param_key, value_num, value_date FROM pi.report_params "
                  "WHERE param_key IN ('report_cutoff_date', 'alert_watch_pct', 'alert_critical_multiplier')")
    p = p.set_index("param_key")
    if "report_cutoff_date" not in p.index:
        raise RuntimeError("Falta report_cutoff_date en pi.report_params")
    num = lambda k, d: float(p.loc[k, "value_num"]) if k in p.index and pd.notna(p.loc[k, "value_num"]) else d
    return {"cutoff": pd.Timestamp(p.loc["report_cutoff_date", "value_date"]),
            "watch": num("alert_watch_pct", 0.80), "red": num("alert_critical_multiplier", 1.5)}


@st.cache_data(ttl=CACHE_SECONDS)
def load_scorecards() -> tuple[pd.DataFrame, pd.DataFrame]:
    return (run_query("SELECT * FROM pi.v_manager_scorecard"),
            run_query("SELECT * FROM pi.v_carrier_scorecard"))


@st.cache_data(ttl=CACHE_SECONDS)
def load_phase_medians() -> pd.DataFrame:
    return run_query("SELECT phase_key, median_days FROM pi.v_phase_summary")


@st.cache_data(ttl=CACHE_SECONDS)
def load_case(case_id: str) -> pd.Series:
    return run_query("SELECT * FROM pi.v_case_phases WHERE case_id = :id", {"id": case_id}).iloc[0]


# ──────────────────────────────────────────────────────────────────────────────
# 3. LÓGICA DE ALERTAS (misma regla que pi.v_open_case_alerts, con umbrales ajustables)
# ──────────────────────────────────────────────────────────────────────────────
def classify(df: pd.DataFrame, scale: float, watch: float, red: float) -> pd.DataFrame:
    """RED: días > umbral x multiplicador | AMBER: días > umbral | WATCH: días >= umbral x watch%."""
    out = df.copy()
    thr = out["threshold_days"].astype(float) * scale
    days = out["days_in_current_phase"].astype(float)
    out["threshold_used"] = thr
    out["pct_used"] = days / thr
    out["days_over"] = (days - thr).clip(lower=0)
    out["alert_level"] = np.select([days > thr * red, days > thr, days >= thr * watch],
                                   ["RED", "AMBER", "WATCH"], default="ON TRACK")
    out["flagged"] = out["alert_level"] != "ON TRACK"
    out["money_k"] = out["money_at_stake"] / 1000
    return out


def sort_view(v: pd.DataFrame, choice: str) -> pd.DataFrame:
    if choice == "Severity, then money":
        v = v.assign(_sev=v["alert_level"].map({lvl: i for i, lvl in enumerate(LEVELS)}))
        v = v.sort_values(["_sev", "money_at_stake", "case_id"], ascending=[True, False, True])
    elif choice == "Days over threshold":
        v = v.sort_values(["days_over", "money_at_stake", "case_id"], ascending=[False, False, True])
    else:
        v = v.sort_values(["money_at_stake", "days_over", "case_id"], ascending=[False, False, True])
    return v.reset_index(drop=True)


# ──────────────────────────────────────────────────────────────────────────────
# 4. COMPONENTES DE GRÁFICO
# ──────────────────────────────────────────────────────────────────────────────
def level_color(title: str | None = "Alert") -> alt.Color:
    return alt.Color("alert_level:N", scale=LEVEL_SCALE, sort=LEVELS,
                     legend=alt.Legend(title=title, orient="bottom"))


def chart_phase_timeline(case: pd.Series, detail: pd.Series, medians: pd.DataFrame) -> alt.LayerChart:
    """Días de ESTE caso en cada fase vs la mediana de la firma (casos cerrados)."""
    med = medians.set_index("phase_key")["median_days"]
    rows = []
    for k in PHASE_KEYS:
        if k == case["current_phase"]:
            days, status = float(case["days_in_current_phase"]), "In progress"
        else:
            v = detail.get(f"{k}_days")
            days, status = (float(v), "Completed") if pd.notna(v) else (np.nan, "Not reached")
        rows.append({"phase": PHASE_NAME[k], "days": days, "status": status, "median": float(med[k])})
    d = pd.DataFrame(rows)
    y = alt.Y("phase:N", sort=PHASE_NAMES, title=None, axis=alt.Axis(labelOverlap=False, labelLimit=240))
    status_scale = alt.Scale(domain=["Completed", "In progress"],
                             range=["#2a9d8f", LEVEL_COLOR[case["alert_level"]]])
    bars = (alt.Chart(d.dropna(subset=["days"])).mark_bar(size=16)
            .encode(y=y, x=alt.X("days:Q", title="Days"),
                    color=alt.Color("status:N", scale=status_scale, legend=alt.Legend(title=None, orient="bottom")),
                    tooltip=["phase", "status", alt.Tooltip("days:Q", format=".0f")]))
    ticks = (alt.Chart(d).mark_tick(color="black", thickness=2.5, size=22)
             .encode(y=y, x="median:Q", tooltip=["phase", alt.Tooltip("median:Q", title="Firm median", format=".1f")]))
    thr = pd.DataFrame({"phase": [PHASE_NAME[case["current_phase"]]], "threshold": [float(case["threshold_used"])]})
    diamond = (alt.Chart(thr).mark_point(shape="diamond", size=130, filled=True, color="#222")
               .encode(y=y, x="threshold:Q", tooltip=[alt.Tooltip("threshold:Q", title="Alert threshold", format=".0f")]))
    return alt.layer(bars, ticks, diamond).properties(height=340)


def chart_risk_map(v: pd.DataFrame, watch: float, red: float) -> alt.LayerChart:
    x_max = max(150.0, float(v["pct_used"].max()) * 108)
    pts = (alt.Chart(v).mark_circle(opacity=0.85, stroke="white", strokeWidth=1)
           .encode(x=alt.X("pct_plot:Q", title="% of alert threshold used  (days in phase ÷ threshold)",
                           scale=alt.Scale(domain=[0, x_max])),
                   y=alt.Y("money_k:Q", title="Money at stake ($K)"),
                   size=alt.Size("money_k:Q", legend=None, scale=alt.Scale(range=[70, 750])),
                   color=level_color(),
                   tooltip=["case_id", "case_manager", "insurance_company", "phase_name",
                            alt.Tooltip("days_in_current_phase:Q", title="Days in phase"),
                            alt.Tooltip("threshold_used:Q", title="Threshold (d)", format=".0f"),
                            alt.Tooltip("money_at_stake:Q", title="At stake", format="$,.0f"), "alert_level"]))
    lines = pd.DataFrame({"x": [watch * 100, 100.0, red * 100], "label": ["WATCH", "AMBER", "RED"]})
    rules = alt.Chart(lines).mark_rule(strokeDash=[5, 4], color="#777").encode(x="x:Q")
    labels = (alt.Chart(lines).mark_text(align="left", baseline="top", dx=4, dy=2, color="#555", fontSize=11)
              .encode(x="x:Q", y=alt.value(0), text="label:N"))
    return alt.layer(rules, labels, pts).properties(height=380)


def chart_by_phase(v: pd.DataFrame, measure: str) -> alt.Chart:
    enc = (alt.X("count():Q", title="Open cases") if measure == "count"
           else alt.X("sum(money_k):Q", title="Money at stake ($K)"))
    return (alt.Chart(v.assign(_ord=v["alert_level"].map({lvl: i for i, lvl in enumerate(LEVELS)})))
            .mark_bar()
            .encode(y=alt.Y("phase_name:N", sort=PHASE_NAMES, title=None,
                            axis=alt.Axis(labelOverlap=False, labelLimit=240)), x=enc,
                    color=level_color(None), order=alt.Order("_ord:Q", sort="ascending"),
                    tooltip=["phase_name", "alert_level", alt.Tooltip("count():Q", title="Cases")])
            .properties(height=340))


def chart_by_owner(v: pd.DataFrame, col: str, title: str) -> alt.Chart:
    order = v.groupby(col).size().sort_values(ascending=False).index.tolist()
    return (alt.Chart(v.assign(_ord=v["alert_level"].map({lvl: i for i, lvl in enumerate(LEVELS)})))
            .mark_bar()
            .encode(y=alt.Y(f"{col}:N", sort=order, title=None, axis=alt.Axis(labelOverlap=False, labelLimit=240)),
                    x=alt.X("count():Q", title=title),
                    color=level_color(None), order=alt.Order("_ord:Q", sort="ascending"),
                    tooltip=[col, "alert_level", alt.Tooltip("count():Q", title="Cases")])
            .properties(height=max(160, 38 * len(order))))


def short_flag(x: str | None) -> str:
    return {"SLOWER than firm": "Slower", "FASTER than firm": "Faster", "In line": "In line"}.get(x, "n/a")


def owner_table(v: pd.DataFrame, col: str) -> pd.DataFrame:
    g = v.groupby(col)
    t = pd.DataFrame({"open_cases": g.size(), "money_m": g["money_at_stake"].sum() / 1e6})
    for lvl in ("RED", "AMBER", "WATCH"):
        t[lvl] = v[v["alert_level"] == lvl].groupby(col).size()
    t["flagged_money_m"] = v[v["flagged"]].groupby(col)["money_at_stake"].sum() / 1e6
    return t.fillna(0).reset_index()


# ──────────────────────────────────────────────────────────────────────────────
# 5. APP
# ──────────────────────────────────────────────────────────────────────────────
def main() -> None:
    # ── Datos --------------------------------------------------------------------------------
    try:
        raw, loaded_at = load_alerts()
        params = load_params()
        managers_sc, carriers_sc = load_scorecards()
        medians = load_phase_medians()
    except DriverError as e:
        st.error(str(e))
        st.stop()
    except Exception as e:  # noqa: BLE001  (demo: mensaje claro en vez de traceback)
        st.error("Could not read the PostgreSQL views. Check the connection (DATABASE_URL or "
                 "PGHOST / PGPORT / PGUSER / PGPASSWORD / PGDATABASE) and that sql/01 to 03 and "
                 "python/01b_load_to_postgres.py have been run.")
        with st.expander("Technical detail"):
            st.code(" ".join(str(e).split())[:700])
        st.stop()

    # ── Estado inicial de los filtros (sin pasar defaults a los widgets) ---------------------------
    defaults = {"f_manager": [], "f_carrier": [], "f_injury": [], "f_stage": [], "f_phase": [], "f_level": [],
                "f_basis": BASIS_OPTIONS[0], "f_min_money": 0, "f_sort": SORT_OPTIONS[0],
                "w_custom": False, "w_scale": 100, "w_watch": int(round(params["watch"] * 100)),
                "w_red": float(params["red"])}
    for k, v in defaults.items():
        st.session_state.setdefault(k, v)

    def reset_filters() -> None:
        for k, v in defaults.items():
            st.session_state[k] = v

    # ── Sidebar ----------------------------------------------------------------------------------
    sb = st.sidebar
    sb.header("Filters")
    sb.multiselect("Case manager", sorted(raw["case_manager"].unique()), key="f_manager", placeholder="All")
    sb.multiselect("Insurance carrier", sorted(raw["insurance_company"].unique()), key="f_carrier", placeholder="All")
    sb.multiselect("Injury type", sorted(raw["injury_type"].unique()), key="f_injury", placeholder="All")
    sb.multiselect("Case stage", sorted(raw["case_stage"].unique()), key="f_stage", placeholder="All")
    sb.multiselect("Current phase", PHASE_NAMES, key="f_phase", placeholder="All")
    sb.multiselect("Alert level", LEVELS, key="f_level", placeholder="All",
                   format_func=lambda x: f"{LEVEL_ICON[x]} {x}")
    sb.radio("Amount type", BASIS_OPTIONS, key="f_basis",
             help="Accepted = real settlement amount. Estimated = based on similar closed cases.")
    sb.slider("Min. money at stake ($K)", 0, int(raw["money_at_stake"].max() // 1000) + 1, step=25, key="f_min_money")
    sb.selectbox("Sort queue by", SORT_OPTIONS, key="f_sort")

    sb.divider()
    custom = sb.toggle("What-if: customize alert thresholds", key="w_custom",
                       help="Recalculates alert levels in this session only. The database is not modified.")
    if custom:
        sb.slider("Scale all thresholds (%)", 50, 200, step=5, key="w_scale",
                  help="100% = thresholds stored in pi.report_params.")
        sb.slider("WATCH starts at (% of threshold)", 50, 95, step=5, key="w_watch")
        sb.slider("RED starts at (× threshold)", 1.1, 3.0, step=0.1, key="w_red")
    sb.divider()
    c1, c2 = sb.columns(2)
    c1.button("↺ Reset", on_click=reset_filters, width="stretch")
    if c2.button("⟳ Refresh", width="stretch", help="Re-read the views from PostgreSQL"):
        st.cache_data.clear()
        st.rerun()

    scale = st.session_state["w_scale"] / 100 if custom else 1.0
    watch = st.session_state["w_watch"] / 100 if custom else params["watch"]
    red = st.session_state["w_red"] if custom else params["red"]
    df = classify(raw, scale, watch, red)
    df["pct_plot"] = df["pct_used"] * 100

    if not custom and (df["alert_level"].values != raw["alert_level"].values).any():
        st.warning("Alert levels computed in the app differ from pi.v_open_case_alerts. Check the thresholds.")

    # ── Filtros ---------------------------------------------------------------------------------------
    s = st.session_state
    mask = pd.Series(True, index=df.index)
    for key, col in (("f_manager", "case_manager"), ("f_carrier", "insurance_company"), ("f_injury", "injury_type"),
                     ("f_stage", "case_stage"), ("f_phase", "phase_name"), ("f_level", "alert_level")):
        if s[key]:
            mask &= df[col].isin(s[key])
    if s["f_basis"] == BASIS_OPTIONS[1]:
        mask &= df["amount_basis"] == "Actual settlement"
    elif s["f_basis"] == BASIS_OPTIONS[2]:
        mask &= df["amount_basis"] != "Actual settlement"
    mask &= df["money_at_stake"] >= s["f_min_money"] * 1000
    view = sort_view(df[mask], s["f_sort"])

    # ── Encabezado y KPIs -------------------------------------------------------------------------------
    st.title("⚖️ PI Legal Ops: Live Case Alerts")
    st.caption(f"Alerts measured as of **{params['cutoff']:%b %d, %Y}** (report cutoff in `pi.report_params`, "
               f"not today's date) · synthetic data · read from PostgreSQL at {loaded_at:%H:%M:%S}"
               + (" · **what-if thresholds active**" if custom else ""))

    if view.empty:
        st.info("No open cases match the current filters.")
        st.stop()

    n_flag = int(view["flagged"].sum())
    mix = {lvl: int((view["alert_level"] == lvl).sum()) for lvl in LEVELS[:3]}
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Open cases", len(view), delta=f"of {len(df)} total" if len(view) != len(df) else None,
              delta_color="off", delta_arrow="off", border=True)
    k2.metric("Money at stake", usd(view["money_at_stake"].sum()), border=True,
              help="Accepted settlement when it exists; otherwise estimated from similar closed cases.")
    k3.metric("Flagged cases", n_flag, delta=f"{n_flag / len(view):.0%} of shown", delta_color="off", delta_arrow="off", border=True)
    k4.metric("Flagged money", usd(view.loc[view["flagged"], "money_at_stake"].sum()), border=True)
    k5.metric(f"{LEVEL_ICON['RED']} Red · {LEVEL_ICON['AMBER']} Amber · {LEVEL_ICON['WATCH']} Watch",
              f"{mix['RED']} · {mix['AMBER']} · {mix['WATCH']}", border=True)

    tab_q, tab_map, tab_own, tab_info = st.tabs(["🚨 Alert queue", "🗺️ Risk map", "👥 Owners", "ℹ️ Method"])

    # ── TAB 1: cola de alertas + detalle ----------------------------------------------------------------------
    with tab_q:
        table = pd.DataFrame({
            "Alert": view["alert_level"].map(lambda x: f"{LEVEL_ICON[x]} {x}"),
            "Case": view["case_id"], "Current phase": view["phase_name"],
            "Days": view["days_in_current_phase"], "Limit (d)": view["threshold_used"].round(0),
            "% of limit": view["pct_plot"], "At stake": view["money_k"],
            "Manager": view["case_manager"], "Carrier": view["insurance_company"],
            "Over by (d)": view["days_over"].round(0), "Injury": view["injury_type"], "Stage": view["case_stage"],
            "Amount": np.where(view["amount_basis"] == "Actual settlement", "Accepted", "Estimated"),
        })
        pct_max = float(max(150, np.ceil(view["pct_plot"].max() / 10) * 10))
        event = st.dataframe(
            table, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
            key="queue_table", height=min(40 + 35 * len(table), 460),
            column_config={
                "Days": st.column_config.NumberColumn(format="%d", width="small"),
                "Limit (d)": st.column_config.NumberColumn(format="%d", width="small",
                                                           help="Alert threshold for the current phase"),
                "% of limit": st.column_config.ProgressColumn(format="%.0f%%", min_value=0, max_value=pct_max,
                                                              help="Days in phase ÷ alert threshold"),
                "Over by (d)": st.column_config.NumberColumn(format="%d"),
                "At stake": st.column_config.NumberColumn(format="$%.0fK"),
            })
        left, right = st.columns([4, 1])
        left.caption("Click a row's checkbox to inspect that case below (by default: the first flagged case). "
                     "'Estimated' amounts come from similar closed cases.")
        export_cols = ["case_id", "alert_level", "case_manager", "insurance_company", "injury_type", "case_stage",
                       "phase_name", "days_in_current_phase", "threshold_used", "days_over", "money_at_stake",
                       "amount_basis", "firm_fee_at_stake", "intake_date", "phase_start_date"]
        right.download_button("⬇ Export CSV", view[export_cols].to_csv(index=False).encode("utf-8"),
                              file_name="pi_open_case_alerts.csv", mime="text/csv", width="stretch")

        sel = event.selection.rows
        if sel and sel[0] < len(view):
            idx = sel[0]
        else:                                    # sin selección: abrir el primer caso con alerta de la cola
            flagged_pos = np.flatnonzero(view["flagged"].to_numpy())
            idx = int(flagged_pos[0]) if len(flagged_pos) else 0
        case = view.iloc[idx]
        detail = load_case(case["case_id"])

        st.divider()
        st.subheader(f"{LEVEL_ICON[case['alert_level']]} {case['case_id']} · {case['phase_name']}")
        d1, d2, d3 = st.columns([1, 1, 2])
        with d1:
            st.metric("Money at stake", usd(case["money_at_stake"]),
                      delta="accepted settlement" if case["amount_basis"] == "Actual settlement" else "estimated",
                      delta_color="off", delta_arrow="off")
            st.metric("Firm fee at stake", usd(case["firm_fee_at_stake"]),
                      delta=f"{case['firm_fee_at_stake'] / case['money_at_stake']:.0%} fee · {case['case_stage']}",
                      delta_color="off", delta_arrow="off")
            st.metric("Days open", int(case["days_open"]), delta=f"since {case['intake_date']:%b %d, %Y}",
                      delta_color="off", delta_arrow="off")
        with d2:
            st.metric("Days in current phase", int(case["days_in_current_phase"]),
                      delta=f"{case['pct_used']:.0%} of {case['threshold_used']:.0f} d threshold", delta_color="off", delta_arrow="off")
            mrow = managers_sc[managers_sc["case_manager"] == case["case_manager"]]
            crow = carriers_sc[carriers_sc["insurance_company"] == case["insurance_company"]]
            st.markdown(f"**Manager:** {case['case_manager']}")
            if len(mrow):
                m = mrow.iloc[0]
                st.caption(f"Controllable median {m['median_controllable_days']:.0f} d "
                           f"({m['controllable_vs_firm_pct']:+.0%} vs firm) · {m['performance_flag']}")
            st.markdown(f"**Carrier:** {case['insurance_company']}")
            if len(crow):
                c = crow.iloc[0]
                st.caption(f"Offer wait {c['median_offer_wait_days']:.0f} d ({c['offer_wait_flag']}) · "
                           f"check wait {c['median_check_wait_days']:.0f} d ({c['check_wait_flag']})"
                           + (" · low sample" if c["sample_flag"] == "Low sample" else ""))
        with d3:
            st.altair_chart(chart_phase_timeline(case, detail, medians), width="stretch")
            st.caption("Bars = this case · black tick = firm median (closed cases) · ◆ = alert threshold of the current phase")

        action = ACTIONS[case["current_phase"]]
        {"RED": st.error, "AMBER": st.warning, "WATCH": st.info}.get(case["alert_level"], st.success)(
            f"**Next action ({case['alert_level']}):** {action}")

    # ── TAB 2: mapa de riesgo ---------------------------------------------------------------------------------------
    with tab_map:
        st.markdown("**Where is the money, and how close is each case to its alert threshold?** "
                    "Cases to the right of a dashed line have crossed that alert level; bigger bubbles = more money.")
        st.altair_chart(chart_risk_map(view, watch, red), width="stretch")
        a, b = st.columns(2)
        with a:
            st.markdown("**Open cases by current phase**")
            st.altair_chart(chart_by_phase(view, "count"), width="stretch")
        with b:
            st.markdown("**Money at stake by current phase ($K)**")
            st.altair_chart(chart_by_phase(view, "money"), width="stretch")

    # ── TAB 3: responsables ------------------------------------------------------------------------------------------
    with tab_own:
        st.markdown("Open-case load and alerts per owner, next to each owner's **closed-case** scorecard. "
                    "Scorecards describe the past and are not adjusted for case mix: read low-sample flags with caution.")
        mt = owner_table(view, "case_manager").merge(
            managers_sc[["case_manager", "closed_cases", "median_controllable_days", "controllable_vs_firm_pct",
                         "performance_flag", "sample_flag"]], on="case_manager", how="left")
        mt["performance_flag"] = mt["performance_flag"].map(short_flag)
        st.markdown("##### Case managers")
        st.dataframe(
            mt.rename(columns={"case_manager": "Manager", "open_cases": "Open", "RED": "🔴", "AMBER": "🟠",
                               "WATCH": "🟡", "money_m": "At stake", "flagged_money_m": "Flagged $",
                               "closed_cases": "Closed n", "median_controllable_days": "Controllable med. (d)",
                               "controllable_vs_firm_pct": "vs firm", "performance_flag": "Scorecard",
                               "sample_flag": "Sample"}),
            hide_index=True, width="stretch",
            column_config={"At stake": st.column_config.NumberColumn(format="$%.2fM"),
                           "Flagged $": st.column_config.NumberColumn(format="$%.2fM"),
                           "Controllable med. (d)": st.column_config.NumberColumn(format="%.1f"),
                           "vs firm": st.column_config.NumberColumn(format="percent")})
        st.altair_chart(chart_by_owner(view, "case_manager", "Open cases"), width="stretch")

        ct = owner_table(view, "insurance_company").merge(
            carriers_sc[["insurance_company", "closed_cases", "median_offer_wait_days", "offer_wait_flag",
                         "median_check_wait_days", "check_wait_flag", "sample_flag"]],
            on="insurance_company", how="left")
        ct["offer_wait_flag"] = ct["offer_wait_flag"].map(short_flag)
        ct["check_wait_flag"] = ct["check_wait_flag"].map(short_flag)
        st.markdown("##### Insurance carriers")
        st.dataframe(
            ct.rename(columns={"insurance_company": "Carrier", "open_cases": "Open", "RED": "🔴", "AMBER": "🟠",
                               "WATCH": "🟡", "money_m": "At stake", "flagged_money_m": "Flagged $",
                               "closed_cases": "Closed n", "median_offer_wait_days": "Offer wait med. (d)",
                               "offer_wait_flag": "Offer flag", "median_check_wait_days": "Check wait med. (d)",
                               "check_wait_flag": "Check flag", "sample_flag": "Sample"}),
            hide_index=True, width="stretch",
            column_config={"At stake": st.column_config.NumberColumn(format="$%.2fM"),
                           "Flagged $": st.column_config.NumberColumn(format="$%.2fM"),
                           "Offer wait med. (d)": st.column_config.NumberColumn(format="%.1f"),
                           "Check wait med. (d)": st.column_config.NumberColumn(format="%.1f")})
        st.altair_chart(chart_by_owner(view, "insurance_company", "Open cases"), width="stretch")

    # ── TAB 4: método ------------------------------------------------------------------------------------------------
    with tab_info:
        st.markdown(f"""
**How an alert is calculated**

Each open case sits in exactly one of the 9 phases, deduced from which milestones are still empty (NULL).
The days it has spent in that phase are compared with that phase's threshold:

| Level | Rule | Current values |
|---|---|---|
| 🟡 WATCH | days ≥ WATCH % × threshold | {watch:.0%} |
| 🟠 AMBER | days > threshold | {scale:.0%} of stored thresholds |
| 🔴 RED | days > threshold × multiplier | {red:.1f}× |

Thresholds, the multiplier and the cutoff date live in the `pi.report_params` table, not in the code.
With the what-if switch off, this page shows exactly what `pi.v_open_case_alerts` returns.

**Money at stake:** the accepted settlement when there is one (8 of the 36 open cases); otherwise an estimate from
similar closed cases (injury type), which is why those amounts are labeled *Estimated*.

**Caveats:** all data is synthetic; scorecards use few cases per owner and are not adjusted for case mix;
thresholds are initial values near the 75th percentile of closed-case phase durations. See the project README.
""")
        thr_tbl = (df.drop_duplicates("current_phase").set_index("current_phase")["threshold_used"]
                   .reindex(PHASE_KEYS).dropna().rename("Threshold (days)").reset_index()
                   .assign(Phase=lambda t: t["current_phase"].map(PHASE_NAME))[["Phase", "Threshold (days)"]])
        st.markdown("**Thresholds in use (phases with open cases)**")
        st.dataframe(thr_tbl, hide_index=True, width="content",
                     column_config={"Threshold (days)": st.column_config.NumberColumn(format="%.0f")})


main()