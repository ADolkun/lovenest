"""2026 tax ESTIMATOR — Married Filing Jointly, California resident.

PLANNING / TRACKING ONLY. This is not tax advice and not a filing tool. The user
files for real with FreeTaxUSA; this module exists to estimate liability and
quarterly payments so the numbers can be tracked through the year.

Stdlib only, no third-party deps.

------------------------------------------------------------------------------
HOW TO ADJUST CONSTANTS
------------------------------------------------------------------------------
Every bracket, deduction, threshold, wage base, and rate lives in the single
top-level dict ASSUMPTIONS_2026 below. Each entry is commented with what it is,
its source, and a FINAL-2026 / PROJECTION / 2025-FALLBACK tag. If a real 2026
figure later differs, change it in that one place — the engine logic does not
hard-code any number.

Tag meanings:
  FINAL 2026   — published official 2026 figure (IRS Rev. Proc. 2025-32, SSA,
                 EDD), reflects the 2025 OBBBA where relevant.
  2025 FALLBACK — California FTB had not published 2026 indexed brackets /
                 standard deduction as of mid-2026 (the 2026 Form 540-ES still
                 points filers at the 2025 table). These are the official 2025
                 values used as a floor. CA indexes to California CPI; expect
                 roughly +3.5% when finalized late 2026. VERIFY for 2026.
  PROJECTION   — a forward estimate, not official. (None are used by default;
                 the CA items use the 2025 fallback rather than guessing.)

VERIFY for 2026 applies to every constant — re-check before relying on output.
"""

from __future__ import annotations

import math


# =============================================================================
# ASSUMPTIONS_2026 — edit everything here.
# =============================================================================
# Brackets are lists of (lower_bound, rate). The bracket applies to income
# above lower_bound up to the next entry's lower_bound (top entry is open-ended).
ASSUMPTIONS_2026: dict = {
    # ---- FEDERAL: ordinary income tax, MFJ ----------------------------------
    # FINAL 2026. IRS Rev. Proc. 2025-32. OBBBA made TCJA rates permanent, so
    # rates stay 10/12/22/24/32/35/37 (no revert to pre-TCJA schedule).
    # VERIFY for 2026.
    "fed_ordinary_brackets_mfj": [
        (0.0, 0.10),
        (24_800.0, 0.12),
        (100_800.0, 0.22),
        (211_400.0, 0.24),
        (403_550.0, 0.32),
        (512_450.0, 0.35),
        (768_700.0, 0.37),
    ],

    # FINAL 2026. IRS Rev. Proc. 2025-32. Base MFJ standard deduction.
    # NOTE: OBBBA also created a temporary (2025-2028) "senior bonus" deduction
    # of up to $6,000 per taxpayer age 65+ (MAGI-phased $150k-$250k MFJ) and the
    # regular age65/blind additions ($1,650 each). Those depend on age/blindness
    # which this estimator does not model, so they are NOT applied. Add manually
    # via itemized_deductions if relevant. VERIFY for 2026.
    "fed_standard_deduction_mfj": 32_200.0,

    # ---- FEDERAL: long-term cap gains / qualified dividends, MFJ ------------
    # FINAL 2026. IRS Rev. Proc. 2025-32 Table 6. Breakpoints are TAXABLE-INCOME
    # thresholds; preferential income stacks on top of ordinary taxable income.
    # 0% up to lt_0_top; 15% up to lt_15_top; 20% above. VERIFY for 2026.
    "fed_ltcg_0_top_mfj": 98_900.0,
    "fed_ltcg_15_top_mfj": 613_700.0,
    "fed_ltcg_20_rate": 0.20,
    "fed_ltcg_15_rate": 0.15,
    "fed_ltcg_0_rate": 0.0,

    # ---- SELF-EMPLOYMENT / SOCIAL SECURITY ---------------------------------
    # FINAL 2026. SSA 2026 fact sheet. OASDI (Social Security) wage base.
    # VERIFY for 2026.
    "ss_wage_base": 184_500.0,
    "se_net_earnings_factor": 0.9235,   # 92.35% of net profit is subject to SE tax (statutory).
    "se_oasdi_rate": 0.124,             # 12.4% OASDI, capped at ss_wage_base. Statutory.
    "se_medicare_rate": 0.029,          # 2.9% Medicare, uncapped. Statutory.

    # ---- NIIT (net investment income tax) ----------------------------------
    # FINAL 2026 (statutory, NOT inflation-indexed; unchanged since 2013).
    "niit_rate": 0.038,
    "niit_magi_threshold_mfj": 250_000.0,

    # ---- Additional Medicare tax -------------------------------------------
    # FINAL 2026 (statutory, NOT indexed). Liability threshold on earned income.
    "addl_medicare_rate": 0.009,
    "addl_medicare_threshold_mfj": 250_000.0,
    # Employer withholding (used by paycheck()) triggers at $200k of wages
    # regardless of filing status; the $250k MFJ figure is reconciled on the
    # return (Form 8959). Statutory.
    "addl_medicare_withholding_threshold": 200_000.0,

    # ---- QBI (Section 199A) -------------------------------------------------
    # 20% rate is FINAL 2026 (the proposed 23% was not enacted; OBBBA made QBI
    # permanent). SIMPLIFIED: this engine applies a flat 20% of SE net profit
    # and does NOT model the W-2/UBIA limits, the SSTB phase-out, the income
    # caps (MFJ phase-in $403,500-$553,500), or the taxable-income ceiling on
    # the deduction. See notes[] in output. VERIFY for 2026.
    "qbi_rate": 0.20,

    # ---- FEDERAL estimated-tax safe harbor ----------------------------------
    # FINAL 2026 (statutory, IRC 6654).
    "fed_safe_harbor_current_pct": 0.90,    # 90% of current-year tax.
    "fed_safe_harbor_prior_pct_low": 1.00,  # 100% of prior-year if prior AGI <= 150k.
    "fed_safe_harbor_prior_pct_high": 1.10, # 110% of prior-year if prior AGI > 150k.
    "fed_safe_harbor_agi_threshold": 150_000.0,
    # Four EQUAL federal installments. FINAL 2026 (weekday-verified, no shift).
    "fed_estimate_due_dates": ["2026-04-15", "2026-06-15", "2026-09-15", "2027-01-15"],
    "fed_estimate_fractions": [0.25, 0.25, 0.25, 0.25],

    # ---- CALIFORNIA: ordinary income tax, MFJ (Schedule Y) -----------------
    # 2025 FALLBACK. Official FTB 2025 Schedule Y. FTB had not published 2026
    # indexed brackets as of mid-2026 (2026 Form 540-ES still references the 2025
    # table). CA indexes to California CPI; expect ~+3.5% for 2026. VERIFY for 2026.
    "ca_ordinary_brackets_mfj": [
        (0.0, 0.01),
        (22_158.0, 0.02),
        (52_528.0, 0.04),
        (82_904.0, 0.06),
        (115_084.0, 0.08),
        (145_448.0, 0.093),
        (742_958.0, 0.103),
        (891_542.0, 0.113),
        (1_485_906.0, 0.123),
    ],

    # 2025 FALLBACK. Official FTB 2025 MFJ standard deduction. CA did NOT conform
    # to the federal OBBBA standard-deduction increase. Expect ~+3.5% for 2026.
    # VERIFY for 2026.
    "ca_standard_deduction_mfj": 11_412.0,

    # CA Mental Health Services Tax (renamed Behavioral Health Services Tax for
    # tax years beginning 2025+; same mechanics). FINAL 2026 (structural).
    # Additional 1% on TAXABLE income over $1,000,000. $1M is NOT indexed and is
    # per-return (not doubled for MFJ).
    "ca_mental_health_rate": 0.01,
    "ca_mental_health_threshold": 1_000_000.0,

    # CA State Disability Insurance (SDI), employee rate. FINAL 2026 (EDD:
    # 1.1% 2024 -> 1.2% 2025 -> 1.3% 2026). NO wage cap since SB 951 (2024) —
    # applies to ALL wages. VERIFY for 2026.
    "ca_sdi_rate": 0.013,

    # ---- CALIFORNIA estimated-tax schedule ---------------------------------
    # FINAL 2026 (2026 Form 540-ES). Uneven 30/40/0/30 schedule. Q3 = 0%.
    "ca_estimate_due_dates": ["2026-04-15", "2026-06-15", "2026-09-15", "2027-01-15"],
    "ca_estimate_fractions": [0.30, 0.40, 0.00, 0.30],
    # CA safe harbor mirrors federal: lesser of 90% current OR 100% prior
    # (110% prior if prior-year CA AGI > 150k). (Millionaire rule — current AGI
    # >= $1M must use 90% current — is noted but not separately modeled here;
    # the engine already takes the min of current vs prior so it is conservative.)
    "ca_safe_harbor_current_pct": 0.90,
    "ca_safe_harbor_prior_pct_low": 1.00,
    "ca_safe_harbor_prior_pct_high": 1.10,
    "ca_safe_harbor_agi_threshold": 150_000.0,

    # ---- PAYCHECK helpers ---------------------------------------------------
    "paycheck_ss_rate_employee": 0.062,    # 6.2% employee OASDI, capped at ss_wage_base. Statutory.
    "paycheck_medicare_rate_employee": 0.0145,  # 1.45% employee Medicare, uncapped. Statutory.

    # ---- RETIREMENT PLAN helpers --------------------------------------------
    # FINAL 2026. IRS Notice 2025-67 / retirement-plan contribution limits.
    "elective_deferral_limit": 24_500.0,
    "catch_up_limit": 8_000.0,
    "catch_up_limit_age_60_63": 11_250.0,
    "annual_additions_limit": 72_000.0,
    "hsa_limit_self": 4_400.0,
    "hsa_limit_family": 8_750.0,
    "hsa_catch_up_limit": 1_000.0,
    "ira_limit": 7_500.0,
    "ira_catch_up_limit": 1_100.0,
    # Roth IRA MAGI phase-out, MFJ: (full below lower, $0 at/above upper).
    # 2025 FALLBACK (official 2025 range). Notice 2025-67 sets the 2026 range
    # slightly higher; this user's MAGI is far below the band so the fallback
    # does not affect output. VERIFY for 2026.
    "roth_ira_phaseout_mfj": (236_000.0, 246_000.0),
}


# =============================================================================
# Small helpers
# =============================================================================
def _g(inp: dict, key: str) -> float:
    """Read a numeric input, defaulting missing/None to 0.0."""
    v = inp.get(key, 0.0)
    return float(v) if v is not None else 0.0


def _r2(x: float) -> float:
    return round(float(x) + 0.0, 2)


def _nonnegative(inp: dict, *keys: str) -> None:
    for key in keys:
        if _g(inp, key) < 0:
            raise ValueError(f"{key} must be non-negative")


def _roth_phaseout_limit(magi: float, limit: float, lower: float, upper: float) -> float:
    """Reduced Roth IRA contribution limit for a given MAGI (IRS worksheet).

    Full limit below `lower`, $0 at/above `upper`, linear between. The reduction
    is rounded UP to the nearest $10, and a nonzero result below $200 is raised
    to $200, per the IRS phase-out rounding rule.
    """
    if upper <= lower or magi < lower:
        return limit
    if magi >= upper:
        return 0.0
    reduction = math.ceil((magi - lower) / (upper - lower) * limit / 10.0) * 10.0
    reduced = limit - reduction
    if 0.0 < reduced < 200.0:
        reduced = 200.0
    return max(reduced, 0.0)


def _tax_from_brackets(income: float, brackets: list) -> float:
    """Progressive tax on `income` given [(lower_bound, rate), ...] ascending."""
    if income <= 0:
        return 0.0
    total = 0.0
    n = len(brackets)
    for i, (lower, rate) in enumerate(brackets):
        if income <= lower:
            break
        upper = brackets[i + 1][0] if i + 1 < n else float("inf")
        taxed = min(income, upper) - lower
        if taxed > 0:
            total += taxed * rate
    return total


# =============================================================================
# Self-employment tax
# =============================================================================
def _se_tax(se_net_profit: float, a: dict) -> tuple[float, float]:
    """Return (se_tax, half_se_tax_deduction).

    OASDI (12.4%) is capped at the SS wage base; Medicare (2.9%) is uncapped.
    Both apply to 92.35% of net profit. Half of the total is an above-the-line
    deduction.
    """
    if se_net_profit <= 0:
        return 0.0, 0.0
    base = se_net_profit * a["se_net_earnings_factor"]
    oasdi = min(base, a["ss_wage_base"]) * a["se_oasdi_rate"]
    medicare = base * a["se_medicare_rate"]
    se_tax = oasdi + medicare
    return se_tax, se_tax / 2.0


# =============================================================================
# Federal preferential-rate (LTCG / qualified dividends) tax
# =============================================================================
def _ltcg_tax(ordinary_taxable: float, pref_income: float, a: dict) -> float:
    """Tax on long-term gains + qualified dividends, stacked ON TOP of ordinary
    taxable income, filling the 0% / 15% / 20% brackets.

    `ordinary_taxable` is taxable income excluding the preferential income.
    `pref_income` is the amount taxed at preferential rates.
    """
    if pref_income <= 0:
        return 0.0
    top0 = a["fed_ltcg_0_top_mfj"]
    top15 = a["fed_ltcg_15_top_mfj"]

    # Width of each preferential band still available above the ordinary stack.
    start = max(ordinary_taxable, 0.0)
    remaining = pref_income
    tax = 0.0

    band0 = max(0.0, top0 - start)
    in0 = min(remaining, band0)
    tax += in0 * a["fed_ltcg_0_rate"]
    remaining -= in0
    start += in0

    band15 = max(0.0, top15 - start)
    in15 = min(remaining, band15)
    tax += in15 * a["fed_ltcg_15_rate"]
    remaining -= in15

    # Anything left is in the 20% band.
    tax += max(0.0, remaining) * a["fed_ltcg_20_rate"]
    return tax


# =============================================================================
# Quarterly estimate splitter
# =============================================================================
def _quarterly(required_annual: float, dates: list, fractions: list) -> list:
    return [
        {"due": d, "amount": _r2(required_annual * f)}
        for d, f in zip(dates, fractions)
    ]


def _safe_harbor_annual(current_tax, prior_tax, prior_agi, a,
                        cur_key, prior_low_key, prior_high_key, agi_key):
    """Required annual payment = lesser of (current% of current tax) and
    (prior% of prior tax), where prior% is 110% if prior AGI exceeds the
    threshold else 100%. If prior-year tax is unknown (0), fall back to the
    current-year requirement.
    """
    current_req = a[cur_key] * current_tax
    if prior_tax <= 0:
        return current_req
    prior_pct = a[prior_high_key] if prior_agi > a[agi_key] else a[prior_low_key]
    prior_req = prior_pct * prior_tax
    return min(current_req, prior_req)


# =============================================================================
# Main estimator
# =============================================================================
def estimate_taxes(inp: dict) -> dict:
    a = ASSUMPTIONS_2026
    notes: list[str] = []

    # ---- Inputs -------------------------------------------------------------
    wages = _g(inp, "wages")
    fed_withholding = _g(inp, "fed_withholding")
    state_withholding = _g(inp, "state_withholding")
    se_net_profit = _g(inp, "se_net_profit")
    st_cap_gains = _g(inp, "st_cap_gains")
    lt_cap_gains = _g(inp, "lt_cap_gains")
    qualified_dividends = _g(inp, "qualified_dividends")
    ordinary_dividends = _g(inp, "ordinary_dividends")
    interest_income = _g(inp, "interest_income")
    other_ordinary_income = _g(inp, "other_ordinary_income")
    pretax_401k = _g(inp, "pretax_401k")
    direct_hsa_deduction = _g(inp, "direct_hsa_deduction")
    trad_ira_deduction = _g(inp, "trad_ira_deduction")
    itemized_deductions = _g(inp, "itemized_deductions")
    prior_year_total_tax = _g(inp, "prior_year_total_tax")
    prior_year_agi = _g(inp, "prior_year_agi")

    # W-2 Box 1 wages are already net of pretax 401(k); we therefore do NOT
    # subtract pretax_401k again from AGI (it would double-count). The field is
    # accepted for the CA add-back logic / documentation and paycheck modeling.
    notes.append(
        "AGI treats `wages` as W-2 Box 1, already reduced by pretax 401(k); "
        "`pretax_401k` is NOT subtracted again (would double-count)."
    )
    if pretax_401k > 0:
        notes.append(
            f"pretax_401k=${pretax_401k:,.0f} assumed already excluded from `wages`."
        )

    # qualified_dividends are a SUBSET of ordinary_dividends per IRS reporting
    # (1099-DIV box 1b is part of box 1a). Total dividends in income = the
    # larger of ordinary_dividends and qualified_dividends, so a caller passing
    # only qualified_dividends still gets them counted.
    total_dividends = max(ordinary_dividends, qualified_dividends)
    if qualified_dividends > ordinary_dividends:
        notes.append(
            "qualified_dividends exceeded ordinary_dividends; treated qualified "
            "as the total dividend amount (qualified is a subset of ordinary)."
        )

    # ---- Self-employment tax ------------------------------------------------
    se_tax, half_se_tax = _se_tax(se_net_profit, a)

    # ---- AGI ----------------------------------------------------------------
    total_income = (
        wages
        + se_net_profit
        + interest_income
        + total_dividends
        + st_cap_gains
        + lt_cap_gains
        + other_ordinary_income
    )
    above_line = direct_hsa_deduction + trad_ira_deduction + half_se_tax
    agi = total_income - above_line

    # MAGI: for NIIT purposes, MAGI ~= AGI for a domestic filer with no foreign
    # exclusions. We model MAGI = AGI.
    magi = agi
    notes.append("MAGI modeled as equal to AGI (no foreign-income add-backs).")

    # ---- QBI deduction (simplified) -----------------------------------------
    qbi_deduction = a["qbi_rate"] * se_net_profit if se_net_profit > 0 else 0.0
    if qbi_deduction > 0:
        notes.append(
            "QBI = flat 20% of se_net_profit. SIMPLIFIED: ignores W-2/UBIA limits, "
            "SSTB phase-out, the MFJ income phase-in ($403,500-$553,500), and the "
            "20%-of-taxable-income ceiling. May overstate QBI at high income."
        )

    # ---- Federal deduction: standard vs itemized ----------------------------
    fed_standard = a["fed_standard_deduction_mfj"]
    fed_itemized = itemized_deductions
    if fed_itemized > fed_standard:
        fed_deduction_used = fed_itemized
        chosen = "itemized"
    else:
        fed_deduction_used = fed_standard
        chosen = "standard"

    # ---- Federal taxable income split (ordinary vs preferential) -----------
    # Preferential income = qualified dividends + LT cap gains (taxed at LTCG
    # rates). Everything else ordinary. ST cap gains are ordinary.
    pref_income = qualified_dividends + max(lt_cap_gains, 0.0)

    # Taxable income after deduction + QBI. Floor at 0.
    taxable_total = max(0.0, agi - fed_deduction_used - qbi_deduction)
    # Ordinary taxable = taxable_total minus the preferential slice (but pref can
    # only be as large as taxable_total).
    pref_in_taxable = min(pref_income, taxable_total)
    federal_taxable_ordinary = max(0.0, taxable_total - pref_in_taxable)

    federal_ordinary_tax = _tax_from_brackets(
        federal_taxable_ordinary, a["fed_ordinary_brackets_mfj"]
    )
    federal_ltcg_tax = _ltcg_tax(federal_taxable_ordinary, pref_in_taxable, a)

    # ---- NIIT ---------------------------------------------------------------
    # Net investment income: interest + dividends + ST gains + LT gains.
    # (SE income is NOT investment income.)
    net_investment_income = (
        interest_income + total_dividends + st_cap_gains + max(lt_cap_gains, 0.0)
    )
    niit_base = min(
        max(net_investment_income, 0.0),
        max(magi - a["niit_magi_threshold_mfj"], 0.0),
    )
    niit = niit_base * a["niit_rate"]

    # ---- Additional Medicare tax (0.9% on earned income over threshold) -----
    # Earned income = wages + 92.35%-of-SE-profit (the Medicare-wage equivalent).
    earned_income = wages + se_net_profit * a["se_net_earnings_factor"]
    addl_medicare = (
        max(earned_income - a["addl_medicare_threshold_mfj"], 0.0)
        * a["addl_medicare_rate"]
    )

    # ---- Federal total ------------------------------------------------------
    federal_total_tax = (
        federal_ordinary_tax + federal_ltcg_tax + se_tax + niit + addl_medicare
    )
    federal_effective_rate = (federal_total_tax / agi) if agi > 0 else 0.0

    # =========================================================================
    # CALIFORNIA
    # =========================================================================
    # CA AGI: starts from federal AGI with CA-specific adjustments.
    #  - CA does NOT allow the HSA deduction -> ADD HSA back.
    #  - CA conforms to pretax 401(k) (already excluded from wages, like federal)
    #    -> no adjustment.
    #  - CA taxes LT gains and qualified dividends as ordinary income -> they are
    #    already in federal AGI at full value, so no separate add for CA.
    #  - CA does NOT allow the federal QBI deduction; but QBI was never in AGI
    #    (it is a deduction from AGI federally), so CA AGI needs no QBI add-back.
    ca_agi = agi + direct_hsa_deduction
    notes.append(
        "Direct/outside-payroll HSA contributions are deducted federally and "
        "added back for California. Payroll HSA deductions should already be "
        "excluded from W-2 Box 1 wages and must not be entered again here."
    )
    notes.append("CA taxes LT cap gains and qualified dividends as ordinary income.")

    ca_standard = a["ca_standard_deduction_mfj"]
    ca_itemized = itemized_deductions  # simplified: same itemized figure as federal.
    ca_deduction_used = ca_itemized if ca_itemized > ca_standard else ca_standard
    notes.append(
        "CA itemized deductions modeled as equal to the federal itemized figure "
        "(real CA Schedule CA adjustments not applied)."
    )

    ca_taxable = max(0.0, ca_agi - ca_deduction_used)
    ca_tax = _tax_from_brackets(ca_taxable, a["ca_ordinary_brackets_mfj"])
    ca_mental_health_tax = (
        max(ca_taxable - a["ca_mental_health_threshold"], 0.0)
        * a["ca_mental_health_rate"]
    )
    ca_total_tax = ca_tax + ca_mental_health_tax
    notes.append(
        "CA SDI (1.3% of all wages, no cap as of SB 951) is a payroll withholding, "
        "not part of income-tax liability; see paycheck() for it. Not added to ca_total_tax."
    )

    # =========================================================================
    # Totals, withholding, balance
    # =========================================================================
    total_tax = federal_total_tax + ca_total_tax
    total_withholding = fed_withholding + state_withholding
    balance_due_or_refund = total_tax - total_withholding  # positive = owe.

    # ---- Quarterly estimates ------------------------------------------------
    # Federal safe harbor on the FEDERAL liability.
    safe_harbor_annual = _safe_harbor_annual(
        federal_total_tax, prior_year_total_tax, prior_year_agi, a,
        "fed_safe_harbor_current_pct",
        "fed_safe_harbor_prior_pct_low",
        "fed_safe_harbor_prior_pct_high",
        "fed_safe_harbor_agi_threshold",
    )
    # Net required estimated payments = required annual minus what withholding
    # already covers (withholding is treated as paid evenly). Floor at 0.
    fed_est_required = max(0.0, safe_harbor_annual - fed_withholding)
    fed_quarterly = _quarterly(
        fed_est_required, a["fed_estimate_due_dates"], a["fed_estimate_fractions"]
    )

    # California estimates: 30/40/0/30 on CA required annual, net of CA withholding.
    ca_safe_harbor_annual = _safe_harbor_annual(
        ca_total_tax, 0.0, prior_year_agi, a,
        "ca_safe_harbor_current_pct",
        "ca_safe_harbor_prior_pct_low",
        "ca_safe_harbor_prior_pct_high",
        "ca_safe_harbor_agi_threshold",
    )
    # prior_year_total_tax is a combined figure in this contract; we don't have a
    # CA-only prior tax, so CA uses 90% of current-year CA tax (conservative).
    notes.append(
        "CA estimates use 90% of current-year CA tax (no CA-only prior-year tax "
        "is provided); 30/40/0/30 schedule, Q3 = 0%."
    )
    ca_est_required = max(0.0, ca_safe_harbor_annual - state_withholding)
    ca_quarterly = _quarterly(
        ca_est_required, a["ca_estimate_due_dates"], a["ca_estimate_fractions"]
    )

    notes.append(
        "Federal safe harbor = min(90% current, 110% prior) since prior AGI > "
        "$150k (110%); falls back to 90% current if no prior-year tax given."
    )
    notes.append("ESTIMATE ONLY — not tax advice. File for real via FreeTaxUSA.")

    # ---- Output -------------------------------------------------------------
    return {
        "agi": _r2(agi),
        "magi": _r2(magi),
        "federal_standard_deduction": _r2(fed_standard),
        "federal_itemized": _r2(fed_itemized),
        "federal_deduction_used": _r2(fed_deduction_used),
        "federal_taxable_ordinary": _r2(federal_taxable_ordinary),
        "federal_ordinary_tax": _r2(federal_ordinary_tax),
        "federal_ltcg_tax": _r2(federal_ltcg_tax),
        "qbi_deduction": _r2(qbi_deduction),
        "se_tax": _r2(se_tax),
        "niit": _r2(niit),
        "additional_medicare": _r2(addl_medicare),
        "federal_total_tax": _r2(federal_total_tax),
        "federal_effective_rate": _r2(federal_effective_rate),
        "ca_taxable": _r2(ca_taxable),
        "ca_tax": _r2(ca_tax),
        "ca_mental_health_tax": _r2(ca_mental_health_tax),
        "ca_total_tax": _r2(ca_total_tax),
        "total_tax": _r2(total_tax),
        "total_withholding": _r2(total_withholding),
        "balance_due_or_refund": _r2(balance_due_or_refund),
        "safe_harbor_annual": _r2(safe_harbor_annual),
        "fed_quarterly": fed_quarterly,
        "ca_quarterly": ca_quarterly,
        "std_vs_itemized": {
            "federal_standard": _r2(fed_standard),
            "federal_itemized": _r2(fed_itemized),
            "chosen": chosen,
        },
        "assumptions": ASSUMPTIONS_2026,
        "notes": notes,
    }


# =============================================================================
# Paycheck (biweekly take-home)
# =============================================================================
def paycheck(inp: dict) -> dict:
    a = ASSUMPTIONS_2026
    notes: list[str] = []

    annual_salary = _g(inp, "annual_salary")
    pay_periods_value = _g(inp, "pay_periods") if "pay_periods" in inp else 26.0
    if not pay_periods_value.is_integer():
        raise ValueError("pay_periods must be a whole number")
    pay_periods = int(pay_periods_value)
    pretax_401k_pct = _g(inp, "pretax_401k_pct")
    roth_401k_pct = _g(inp, "roth_401k_pct")
    hsa_per_period = _g(inp, "hsa_per_period")
    roth_ira_per_period = _g(inp, "roth_ira_per_period")
    other_pretax_per_period = _g(inp, "other_pretax_per_period")
    extra_fed_withholding_per_period = _g(inp, "extra_fed_withholding_per_period")
    ytd_ss_wages = _g(inp, "ytd_social_security_wages")
    ytd_medicare_wages = _g(inp, "ytd_medicare_wages")

    _nonnegative(
        inp,
        "annual_salary", "pretax_401k_pct", "roth_401k_pct", "hsa_per_period",
        "roth_ira_per_period", "other_pretax_per_period",
        "extra_fed_withholding_per_period",
        "ytd_social_security_wages", "ytd_medicare_wages",
    )
    if pay_periods <= 0:
        raise ValueError("pay_periods must be greater than zero")
    if pretax_401k_pct + roth_401k_pct > 100:
        raise ValueError("combined 401(k) percentage cannot exceed 100")

    gross = annual_salary / pay_periods
    k401_pretax = gross * (pretax_401k_pct / 100.0)
    k401_roth = gross * (roth_401k_pct / 100.0)
    hsa_pretax = hsa_per_period
    other_pretax = other_pretax_per_period

    # Pretax items reduce wages subject to federal & CA income tax (401k, HSA,
    # other pretax like FSA/insurance). Roth IRA is POST-tax — does not reduce.
    taxable_for_fed = gross - k401_pretax - hsa_pretax - other_pretax
    taxable_for_fed = max(0.0, taxable_for_fed)
    # CA: HSA does NOT reduce CA-taxable wages (no CA HSA deduction). 401k does.
    taxable_for_ca = gross - k401_pretax - other_pretax
    taxable_for_ca = max(0.0, taxable_for_ca)
    notes.append(
        "HSA reduces federal taxable wages but NOT California taxable wages "
        "(CA does not conform to the HSA deduction)."
    )
    notes.append("Roth 401(k) and Roth IRA contributions are post-tax and do not reduce taxable wages.")

    # ---- Income-tax withholding (approximation) -----------------------------
    # Annualize the per-period taxable wage, run it through the MFJ brackets,
    # subtract the MFJ standard deduction, then divide back by pay periods.
    # This approximates the percentage method without the W-4 step adjustments.
    annual_taxable_fed = taxable_for_fed * pay_periods
    fed_annual_tax = _tax_from_brackets(
        max(0.0, annual_taxable_fed - a["fed_standard_deduction_mfj"]),
        a["fed_ordinary_brackets_mfj"],
    )
    federal_income_withholding = fed_annual_tax / pay_periods + extra_fed_withholding_per_period
    notes.append(
        "Federal withholding approximated: annualized MFJ brackets on taxable "
        "wages less the MFJ standard deduction. Not the exact IRS percentage method."
    )

    annual_taxable_ca = taxable_for_ca * pay_periods
    ca_annual_tax = _tax_from_brackets(
        max(0.0, annual_taxable_ca - a["ca_standard_deduction_mfj"]),
        a["ca_ordinary_brackets_mfj"],
    )
    ca_income_withholding = ca_annual_tax / pay_periods
    notes.append("CA withholding approximated via annualized CA MFJ brackets; not the EDD DE 44 method.")

    # ---- FICA ---------------------------------------------------------------
    # Section 125 HSA/other pretax payroll deductions generally reduce FICA
    # wages; 401(k) deferrals do not. YTD wage inputs make the next-check cap
    # handling correct after salary changes or bonuses.
    fica_wages = max(gross - hsa_pretax - other_pretax, 0.0)
    if "ytd_social_security_wages" in inp:
        social_security = min(
            fica_wages,
            max(a["ss_wage_base"] - ytd_ss_wages, 0.0),
        ) * a["paycheck_ss_rate_employee"]
    else:
        social_security = (
            min(fica_wages * pay_periods, a["ss_wage_base"])
            * a["paycheck_ss_rate_employee"] / pay_periods
        )

    medicare = fica_wages * a["paycheck_medicare_rate_employee"]
    if "ytd_medicare_wages" in inp:
        before = max(ytd_medicare_wages - a["addl_medicare_withholding_threshold"], 0.0)
        after = max(ytd_medicare_wages + fica_wages - a["addl_medicare_withholding_threshold"], 0.0)
        addl_med = (after - before) * a["addl_medicare_rate"]
    else:
        addl_med = (
            max(fica_wages * pay_periods - a["addl_medicare_withholding_threshold"], 0.0)
            * a["addl_medicare_rate"] / pay_periods
        )
    medicare += addl_med
    if addl_med > 0:
        notes.append(
            "Additional Medicare 0.9% withheld on wages over $200k (employer "
            "withholding threshold, filing-status-independent); reconciled on the return."
        )

    # ---- CA SDI -------------------------------------------------------------
    # 1.3% of all wages, no cap (SB 951). Applied to gross.
    ca_sdi = gross * a["ca_sdi_rate"]

    # ---- Net ----------------------------------------------------------------
    # Roth IRA leaves the paycheck after tax (a deduction from take-home).
    deductions_total = (
        k401_pretax
        + k401_roth
        + hsa_pretax
        + other_pretax
        + federal_income_withholding
        + social_security
        + medicare
        + ca_income_withholding
        + ca_sdi
        + roth_ira_per_period
    )
    net_take_home_per_period = gross - deductions_total

    distribution = {
        "gross": _r2(gross),
        "401k_pretax": _r2(k401_pretax),
        "401k_roth": _r2(k401_roth),
        "hsa_pretax": _r2(hsa_pretax),
        "other_pretax": _r2(other_pretax),
        "federal_income_withholding": _r2(federal_income_withholding),
        "social_security": _r2(social_security),
        "medicare": _r2(medicare),
        "ca_income_withholding": _r2(ca_income_withholding),
        "ca_sdi": _r2(ca_sdi),
        "roth_ira": _r2(roth_ira_per_period),
        "net_take_home": _r2(net_take_home_per_period),
    }

    notes.append("ESTIMATE ONLY — withholding approximations differ from employer payroll output.")

    return {
        "gross": _r2(gross),
        "pay_periods": pay_periods,
        "401k_pretax": _r2(k401_pretax),
        "401k_roth": _r2(k401_roth),
        "hsa_pretax": _r2(hsa_pretax),
        "taxable_for_fed": _r2(taxable_for_fed),
        "taxable_for_ca": _r2(taxable_for_ca),
        "federal_income_withholding": _r2(federal_income_withholding),
        "social_security": _r2(social_security),
        "medicare": _r2(medicare),
        "ca_income_withholding": _r2(ca_income_withholding),
        "ca_sdi": _r2(ca_sdi),
        "roth_ira_per_period": _r2(roth_ira_per_period),
        "net_take_home_per_period": _r2(net_take_home_per_period),
        "distribution": distribution,
        "notes": notes,
    }


def plan_contributions(inp: dict) -> dict:
    """Plan the rest of 2026 from actual YTD totals and future payroll only.

    Past salary and election changes are already reflected in YTD actuals, so
    they are intentionally not reconstructed. Employer contributions remain
    separate from the employee elective-deferral limit.
    """
    a = ASSUMPTIONS_2026
    keys = (
        "annual_salary", "pay_periods", "remaining_paychecks", "age", "eligible_bonus",
        "employee_401k_ytd", "employer_401k_ytd", "after_tax_401k_ytd",
        "target_401k", "current_401k_pct", "future_roth_share_pct",
        "current_roth_share_pct", "other_pretax_per_period",
        "roth_ira_per_period", "extra_fed_withholding_per_period",
        "ytd_social_security_wages", "ytd_medicare_wages",
        "payroll_rate_increment", "plan_max_pct", "match_required_pct",
        "hsa_goal", "hsa_ytd", "current_hsa_per_period", "ira_goal", "ira_ytd",
        "ira_goal_self", "ira_ytd_self", "ira_goal_spouse", "ira_ytd_spouse",
        "eligible_comp", "match_rate", "match_cap_pct", "frp_pct", "frp_ytd",
        "years_of_service", "frp_vest_years", "magi",
    )
    _nonnegative(inp, *keys)

    salary = _g(inp, "annual_salary")
    pay_periods_value = _g(inp, "pay_periods") if "pay_periods" in inp else 26.0
    remaining_value = _g(inp, "remaining_paychecks")
    age_value = _g(inp, "age")
    if not pay_periods_value.is_integer() or not remaining_value.is_integer() or not age_value.is_integer():
        raise ValueError("pay_periods, remaining_paychecks, and age must be whole numbers")
    pay_periods = int(pay_periods_value)
    remaining_paychecks = int(remaining_value)
    age = int(age_value)
    if pay_periods <= 0:
        raise ValueError("pay_periods must be greater than zero")
    if remaining_paychecks > pay_periods:
        raise ValueError("remaining_paychecks cannot exceed pay_periods")

    current_rate = _g(inp, "current_401k_pct")
    current_roth_share = _g(inp, "current_roth_share_pct")
    roth_share = _g(inp, "future_roth_share_pct")
    increment = _g(inp, "payroll_rate_increment") if "payroll_rate_increment" in inp else 0.1
    plan_max = _g(inp, "plan_max_pct") if "plan_max_pct" in inp else 100.0
    if increment <= 0:
        raise ValueError("payroll_rate_increment must be greater than zero")
    if (
        current_rate > 100 or current_roth_share > 100 or roth_share > 100
        or plan_max > 100 or increment > 100
    ):
        raise ValueError("percentage inputs cannot exceed 100")

    catch_up = 0.0
    if 60 <= age <= 63:
        catch_up = a["catch_up_limit_age_60_63"]
    elif age >= 50:
        catch_up = a["catch_up_limit"]
    elective_limit = a["elective_deferral_limit"] + catch_up

    employee_ytd = _g(inp, "employee_401k_ytd")
    employer_ytd = _g(inp, "employer_401k_ytd")
    after_tax_ytd = _g(inp, "after_tax_401k_ytd")
    requested_target = _g(inp, "target_401k") if "target_401k" in inp else elective_limit
    target = min(requested_target, elective_limit)

    eligible_comp = _g(inp, "eligible_comp") if "eligible_comp" in inp else salary
    match_rate = _g(inp, "match_rate")
    match_cap_pct = _g(inp, "match_cap_pct")
    frp_pct = _g(inp, "frp_pct")
    frp_ytd = _g(inp, "frp_ytd")
    years_of_service = _g(inp, "years_of_service")
    frp_vest_years = _g(inp, "frp_vest_years") if "frp_vest_years" in inp else 3.0

    catch_up_used = min(max(employee_ytd - a["elective_deferral_limit"], 0.0), catch_up)
    additions_used = min(employee_ytd, a["elective_deferral_limit"]) + employer_ytd + after_tax_ytd
    if "frp_ytd" in inp or "frp_pct" in inp:
        additions_used += frp_ytd
    additions_room = max(a["annual_additions_limit"] - additions_used, 0.0)
    catch_up_room = max(catch_up - catch_up_used, 0.0)
    statutory_room = additions_room + catch_up_room
    remaining_401k = min(max(target - employee_ytd, 0.0), statutory_room)

    gross_per_paycheck = salary / pay_periods
    eligible_bonus = _g(inp, "eligible_bonus")
    remaining_gross = gross_per_paycheck * remaining_paychecks + eligible_bonus
    raw_rate = remaining_401k / remaining_gross * 100 if remaining_gross else 0.0
    recommended_rate = min(math.ceil(raw_rate / increment) * increment, plan_max) if raw_rate else 0.0

    recommended_per_paycheck = _r2(gross_per_paycheck * recommended_rate / 100.0)
    recommended_bonus = _r2(eligible_bonus * recommended_rate / 100.0)
    projected_401k_uncapped = employee_ytd + recommended_per_paycheck * remaining_paychecks + recommended_bonus
    projected_401k = min(projected_401k_uncapped, target)
    projected_401k_shortfall = max(target - projected_401k_uncapped, 0.0)
    final_pay_adjustment = max(projected_401k_uncapped - target, 0.0)

    current_per_paycheck = _r2(gross_per_paycheck * current_rate / 100.0)
    current_bonus = _r2(eligible_bonus * current_rate / 100.0)
    projected_at_current = employee_ytd + current_per_paycheck * remaining_paychecks + current_bonus

    effective_deferral_pct = min(recommended_rate, match_cap_pct)
    annual_match = match_rate * (effective_deferral_pct / 100.0) * eligible_comp
    full_match_annual = match_rate * (match_cap_pct / 100.0) * eligible_comp
    match_left_on_table = max(full_match_annual - annual_match, 0.0)
    annual_frp = (frp_pct / 100.0) * eligible_comp
    employer_total_annual = annual_match + annual_frp
    projected_total_additions = (
        min(projected_401k, elective_limit) + after_tax_ytd + annual_match + annual_frp
    )
    additions_headroom = max(a["annual_additions_limit"] - projected_total_additions, 0.0)

    hsa_goal = _g(inp, "hsa_goal")
    hsa_ytd = _g(inp, "hsa_ytd")
    hsa_coverage = str(inp.get("hsa_coverage", "family"))
    if hsa_coverage not in {"self", "family"}:
        raise ValueError("hsa_coverage must be self or family")
    hsa_limit = a[f"hsa_limit_{hsa_coverage}"]
    if age >= 55:
        hsa_limit += a["hsa_catch_up_limit"]
    hsa_remaining = max(hsa_goal - hsa_ytd, 0.0)
    hsa_per_paycheck = _r2(hsa_remaining / remaining_paychecks) if remaining_paychecks else 0.0
    current_hsa = _g(inp, "current_hsa_per_period")
    hsa_projected_current = hsa_ytd + current_hsa * remaining_paychecks
    hsa_projected_overage = max(hsa_projected_current - hsa_goal, 0.0)
    hsa_gap_to_limit = max(hsa_limit - hsa_projected_current, 0.0)
    hsa_per_paycheck_to_max = (
        _r2(max(hsa_limit - hsa_ytd, 0.0) / remaining_paychecks) if remaining_paychecks else 0.0
    )

    pretax_rate = recommended_rate * (100.0 - roth_share) / 100.0
    roth_rate = recommended_rate * roth_share / 100.0
    current_paycheck = paycheck({
        "annual_salary": salary,
        "pay_periods": pay_periods,
        "pretax_401k_pct": current_rate * (100.0 - current_roth_share) / 100.0,
        "roth_401k_pct": current_rate * current_roth_share / 100.0,
        "hsa_per_period": current_hsa,
        "other_pretax_per_period": _g(inp, "other_pretax_per_period"),
        "roth_ira_per_period": _g(inp, "roth_ira_per_period"),
        "extra_fed_withholding_per_period": _g(inp, "extra_fed_withholding_per_period"),
        "ytd_social_security_wages": _g(inp, "ytd_social_security_wages"),
        "ytd_medicare_wages": _g(inp, "ytd_medicare_wages"),
    })
    recommended_paycheck = paycheck({
        "annual_salary": salary,
        "pay_periods": pay_periods,
        "pretax_401k_pct": pretax_rate,
        "roth_401k_pct": roth_rate,
        "hsa_per_period": hsa_per_paycheck,
        "other_pretax_per_period": _g(inp, "other_pretax_per_period"),
        "roth_ira_per_period": _g(inp, "roth_ira_per_period"),
        "extra_fed_withholding_per_period": _g(inp, "extra_fed_withholding_per_period"),
        "ytd_social_security_wages": _g(inp, "ytd_social_security_wages"),
        "ytd_medicare_wages": _g(inp, "ytd_medicare_wages"),
    })

    ira_limit = a["ira_limit"] + (a["ira_catch_up_limit"] if age >= 50 else 0.0)
    if "ira_goal_self" in inp or "ira_ytd_self" in inp:
        ira_goal_self = _g(inp, "ira_goal_self")
        ira_ytd_self = _g(inp, "ira_ytd_self")
    else:
        ira_goal_self = _g(inp, "ira_goal")
        ira_ytd_self = _g(inp, "ira_ytd")
    ira_goal_spouse = _g(inp, "ira_goal_spouse")
    ira_ytd_spouse = _g(inp, "ira_ytd_spouse")
    ira_remaining_self = max(ira_goal_self - ira_ytd_self, 0.0)
    ira_remaining_spouse = max(ira_goal_spouse - ira_ytd_spouse, 0.0)
    ira_remaining_total = ira_remaining_self + ira_remaining_spouse

    roth_lower, roth_upper = a["roth_ira_phaseout_mfj"]
    magi = _g(inp, "magi") if "magi" in inp else (salary + eligible_bonus)
    roth_ira_reduced_limit = _roth_phaseout_limit(magi, ira_limit, roth_lower, roth_upper)

    notes = []
    warnings = []
    if requested_target > elective_limit:
        warnings.append(f"401(k) target capped at the 2026 employee limit of ${elective_limit:,.0f}.")
    if employee_ytd > elective_limit:
        warnings.append("Employee deferrals already exceed the modeled 2026 limit; verify all plans and correct promptly.")
    if remaining_401k and not remaining_gross:
        warnings.append("No eligible payroll remains, so the 401(k) target cannot be reached through payroll.")
    if raw_rate > plan_max:
        warnings.append("The target needs a rate above the plan maximum; the projection shows the resulting shortfall.")
    match_floor = _g(inp, "match_required_pct")
    if match_floor and recommended_rate < match_floor:
        warnings.append("The recommended rate is below the full-match threshold; check whether the plan has an annual true-up.")
    if additions_room <= 0 and remaining_401k:
        warnings.append("Employer/after-tax contributions have used the modeled annual-additions room.")
    if hsa_projected_overage:
        warnings.append(
            f"The current HSA pace exceeds the entered goal by ${hsa_projected_overage:,.2f}."
        )
    if hsa_goal > hsa_limit:
        warnings.append(
            f"The entered HSA goal exceeds the modeled 2026 {hsa_coverage}-coverage limit of ${hsa_limit:,.0f}."
        )
    if ira_goal_self > ira_limit:
        warnings.append(
            f"The entered self IRA goal exceeds the modeled 2026 per-person limit of ${ira_limit:,.0f}; income eligibility may reduce it further."
        )
    if ira_goal_spouse > ira_limit:
        warnings.append(
            f"The entered spouse IRA goal exceeds the modeled 2026 per-person limit of ${ira_limit:,.0f}; income eligibility may reduce it further."
        )
    if magi >= roth_lower:
        if roth_ira_reduced_limit <= 0:
            warnings.append(
                f"MAGI ${magi:,.0f} is at or above the ${roth_upper:,.0f} MFJ Roth IRA ceiling; "
                "direct Roth IRA contributions are not allowed (consider a backdoor Roth)."
            )
        else:
            warnings.append(
                f"MAGI ${magi:,.0f} is within the MFJ Roth IRA phase-out (${roth_lower:,.0f}-${roth_upper:,.0f}); "
                f"each spouse's Roth IRA limit is reduced to ${roth_ira_reduced_limit:,.0f}."
            )
    if ira_goal_spouse > 0:
        notes.append(
            "A non-working spouse may still fund a full Roth IRA under MFJ: the working "
            "spouse's earned income can cover both spouses' IRA contributions."
        )
    if projected_total_additions > a["annual_additions_limit"]:
        warnings.append(
            f"Projected annual additions ${projected_total_additions:,.0f} exceed the 415(c) "
            f"limit of ${a['annual_additions_limit']:,.0f}; employer + after-tax dollars leave no room."
        )
    if frp_pct > 0 and years_of_service < frp_vest_years:
        notes.append(
            f"FRP vests after {frp_vest_years:.0f} years of service; you have {years_of_service}. "
            "Employer FRP dollars are unvested until then."
        )
    if hsa_gap_to_limit > 0:
        notes.append(
            f"On pace for ${hsa_projected_current:,.0f}; ${hsa_gap_to_limit:,.0f} below the "
            f"${hsa_limit:,.0f} {hsa_coverage} max — raise to ${hsa_per_paycheck_to_max:,.2f}/paycheck to max it."
        )
    if catch_up and salary > 150_000:
        warnings.append(
            "For some higher earners, 2026 catch-up contributions must be Roth; "
            "verify the rule and your prior-year FICA wages with payroll."
        )

    return {
        "elective_limit": _r2(elective_limit),
        "annual_additions_limit": _r2(a["annual_additions_limit"]),
        "target_401k": _r2(target),
        "employee_401k_ytd": _r2(employee_ytd),
        "employer_401k_ytd": _r2(employer_ytd),
        "remaining_401k": _r2(remaining_401k),
        "gross_per_paycheck": _r2(gross_per_paycheck),
        "recommended_401k_pct": _r2(recommended_rate),
        "recommended_401k_per_paycheck": recommended_per_paycheck,
        "projected_401k": _r2(projected_401k),
        "projected_401k_at_current_rate": _r2(projected_at_current),
        "projected_401k_shortfall": _r2(projected_401k_shortfall),
        "current_rate_shortfall": _r2(max(target - projected_at_current, 0.0)),
        "final_pay_adjustment": _r2(final_pay_adjustment),
        "hsa_remaining": _r2(hsa_remaining),
        "recommended_hsa_per_paycheck": hsa_per_paycheck,
        "projected_hsa_at_current_rate": _r2(hsa_projected_current),
        "projected_hsa_overage": _r2(hsa_projected_overage),
        "hsa_gap_to_limit": _r2(hsa_gap_to_limit),
        "hsa_per_paycheck_to_max": hsa_per_paycheck_to_max,
        "hsa_limit": _r2(hsa_limit),
        "ira_remaining": _r2(ira_remaining_total),
        "ira_remaining_self": _r2(ira_remaining_self),
        "ira_remaining_spouse": _r2(ira_remaining_spouse),
        "ira_remaining_total": _r2(ira_remaining_total),
        "ira_goal_self": _r2(ira_goal_self),
        "ira_ytd_self": _r2(ira_ytd_self),
        "ira_goal_spouse": _r2(ira_goal_spouse),
        "ira_ytd_spouse": _r2(ira_ytd_spouse),
        "ira_limit": _r2(ira_limit),
        "roth_ira_magi": _r2(magi),
        "roth_ira_reduced_limit": _r2(roth_ira_reduced_limit),
        "annual_match": _r2(annual_match),
        "full_match_annual": _r2(full_match_annual),
        "match_left_on_table": _r2(match_left_on_table),
        "annual_frp": _r2(annual_frp),
        "employer_total_annual": _r2(employer_total_annual),
        "projected_total_additions": _r2(projected_total_additions),
        "additions_headroom": _r2(additions_headroom),
        "eligible_comp": _r2(eligible_comp),
        "match_rate": _r2(match_rate),
        "match_cap_pct": _r2(match_cap_pct),
        "frp_pct": _r2(frp_pct),
        "frp_ytd": _r2(frp_ytd),
        "years_of_service": _r2(years_of_service),
        "frp_vest_years": _r2(frp_vest_years),
        "current_take_home": current_paycheck["net_take_home_per_period"],
        "recommended_take_home": recommended_paycheck["net_take_home_per_period"],
        "take_home_change": _r2(
            recommended_paycheck["net_take_home_per_period"]
            - current_paycheck["net_take_home_per_period"]
        ),
        "recommended_paycheck_inputs": {
            "annual_salary": _r2(salary),
            "pay_periods": pay_periods,
            "pretax_401k_pct": _r2(pretax_rate),
            "roth_401k_pct": _r2(roth_rate),
            "hsa_per_period": hsa_per_paycheck,
            "other_pretax_per_period": _r2(_g(inp, "other_pretax_per_period")),
            "roth_ira_per_period": _r2(_g(inp, "roth_ira_per_period")),
            "extra_fed_withholding_per_period": _r2(_g(inp, "extra_fed_withholding_per_period")),
            "ytd_social_security_wages": _r2(_g(inp, "ytd_social_security_wages")),
            "ytd_medicare_wages": _r2(_g(inp, "ytd_medicare_wages")),
        },
        "warnings": warnings,
        "notes": notes,
    }


# =============================================================================
# Demo scenarios
# =============================================================================
def _print_kv(d: dict, keys: list, indent: str = "  ") -> None:
    width = max(len(k) for k in keys)
    for k in keys:
        v = d[k]
        if isinstance(v, float):
            print(f"{indent}{k:<{width}} : {v:>16,.2f}")
        else:
            print(f"{indent}{k:<{width}} : {v}")


if __name__ == "__main__":
    print("=" * 78)
    print("SCENARIO 1 — estimate_taxes (MFJ, CA)")
    print("=" * 78)
    s1_in = {
        "wages": 185_000,            # one ~120k + one ~65k W2 (Box 1, net of 401k)
        "se_net_profit": 15_000,
        "lt_cap_gains": 8_000,
        "qualified_dividends": 1_200,
        "interest_income": 300,
        "fed_withholding": 24_000,
        "state_withholding": 8_000,
        "pretax_401k": 15_000,
        "direct_hsa_deduction": 4_150,
        "prior_year_total_tax": 31_000,
        "prior_year_agi": 190_000,
    }
    print("Inputs:")
    for k, v in s1_in.items():
        print(f"  {k:<22} : {v:,}")
    r1 = estimate_taxes(s1_in)

    print("\n-- Income & deductions --")
    _print_kv(r1, [
        "agi", "magi", "federal_standard_deduction", "federal_itemized",
        "federal_deduction_used", "qbi_deduction", "federal_taxable_ordinary",
    ])
    print("\n-- Federal tax --")
    _print_kv(r1, [
        "federal_ordinary_tax", "federal_ltcg_tax", "se_tax", "niit",
        "additional_medicare", "federal_total_tax", "federal_effective_rate",
    ])
    print("\n-- California tax --")
    _print_kv(r1, ["ca_taxable", "ca_tax", "ca_mental_health_tax", "ca_total_tax"])
    print("\n-- Totals --")
    _print_kv(r1, [
        "total_tax", "total_withholding", "balance_due_or_refund",
        "safe_harbor_annual",
    ])
    print("\n-- std_vs_itemized --")
    sv = r1["std_vs_itemized"]
    print(f"  federal_standard : {sv['federal_standard']:>16,.2f}")
    print(f"  federal_itemized : {sv['federal_itemized']:>16,.2f}")
    print(f"  chosen           : {sv['chosen']}")

    print("\n-- Federal quarterly estimates (4 equal) --")
    for q in r1["fed_quarterly"]:
        print(f"  {q['due']} : {q['amount']:>12,.2f}")
    print("\n-- California quarterly estimates (30/40/0/30) --")
    for q in r1["ca_quarterly"]:
        print(f"  {q['due']} : {q['amount']:>12,.2f}")

    print("\n-- Notes --")
    for n in r1["notes"]:
        print(f"  * {n}")

    print("\n" + "=" * 78)
    print("SCENARIO 2 — paycheck (biweekly, MFJ/CA)")
    print("=" * 78)
    s2_in = {
        "annual_salary": 120_000,
        "pretax_401k_pct": 10,
        "hsa_per_period": 160,
        "roth_ira_per_period": 250,
    }
    print("Inputs:")
    for k, v in s2_in.items():
        print(f"  {k:<22} : {v:,}")
    r2 = paycheck(s2_in)

    print("\n-- Per-period (biweekly) --")
    _print_kv(r2, [
        "gross", "401k_pretax", "hsa_pretax", "taxable_for_fed", "taxable_for_ca",
        "federal_income_withholding", "social_security", "medicare",
        "ca_income_withholding", "ca_sdi", "roth_ira_per_period",
        "net_take_home_per_period",
    ])
    print(f"\n  pay_periods            : {r2['pay_periods']}")
    print("\n-- Distribution (per period) --")
    dist = r2["distribution"]
    for k in dist:
        print(f"  {k:<28} : {dist[k]:>16,.2f}")

    print("\n-- Notes --")
    for n in r2["notes"]:
        print(f"  * {n}")
