"""Money back on spending — ADR 0011's spending offsets and reimbursement
links — stays out of income and comes off write-offs. Income counts what user
P&L counts; write-offs keep their own inclusion rules.

The query tests need Postgres: point TAX_TEST_DATABASE_URL at a disposable
database. Every table is TEMP, so nothing outlives the test's session.
"""
import ast
import os
import pathlib
import unittest
import uuid
from contextlib import contextmanager
from unittest import mock

import app

TEST_DB = os.environ.get("TAX_TEST_DATABASE_URL", "")
SEMANTICS = (
    pathlib.Path(__file__).resolve().parents[2]
    / "backend" / "app" / "services" / "transaction_semantics.py"
)
WS = str(uuid.uuid4())

SCHEMA = """
CREATE TEMP TABLE accounts (
    id uuid PRIMARY KEY,
    type text NOT NULL,
    is_closed boolean NOT NULL DEFAULT false
);
CREATE TEMP TABLE category_groups (id uuid PRIMARY KEY, name text NOT NULL);
CREATE TEMP TABLE categories (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    group_id uuid,
    is_income boolean NOT NULL DEFAULT false,
    is_ignored boolean NOT NULL DEFAULT false,
    treat_as_transfer boolean NOT NULL DEFAULT false
);
CREATE TEMP TABLE transactions (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    account_id uuid NOT NULL,
    category_id uuid,
    type text NOT NULL,
    amount numeric(15, 2) NOT NULL,
    amount_primary numeric(15, 2),
    date date NOT NULL,
    description text NOT NULL,
    notes text,
    status text NOT NULL DEFAULT 'posted',
    source text NOT NULL DEFAULT 'manual',
    is_ignored boolean NOT NULL DEFAULT false,
    exclude_from_pnl boolean NOT NULL DEFAULT false,
    transfer_pair_id uuid
);
CREATE TEMP TABLE transaction_reimbursements (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    reimbursement_id uuid NOT NULL,
    expense_id uuid NOT NULL,
    amount numeric(15, 2) NOT NULL CHECK (amount > 0),
    created_at timestamptz NOT NULL DEFAULT now()
);
"""


class CardPaymentMarkersTest(unittest.TestCase):
    @unittest.skipUnless(SEMANTICS.exists(), "backend source not checked out")
    def test_markers_match_the_backend(self):
        """A repayment the backend recognizes but this app does not would be
        taxed as income."""
        tree = ast.parse(SEMANTICS.read_text())
        backend = {
            node.targets[0].id: ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id.endswith("_PAYMENT_MARKERS")
        }
        self.assertEqual(backend["_CARD_CREDIT_PAYMENT_MARKERS"], app._CARD_CREDIT_PAYMENT_MARKERS)
        self.assertEqual(
            backend["_BANK_DEBIT_CARD_PAYMENT_MARKERS"], app._BANK_DEBIT_CARD_PAYMENT_MARKERS
        )


@unittest.skipUnless(TEST_DB, "set TAX_TEST_DATABASE_URL to a disposable Postgres")
class LedgerQueryTest(unittest.TestCase):
    def setUp(self):
        import psycopg

        self.conn = psycopg.connect(TEST_DB, autocommit=True)
        self.addCleanup(self.conn.close)
        self.conn.execute(SCHEMA)
        self.checking = self._row("accounts", type="checking")
        self.card = self._row("accounts", type="credit_card")
        self.business = self._row("category_groups", name="Business / Schedule C")
        self.salary = self._row("categories", name="Salary", is_income=True)
        self.dining = self._row("categories", name="Dining")
        self.software = self._row("categories", name="Software", group_id=self.business)
        self.travel = self._row("categories", name="Travel", group_id=self.business)

    def _row(self, table, **values):
        values.setdefault("id", uuid.uuid4())
        self.conn.execute(
            f"INSERT INTO {table} ({', '.join(values)}) "
            f"VALUES ({', '.join(f'%({k})s' for k in values)})",
            values,
        )
        return values["id"]

    def _txn(self, type_, amount, category=None, account=None, **extra):
        extra.setdefault("date", "2026-03-01")
        extra.setdefault("description", "TEST")
        return self._row(
            "transactions", workspace_id=WS, account_id=account or self.checking,
            category_id=category, type=type_, amount=amount, **extra,
        )

    def _link(self, reimbursement, expense, amount):
        self._row(
            "transaction_reimbursements", workspace_id=WS,
            reimbursement_id=reimbursement, expense_id=expense, amount=amount,
        )

    @contextmanager
    def _db(self):
        yield self.conn

    def _call(self, endpoint):
        with mock.patch.object(app, "DATABASE_URL", "postgresql://test"), \
             mock.patch.object(app, "HUB_WORKSPACE_ID", WS), \
             mock.patch.object(app, "db_conn", self._db), \
             mock.patch.object(app, "_ytd_end", return_value="2027-01-01"), \
             mock.patch.object(app, "_reportable_gain", return_value={}), \
             mock.patch.object(app, "_reportable_income", return_value={}):
            result = endpoint({"X-Workspace-Id": WS})
        self.assertNotIn("error", result)
        return result

    def test_income_leaves_out_credits_that_offset_spending(self):
        rewards = self._row("categories", name="Card rewards", is_income=True)
        self._txn("credit", 5000, self.salary)
        self._txn("credit", 15, rewards, self.card)
        self._txn("credit", 100)
        self._txn("credit", 40, self.dining, description="EMPLOYER REIMBURSEMENT")
        self._txn("credit", 25, None, self.card, description="AMAZON REFUND")

        self.assertEqual(self._call(app.prefill)["total_income"], 5115)

    def test_income_skips_what_user_pnl_skips(self):
        pair = uuid.uuid4()
        self._txn("credit", 300, transfer_pair_id=pair)
        self._txn("debit", 300, transfer_pair_id=pair)
        self._txn("credit", 500, None, self.card, description="AUTOPAY PAYMENT - THANK YOU")
        self._txn("credit", 70, self.salary, exclude_from_pnl=True)
        self._txn("credit", 60, self.salary, is_ignored=True)
        self._txn("credit", 1000, source="opening_balance")
        self._txn("credit", 20, source="settlement")
        self._txn("credit", 10, self.salary, status="pending")
        self._txn("credit", 80, self._row("categories", name="Moves", treat_as_transfer=True))
        self._txn("credit", 90, self._row("categories", name="Hidden", is_income=True, is_ignored=True))
        self._txn("credit", 1, self.salary)

        self.assertEqual(self._call(app.prefill)["total_income"], 1)

    def test_linked_reimbursement_leaves_income_and_shrinks_the_expense(self):
        self._txn("credit", 1000, self.salary)
        flight = self._txn("debit", 200, self.travel, date="2026-03-02")
        repaid = self._txn("credit", 120, description="ACME PAYROLL EXPENSES")
        self._link(repaid, flight, 120)
        hotel = self._txn("debit", 300, self.travel)
        over_paid = self._txn("credit", 100, description="ACME PAYROLL EXPENSES")
        self._link(over_paid, hotel, 40)

        self.assertEqual(self._call(app.prefill)["total_income"], 1060)
        writeoffs = self._call(app.writeoffs)
        self.assertEqual(writeoffs["schedule_c"][0]["category"], "Travel")
        self.assertEqual([i["amount"] for i in writeoffs["schedule_c"][0]["items"]], [80, 260])

    def test_a_link_counts_only_while_both_rows_are_in_user_pnl(self):
        """ADR 0012: a link whose other row left user P&L, or sits on a closed
        account, moves nothing, so the credit is income in full again."""
        moves = self._row("categories", name="Moves", treat_as_transfer=True)
        hidden = self._row("categories", name="Hidden", is_ignored=True)
        closed = self._row("accounts", type="checking", is_closed=True)
        expenses = [
            self._txn("debit", 10, self.dining, is_ignored=True),
            self._txn("debit", 10, self.dining, exclude_from_pnl=True),
            self._txn("debit", 10, moves),
            self._txn("debit", 10, hidden),
            self._txn("debit", 10, self.dining, description="CHASE CREDIT CRD AUTOPAY 4410"),
            self._txn("debit", 10, self.dining, source="settlement"),
            self._txn("debit", 10, self.dining, account=closed),
        ]
        for expense in expenses:
            self._link(self._txn("credit", 10, description="ACME PAYROLL EXPENSES"), expense, 10)
        counted = self._txn("debit", 10, self.dining)
        self._link(self._txn("credit", 10, description="ACME PAYROLL EXPENSES"), counted, 10)

        self.assertEqual(self._call(app.prefill)["total_income"], 70)

    def test_writeoffs_keep_the_full_expense_when_its_link_is_inert(self):
        """Both directions: a repaying credit out of user P&L, and a deductible
        expense user P&L leaves out, each leave the other row whole."""
        moves = self._row("categories", name="Moves", treat_as_transfer=True)
        flight = self._txn("debit", 200, self.travel)
        self._link(self._txn("credit", 200, is_ignored=True), flight, 200)
        fee = self._txn("debit", 40, moves, notes="#ded")
        self._link(self._txn("credit", 40, self.salary), fee, 40)

        writeoffs = self._call(app.writeoffs)
        self.assertEqual(writeoffs["totals"], {"schedule_c_total": 200, "ded_total": 40})
        self.assertEqual(self._call(app.prefill)["total_income"], 40)

    def test_writeoffs_net_refunds_and_drop_what_was_paid_back(self):
        self._txn("debit", 300, self.software, self.card, date="2026-02-01")
        self._txn("credit", 50, self.software, self.card, date="2026-02-10")
        fully_repaid = self._txn("debit", 90, self.travel)
        self._link(self._txn("credit", 90), fully_repaid, 90)
        meals = self._row("categories", name="Meals", group_id=self.business)
        self._txn("credit", 30, meals, description="REFUND FOR LAST YEAR")

        result = self._call(app.writeoffs)

        self.assertEqual(result["schedule_c"], [{
            "category": "Software", "total": 250.0, "count": 2,
            "items": [
                {"date": "2026-02-10", "description": "TEST", "amount": -50.0},
                {"date": "2026-02-01", "description": "TEST", "amount": 300.0},
            ],
        }])
        self.assertEqual(result["totals"]["schedule_c_total"], 250)

    def test_writeoffs_keep_rows_user_pnl_leaves_out(self):
        """The category or tag is the user saying a row is deductible; P&L's
        reasons for dropping a row are about reporting, not tax."""
        transfers = self._row("categories", name="Internal Transfers", treat_as_transfer=True)
        self._txn("debit", 42.50, transfers, notes="CoinLedger #ded", date="2026-04-07")
        self._txn("debit", 42.49, transfers, notes="CoinLedger #ded", date="2026-04-07")
        pair = uuid.uuid4()
        self._txn("debit", 50, self.software, transfer_pair_id=pair)
        self._txn("credit", 50, transfer_pair_id=pair)
        self._txn("debit", 100, self.software, exclude_from_pnl=True)
        self._txn("debit", 20, self.software, source="settlement")
        self._txn("debit", 999, self.software, is_ignored=True)
        self._txn("debit", 999, self.software, status="pending")

        result = self._call(app.writeoffs)

        self.assertEqual(result["totals"]["ded_total"], 84.99)
        self.assertEqual(result["totals"]["schedule_c_total"], 170)

    def test_deduction_tags_net_refunds_and_reimbursements(self):
        self._txn("debit", 60, notes="gift #ded-charity")
        self._txn("credit", 10, None, self.card, notes="partial refund #ded-charity")
        visit = self._txn("debit", 100, notes="#ded-medical")
        self._link(self._txn("credit", 100, description="INSURER"), visit, 100)

        result = self._call(app.writeoffs)

        self.assertEqual([(t["tag"], t["total"], t["count"]) for t in result["deduction_tags"]],
                         [("#ded-charity", 50.0, 2)])
        self.assertEqual(result["totals"]["ded_total"], 50)


if __name__ == "__main__":
    unittest.main()
