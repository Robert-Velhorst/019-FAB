from unittest.mock import MagicMock, patch

from src.banking.banking_api import BankingAPI


def _api():
    return BankingAPI(
        {
            "banking_api_endpoint": "https://bank.example.test",
            "banking_api_access_token": "test-token",
            "banking_api_client_id": "test-client",
            "banking_api_client_secret": "test-secret",
        }
    )


def test_transaction_request_uses_bounded_timeout():
    response = MagicMock()
    response.json.return_value = {"transactions": []}

    with patch("src.banking.banking_api.requests.get", return_value=response) as get:
        assert _api().fetch_transactions(start_date="2026-01-01", end_date="2026-01-31") == []

    assert get.call_args.kwargs["timeout"] == BankingAPI.REQUEST_TIMEOUT


def test_balance_request_uses_bounded_timeout():
    response = MagicMock()
    response.json.return_value = {"balance": 12.5}

    with patch("src.banking.banking_api.requests.get", return_value=response) as get:
        assert _api().get_account_balance() == {"balance": 12.5}

    assert get.call_args.kwargs["timeout"] == BankingAPI.REQUEST_TIMEOUT
