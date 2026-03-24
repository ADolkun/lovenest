from typing import Optional

from pydantic import BaseModel


class DashboardSummary(BaseModel):
    total_balance: dict[str, float]  # currency -> amount
    total_balance_primary: float = 0.0  # consolidated in primary currency
    balance_date: str  # ISO date string, e.g. "2026-03-02"
    monthly_income: float
    monthly_expenses: float
    monthly_income_primary: float = 0.0
    monthly_expenses_primary: float = 0.0
    accounts_count: int
    pending_categorization: int
    pending_categorization_amount: float
    assets_value: dict[str, float] = {}  # currency -> total asset value
    assets_value_primary: float = 0.0
    primary_currency: str = "USD"


class SpendingByCategory(BaseModel):
    category_id: Optional[str]
    category_name: str
    category_icon: str
    category_color: str
    total: float
    percentage: float


class MonthlyTrend(BaseModel):
    month: str  # "2026-01"
    income: float
    expenses: float


class DailyBalance(BaseModel):
    day: int
    balance: Optional[float]  # None for future days beyond cutoff


class BalanceHistory(BaseModel):
    current: list[DailyBalance]
    previous: list[DailyBalance]


class ProjectedTransaction(BaseModel):
    recurring_id: str
    description: str
    amount: float
    amount_primary: Optional[float] = None
    currency: str
    type: str  # debit, credit
    date: str  # YYYY-MM-DD
    category_id: Optional[str]
    category_name: Optional[str]
    category_icon: Optional[str]
    category_color: Optional[str] = None
