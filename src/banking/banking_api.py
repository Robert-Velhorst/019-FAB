from typing import Any, Dict, List

import requests

from src.banking.bank_transaction_importer import BankTransactionImporter


class BankingAPI:
    """Fetches bank transactions from local exports first, with API fallback support."""

    REQUEST_TIMEOUT = (5, 20)

    def __init__(self, config: Dict[str, Any]):
        self.config = config or {}
        self.importer = BankTransactionImporter(self.config)
        self.mode = self.config.get(
            "banking_mode",
            "api" if self.config.get("banking_api_endpoint") else "local_import",
        )
        self.api_endpoint = self.config.get("banking_api_endpoint")
        credential_config = self.config.get("banking_api_credentials", {})
        self.client_id = self.config.get("banking_api_client_id") or credential_config.get("client_id")
        self.client_secret = self.config.get("banking_api_client_secret") or credential_config.get("client_secret")

    def _authenticate(self) -> str:
        if self.client_id and self.client_secret:
            return self.config.get("banking_api_access_token", "")
        raise ValueError("Banking API credentials not configured.")

    def fetch_transactions(self, credentials: Dict[str, Any] = None, start_date: str = None, end_date: str = None) -> List[Dict[str, Any]]:
        if self.mode == "local_import":
            transactions = self.importer.import_transactions()
        elif self.mode == "api":
            transactions = self._fetch_transactions_from_api(start_date=start_date, end_date=end_date)
        else:
            transactions = []
        return self._filter_by_date(transactions, start_date=start_date, end_date=end_date)

    def _fetch_transactions_from_api(self, start_date: str = None, end_date: str = None) -> List[Dict[str, Any]]:
        if not self.api_endpoint:
            return []
        try:
            access_token = self._authenticate()
            headers = {"Authorization": f"Bearer {access_token}"}
            params = {"start_date": start_date, "end_date": end_date}
            response = requests.get(
                f"{self.api_endpoint}/transactions",
                headers=headers,
                params=params,
                timeout=self.REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and isinstance(payload.get("transactions"), list):
                return payload["transactions"]
            if isinstance(payload, list):
                return payload
            return []
        except requests.exceptions.RequestException as exc:
            print(f"Error fetching banking transactions: {exc}")
            return []
        except ValueError as exc:
            print(f"Authentication error: {exc}")
            return []

    def get_account_balance(self, credentials: Dict[str, Any] = None) -> Dict[str, Any]:
        if self.mode != "api" or not self.api_endpoint:
            return {}
        try:
            access_token = self._authenticate()
            headers = {"Authorization": f"Bearer {access_token}"}
            response = requests.get(
                f"{self.api_endpoint}/balance",
                headers=headers,
                timeout=self.REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            print(f"Error fetching account balance: {exc}")
            return {}

    @staticmethod
    def _filter_by_date(transactions: List[Dict[str, Any]], start_date: str = None, end_date: str = None) -> List[Dict[str, Any]]:
        filtered = []
        for transaction in transactions:
            date_value = transaction.get("date") or transaction.get("transaction_date")
            if start_date and date_value and str(date_value) < str(start_date):
                continue
            if end_date and date_value and str(date_value) > str(end_date):
                continue
            filtered.append(transaction)
        return filtered


