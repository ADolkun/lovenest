import unittest

import tax_engine


class ContributionPlannerTest(unittest.TestCase):
    def test_midyear_actuals_drive_remaining_rate_and_take_home(self):
        result = tax_engine.plan_contributions({
            "annual_salary": 100_000,
            "pay_periods": 26,
            "remaining_paychecks": 13,
            "employee_401k_ytd": 12_000,
            "employer_401k_ytd": 3_000,
            "target_401k": 24_500,
            "current_401k_pct": 10,
            "current_roth_share_pct": 0,
            "future_roth_share_pct": 0,
            "payroll_rate_increment": 0.1,
            "plan_max_pct": 100,
            "hsa_goal": 8_000,
            "hsa_ytd": 4_000,
            "current_hsa_per_period": 350,
            "ira_goal": 7_000,
            "ira_ytd": 7_000,
            "other_pretax_per_period": 25,
            "roth_ira_per_period": 100,
            "extra_fed_withholding_per_period": 20,
        })

        self.assertEqual(result["remaining_401k"], 12_500)
        self.assertEqual(result["recommended_401k_pct"], 25)
        self.assertEqual(result["projected_401k"], 24_500)
        self.assertEqual(result["recommended_hsa_per_paycheck"], 307.69)
        self.assertEqual(result["projected_hsa_overage"], 550)
        self.assertEqual(result["ira_remaining"], 0)
        self.assertLess(result["recommended_take_home"], result["current_take_home"])

    def test_roth_401k_is_post_tax_but_still_leaves_the_paycheck(self):
        pretax = tax_engine.paycheck({
            "annual_salary": 100_000,
            "pay_periods": 26,
            "pretax_401k_pct": 10,
        })
        roth = tax_engine.paycheck({
            "annual_salary": 100_000,
            "pay_periods": 26,
            "roth_401k_pct": 10,
        })

        self.assertEqual(roth["401k_roth"], pretax["401k_pretax"])
        self.assertGreater(roth["taxable_for_fed"], pretax["taxable_for_fed"])
        self.assertLess(roth["net_take_home_per_period"], pretax["net_take_home_per_period"])

    def test_rejects_impossible_payroll_inputs(self):
        with self.assertRaisesRegex(ValueError, "remaining_paychecks"):
            tax_engine.plan_contributions({
                "annual_salary": 100_000,
                "pay_periods": 12,
                "remaining_paychecks": 13,
            })

    def test_explicit_zero_target_and_plan_max_stay_zero(self):
        result = tax_engine.plan_contributions({
            "annual_salary": 100_000,
            "pay_periods": 26,
            "remaining_paychecks": 13,
            "target_401k": 0,
            "plan_max_pct": 0,
        })

        self.assertEqual(result["target_401k"], 0)
        self.assertEqual(result["recommended_401k_pct"], 0)

    def test_plan_max_reports_unreachable_target(self):
        result = tax_engine.plan_contributions({
            "annual_salary": 50_000,
            "pay_periods": 26,
            "remaining_paychecks": 1,
            "target_401k": 24_500,
            "plan_max_pct": 10,
        })

        self.assertEqual(result["recommended_401k_pct"], 10)
        self.assertGreater(result["projected_401k_shortfall"], 0)

    def test_ytd_fica_wages_control_the_next_paycheck_caps(self):
        result = tax_engine.paycheck({
            "annual_salary": 260_000,
            "pay_periods": 26,
            "ytd_social_security_wages": 184_000,
            "ytd_medicare_wages": 199_500,
        })

        self.assertEqual(result["gross"], 10_000)
        self.assertEqual(result["social_security"], 31)
        self.assertEqual(result["medicare"], 230.5)

    def test_hsa_and_ira_goals_warn_above_age_and_coverage_limits(self):
        result = tax_engine.plan_contributions({
            "annual_salary": 100_000,
            "pay_periods": 26,
            "remaining_paychecks": 13,
            "age": 30,
            "hsa_coverage": "self",
            "hsa_goal": 5_000,
            "ira_goal": 8_000,
        })

        self.assertEqual(result["hsa_limit"], 4_400)
        self.assertEqual(result["ira_limit"], 7_500)
        self.assertTrue(any("HSA goal" in warning for warning in result["warnings"]))
        self.assertTrue(any("IRA goal" in warning for warning in result["warnings"]))


    def test_dual_ira_tracks_self_and_spouse_remaining(self):
        result = tax_engine.plan_contributions({
            "annual_salary": 147_000,
            "pay_periods": 24,
            "remaining_paychecks": 11,
            "ira_goal_self": 7_500,
            "ira_ytd_self": 7_500,
            "ira_goal_spouse": 7_500,
            "ira_ytd_spouse": 0,
        })

        self.assertEqual(result["ira_remaining_self"], 0)
        self.assertEqual(result["ira_remaining_spouse"], 7_500)
        self.assertEqual(result["ira_remaining_total"], 7_500)
        self.assertEqual(result["ira_remaining"], 7_500)
        self.assertTrue(any("spouse" in n.lower() for n in result["notes"]))

    def test_legacy_ira_keys_still_drive_ira_remaining(self):
        result = tax_engine.plan_contributions({
            "annual_salary": 100_000,
            "pay_periods": 26,
            "remaining_paychecks": 13,
            "ira_goal": 7_000,
            "ira_ytd": 2_000,
        })

        self.assertEqual(result["ira_remaining"], 5_000)
        self.assertEqual(result["ira_remaining_self"], 5_000)
        self.assertEqual(result["ira_remaining_spouse"], 0)

    def test_employer_match_below_cap_leaves_money_on_table(self):
        result = tax_engine.plan_contributions({
            "annual_salary": 147_000,
            "pay_periods": 24,
            "remaining_paychecks": 24,
            "employee_401k_ytd": 0,
            "target_401k": 4_410,
            "eligible_comp": 147_000,
            "match_rate": 0.90,
            "match_cap_pct": 5,
            "frp_pct": 3.5,
        })

        self.assertEqual(result["recommended_401k_pct"], 3)
        self.assertEqual(result["full_match_annual"], round(0.90 * 0.05 * 147_000, 2))
        self.assertEqual(result["annual_match"], round(0.90 * 0.03 * 147_000, 2))
        self.assertGreater(result["match_left_on_table"], 0)
        self.assertEqual(result["annual_frp"], round(0.035 * 147_000, 2))
        self.assertAlmostEqual(
            result["employer_total_annual"],
            result["annual_match"] + result["annual_frp"], places=2,
        )

    def test_projected_total_additions_and_headroom(self):
        result = tax_engine.plan_contributions({
            "annual_salary": 147_000,
            "pay_periods": 24,
            "remaining_paychecks": 24,
            "target_401k": 24_500,
            "eligible_comp": 147_000,
            "match_rate": 0.90,
            "match_cap_pct": 5,
            "frp_pct": 3.5,
        })

        expected = (
            min(result["projected_401k"], result["elective_limit"])
            + result["annual_match"] + result["annual_frp"]
        )
        self.assertAlmostEqual(result["projected_total_additions"], expected, places=2)
        self.assertEqual(
            result["additions_headroom"],
            round(max(72_000 - result["projected_total_additions"], 0), 2),
        )

    def test_roth_magi_phaseout_reduces_only_inside_the_band(self):
        low = tax_engine.plan_contributions({
            "annual_salary": 147_000,
            "pay_periods": 24,
            "remaining_paychecks": 11,
            "magi": 160_000,
            "ira_goal_self": 7_500,
        })
        self.assertEqual(low["roth_ira_reduced_limit"], 7_500)
        self.assertFalse(any("phase-out" in w for w in low["warnings"]))

        inside = tax_engine.plan_contributions({
            "annual_salary": 147_000,
            "pay_periods": 24,
            "remaining_paychecks": 11,
            "magi": 241_000,
            "ira_goal_self": 7_500,
        })
        self.assertLess(inside["roth_ira_reduced_limit"], 7_500)
        self.assertGreater(inside["roth_ira_reduced_limit"], 0)
        self.assertTrue(any("phase-out" in w for w in inside["warnings"]))


class RetirementProjectionTest(unittest.TestCase):
    def test_pure_accumulation_matches_compound_growth(self):
        result = tax_engine.project_retirement({
            "current_age": 30, "retire_age": 40, "end_age": 40,
            "return_rate": 7.5, "inflation_rate": 0,
            "trad_401k": 10_000, "annual_spend": 0,
        })
        self.assertAlmostEqual(result["retire_total"], 10_000 * (1.075 ** 10), places=2)

    def test_decumulation_spends_taxable_then_roth_basis_then_trad(self):
        result = tax_engine.project_retirement({
            "current_age": 40, "retire_age": 41, "end_age": 41,
            "return_rate": 0, "inflation_rate": 0,
            "taxable": 5_000, "roth_ira": 5_000, "roth_basis": 5_000,
            "trad_401k": 100_000, "annual_spend": 8_000, "effective_tax_rate": 12,
        })
        last = result["series"][-1]
        self.assertEqual(last["taxable"], 0)        # taxable drained first
        self.assertEqual(last["roth_ira"], 2_000)   # then roth basis (5k -> 2k)
        self.assertEqual(last["trad_401k"], 100_000)  # trad untouched
        self.assertEqual(last["penalty"], 0)

    def test_early_trad_only_withdrawal_incurs_penalty(self):
        result = tax_engine.project_retirement({
            "current_age": 40, "retire_age": 41, "end_age": 41,
            "return_rate": 0, "inflation_rate": 0,
            "trad_401k": 100_000, "annual_spend": 8_760, "effective_tax_rate": 12,
        })
        last = result["series"][-1]
        gross = 8_760 / (1 - 0.12 - 0.125)
        self.assertAlmostEqual(last["penalty"], gross * 0.125, places=2)
        self.assertGreater(result["total_penalties"], 0)

    def test_conversion_tranche_seasons_exactly_five_years_later(self):
        result = tax_engine.project_retirement({
            "current_age": 49, "retire_age": 50, "end_age": 55,
            "return_rate": 0, "inflation_rate": 0, "effective_tax_rate": 0,
            "trad_401k": 500_000, "annual_spend": 10_000, "annual_conversion": 10_000,
        })
        by_age = {row["age"]: row for row in result["series"]}
        self.assertGreater(by_age[54]["penalty"], 0)   # nothing seasoned yet
        self.assertEqual(by_age[55]["penalty"], 0)      # age-50 tranche now accessible

    def test_rich_but_illiquid_is_not_strong_and_suggests_taxable_bridge(self):
        result = tax_engine.project_retirement({
            "current_age": 35, "retire_age": 45, "end_age": 90,
            "return_rate": 7.5, "inflation_rate": 2.5,
            "trad_401k": 800_000, "annual_trad_401k": 30_000,
            "taxable": 5_000, "annual_taxable": 0,
            "annual_spend": 60_000, "effective_tax_rate": 12,
        })
        self.assertNotEqual(result["feasibility"], "strong")
        self.assertTrue(any("taxable" in s.lower() for s in result["suggestions"]))

    def test_swr_covered_when_four_percent_meets_spend(self):
        result = tax_engine.project_retirement({
            "current_age": 40, "retire_age": 41, "end_age": 90,
            "return_rate": 7.5, "inflation_rate": 2.5,
            "trad_401k": 2_000_000, "annual_spend": 50_000,
        })
        self.assertTrue(result["swr"]["covered"])


if __name__ == "__main__":
    unittest.main()
