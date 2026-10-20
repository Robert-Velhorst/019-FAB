import datetime
from collections import defaultdict
from typing import Any, Dict, List

from dateutil.relativedelta import relativedelta


class FinancialAnalyzer:
    """Provides dependency-light financial summaries and trend projections."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config

    def generate_report(self, transactions: List[Dict[str, Any]]) -> Dict[str, Any]:
        amounts = [_number_or_none(item.get("amount")) or 0.0 for item in transactions]
        income = sum(amount for amount in amounts if amount > 0)
        expenses = sum(amount for amount in amounts if amount < 0)
        return {
            "total_income": income,
            "total_expenses": expenses,
            "net_cash_flow": income + expenses,
            "transaction_count": len(transactions),
        }

    def generate_expense_report(
        self,
        transactions: List[Dict[str, Any]],
        group_by: str = "category",
    ) -> Dict[str, Any]:
        if not transactions:
            return {"message": "No transactions to report.", "summary": {}}

        summary: Dict[str, float] = {}
        for transaction in transactions:
            extracted = transaction.get("extracted_data")
            extracted = extracted if isinstance(extracted, dict) else {}
            key = transaction.get(group_by) or extracted.get(group_by)
            if key is None:
                continue
            amount = _number_or_none(extracted.get("total_amount"))
            if amount is None:
                amount = _number_or_none(transaction.get("amount")) or 0.0
            summary[str(key)] = summary.get(str(key), 0.0) + amount
        return {"message": "Expense report generated successfully.", "summary": summary}

    def cash_flow_forecast(
        self,
        transactions: List[Dict[str, Any]],
        forecast_months: int = 3,
    ) -> Dict[str, Any]:
        if not transactions:
            return {"message": "No transactions for forecasting.", "forecast": {}}

        monthly_totals = _monthly_totals(transactions)
        average_monthly_expense = 0.0
        if monthly_totals:
            first_month = min(monthly_totals)
            last_month = max(monthly_totals)
            month_count = (
                (last_month.year - first_month.year) * 12
                + last_month.month - first_month.month + 1
            )
            average_monthly_expense = sum(monthly_totals.values()) / month_count

        current_month = datetime.date.today().replace(day=1)
        forecast = {
            (current_month + relativedelta(months=offset)).strftime("%Y-%m"):
                -average_monthly_expense
            for offset in range(max(0, forecast_months))
        }
        return {"message": "Cash flow forecast generated.", "forecast": forecast}

    def analyze_trend(
        self,
        transactions: List[Dict[str, Any]],
        category: str = None,
    ) -> Dict[str, Any]:
        if not transactions:
            return {"message": "No transactions for trend analysis.", "trend": {}}

        selected = [
            transaction for transaction in transactions
            if not category or transaction.get("category") == category
        ]
        trend = {
            month.strftime("%Y-%m"): amount
            for month, amount in sorted(_monthly_totals(selected).items())
        }
        return {"message": "Spending trend analyzed.", "trend": trend}


def _number_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _parse_date(value: Any) -> datetime.date | None:
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    text = str(value or "").strip()
    try:
        return datetime.date.fromisoformat(text[:10])
    except ValueError:
        return None


def _monthly_totals(transactions: List[Dict[str, Any]]) -> Dict[datetime.date, float]:
    monthly_totals: Dict[datetime.date, float] = defaultdict(float)
    for transaction in transactions:
        extracted = transaction.get("extracted_data")
        extracted = extracted if isinstance(extracted, dict) else {}
        amount = _number_or_none(extracted.get("total_amount"))
        transaction_date = _parse_date(extracted.get("transaction_date"))
        if amount is None or transaction_date is None:
            continue
        monthly_totals[transaction_date.replace(day=1)] += amount
    return dict(monthly_totals)
