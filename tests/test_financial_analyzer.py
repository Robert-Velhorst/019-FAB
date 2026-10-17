import datetime

from dateutil.relativedelta import relativedelta

from src.financial_analysis.financial_analyzer import FinancialAnalyzer


def test_financial_reports_and_trends_use_standard_library_aggregation():
    analyzer = FinancialAnalyzer({})
    transactions = [
        {
            "category": "Food",
            "amount": -10,
            "extracted_data": {
                "total_amount": 10,
                "transaction_date": "2026-01-05",
                "vendor_name": "Shop A",
            },
        },
        {
            "category": "Food",
            "amount": -15,
            "extracted_data": {
                "total_amount": 15,
                "transaction_date": "2026-03-06",
                "vendor_name": "Shop A",
            },
        },
        {
            "category": "Travel",
            "amount": -5,
            "extracted_data": {
                "total_amount": 5,
                "transaction_date": "2026-03-09",
                "vendor_name": "Rail",
            },
        },
    ]

    assert analyzer.generate_expense_report(transactions)["summary"] == {"Food": 25.0, "Travel": 5.0}
    assert analyzer.generate_expense_report(transactions, "vendor_name")["summary"] == {
        "Shop A": 25.0,
        "Rail": 5.0,
    }
    assert analyzer.analyze_trend(transactions, "Food")["trend"] == {
        "2026-01": 10.0,
        "2026-03": 15.0,
    }


def test_cash_flow_forecast_averages_missing_months_and_is_json_friendly():
    analyzer = FinancialAnalyzer({})
    transactions = [
        {"extracted_data": {"total_amount": 10, "transaction_date": "2026-01-05"}},
        {"extracted_data": {"total_amount": 20, "transaction_date": "2026-03-05"}},
        {"extracted_data": {"total_amount": "bad", "transaction_date": "2026-03-06"}},
    ]

    result = analyzer.cash_flow_forecast(transactions, forecast_months=2)

    current_month = datetime.date.today().replace(day=1)
    expected = {
        (current_month + relativedelta(months=offset)).strftime("%Y-%m"): -10.0
        for offset in range(2)
    }
    assert result["forecast"] == expected
