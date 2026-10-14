# Personal Injury Settlement & Bottleneck Analytics

End-to-end analytics project modeled on the lifecycle of a plaintiff-side Personal Injury firm: **where does a case actually lose time, and how much of that delay can the firm control?**

> **All data is synthetic.** Client names, amounts, carrier timing differences and case-manager performance are generated parameters, not real firm or carrier data. No PHI/PII is used.

## Business questions

1. What share of the intake-to-closing cycle is **controllable** (firm-owned work) vs. **external** (waiting on providers and adjusters)?
2. Which cases are **stagnant** (> 45 days with no movement on records/bills or an adjuster's first offer) and need action today?
3. How much did lien negotiation **save clients**, and what does the firm **earn** in attorney fees?
4. Which case managers, carriers and injury types drive delay?

## Pipeline

```
python/01_generate_dataset.py ──> data/pi_cases.csv (200 cases)
        │
        ├─ PostgreSQL: sql/01_schema_and_load.sql ─> sql/02_metrics_view.sql (pi.v_case_metrics) ─> sql/03_kpi_queries.sql ─> Power BI
        └─ Python:     python/02_analyze.py ─> data/pi_cases_enriched.csv + reports/PI_Executive_Report.xlsx + reports/charts/*.png
```

The SQL view and the pandas code implement the **same** business rules; results were cross-checked (open/closed counts, 5 active alerts, 37.1% dollar-weighted lien reduction, $2.34M in fees).

## Quick start

```bash
pip install -r requirements.txt
python python/01_generate_dataset.py      # optional: dataset is already included
python python/02_analyze.py               # metrics + Excel report + charts

createdb pi_analytics
psql -d pi_analytics -f sql/01_schema_and_load.sql   # run from repo root (\copy uses relative path)
psql -d pi_analytics -f sql/02_metrics_view.sql
psql -d pi_analytics -f sql/03_kpi_queries.sql
```

## Data dictionary (`data/pi_cases.csv`)

| Column | Meaning |
|---|---|
| `case_id` | `PI-<intake year>-<seq>` |
| `injury_type` | Motor Vehicle Accident, Slip & Fall, Dog Bite, Pedestrian |
| `insurance_company` | Adverse liability carrier. Auto carriers for MVA/Pedestrian; homeowner/premises carriers for Dog Bite and Slip & Fall |
| `total_medical_bills` | Billed charges once complete records/bills are received |
| `settlement_amount` | Gross settlement (clusters at policy limits, as in practice) |
| `attorney_fee_pct` | 0.33 pre-suit, 0.40 once litigation is filed |
| `original_liens_amount` / `final_reduced_liens_amount` | Liens as asserted vs. as negotiated (health insurer, Medicare/Medicaid, hospital lien, MedPay, provider LOP balances) |
| `intake_date` | Signed retainer |
| `treatment_completion_date` | Client reaches MMI / discharged from treatment |
| `medical_records_received_date` | Complete records and bills in hand |
| `demand_sent_date` | Demand package sent to the adjuster |
| `first_offer_date` | Adjuster's first written offer |
| `settlement_accepted_date` | Client accepts / authorizes settlement |
| `closing_statement_date` | Settlement statement signed; disbursement |

**Open cases:** the as-of date is 2026-10-13. Milestones not yet reached are `NULL` (34 open cases), so the dataset exercises real-world NULL handling and active aging.

## Business logic

| Metric | Definition | Type |
|---|---|---|
| `treatment_days` | treatment completion − intake | Client / medical |
| `records_wait_days` | records received − treatment completion | **External** (providers) |
| `demand_prep_days` | demand sent − records received | **Controllable** (firm) |
| `offer_wait_days` | first offer − demand sent | **External** (adjuster) |
| `negotiation_days` | settlement accepted − first offer | Strategic / shared |
| `closing_days` | closing statement − settlement accepted | **Controllable** (firm: release, funds, lien resolution, disbursement) |
| `Controllable_Days` | `demand_prep_days + closing_days` | |
| `External_Wait_Days` | `records_wait_days + offer_wait_days` | |
| `intake_to_demand_days` | demand sent − intake (gross, kept for reference) | |
| `Lien_Reduction_Savings` | original liens − final liens ($); `% = savings / original` | |
| `Net_Attorney_Fee` | `settlement_amount × attorney_fee_pct` | |
| `client_net_recovery_before_costs` | settlement − fee − final liens | |
| `Stagnant_Case_Flag` | records wait **or** offer wait > 45 days (historical or ongoing) | |
| `active_stagnation_alert` | **open** case still waiting > 45 days (work these today) | |

The six phases sum exactly to the total cycle, so every day of a case is attributed to one owner.

### Design decisions worth explaining in an interview

- **Why Controllable_Days is not "intake → demand".** That window includes treatment time (driven by the client's medical course) and the records wait (providers). Counting it as firm-controllable would blame the team for delays it cannot influence, so controllable time is limited to demand preparation plus settlement-to-closing. `intake_to_demand_days` is still published for anyone who wants the gross view.
- **Stagnation is split into historical and active.** For closed cases the flag is a post-mortem of where delays happened; for open cases it is an operational alert, aged against `as_of_date`.
- **Net_Attorney_Fee** is the fee earned on the gross recovery. Case costs (filing fees, records, experts) are normally advanced by the firm and reimbursed from the client's share, so they are pass-through and not modeled. To measure firm profitability, add a `case_costs_advanced` column and decide whether the retainer computes the fee before or after costs.
- Fee tiers use 0.33 and 0.40 as stored values; real retainers often state one-third (33⅓%).
- `final_reduced_liens_amount` is only known at closing, so lien savings exist for closed cases only; some liens (Medicare, self-funded ERISA) have little room to reduce.

## Headline findings (synthetic data)

- Average cycle: **322 days**. Treatment 128 d (40%), negotiation 69 d (21%), adjuster wait 42 d (13%), settlement-to-closing 35 d (11%), records wait 32 d (10%), demand prep 17 d (5%).
- Only **16%** of the cycle is controllable; **23%** is external waiting. Inside the controllable slice, **settlement-to-closing takes twice as long as demand prep**.
- Case-manager spread: 43 controllable days (best) vs. 69 (slowest), a 26-day gap on the same kind of work.
- **44%** of cases hit a 45-day stall at some point; adjuster response accounts for roughly 7 in 10 of them.
- Lien negotiation saved clients **$491K (37.1% of $1.33M asserted)** on closed cases; cases with liens over $15K took ~11 more days to close than those under $5K.
- **5 open cases** currently breach the 45-day threshold.

## Extension ideas

Add `case_costs_advanced`, litigation-filed date, lien type, and demand amount to compute demand-to-settlement ratio; forecast fee revenue from open-case pipeline by stage; survival analysis on time-to-settlement.

## Repo layout

```
data/      pi_cases.csv, pi_cases_enriched.csv
sql/       01_schema_and_load.sql, 02_metrics_view.sql, 03_kpi_queries.sql
python/    01_generate_dataset.py, 02_analyze.py
reports/   PI_Executive_Report.xlsx, charts/
```
