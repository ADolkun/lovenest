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


if __name__ == "__main__":
    unittest.main()
