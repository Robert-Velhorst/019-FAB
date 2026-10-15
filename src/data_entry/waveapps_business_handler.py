import csv
import os
import tempfile
from typing import Any, Dict

import requests

from src.data_entry.base import BaseDataEntryHandler
from src.data_entry.waveapps_surface import (
    build_wave_action_payload,
    build_wave_expense_import_row,
    classify_wave_destination,
    plan_wave_action,
    resolve_wave_action_for_document,
)
from src.utils.csv_safety import neutralize_csv_row
from src.data_entry.waveapps_transaction import (
    MONEY_TRANSACTION_CREATE_MUTATION,
    WAVE_GRAPHQL_URL,
    build_expense_transaction_input,
    wave_request_failure,
)


class WaveappsBusinessHandler(BaseDataEntryHandler):
    """Posts verified business expenses as balanced Wave money transactions."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.access_token = self.config.get("waveapps_business_access_token")
        self.business_id = self.config.get("waveapps_business_id")
        self.api_url = self.config.get("waveapps_api_url", WAVE_GRAPHQL_URL)
        self.headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        self.category_mapping = self.config.get("waveapps_business_category_mapping", {})
        self.category_account_ids = self.config.get("waveapps_business_category_account_ids", {})
        self.default_category_account_id = self.config.get("waveapps_business_default_category_account_id")
        self.anchor_account_id = self.config.get("waveapps_business_anchor_account_id")
        self.default_account = self.config.get("waveapps_business_default_account", "Uncategorized")
        self.timeout_seconds = _timeout_seconds(self.config.get("waveapps_request_timeout_seconds"))

    def _map_category_to_waveapps(self, category: str) -> str:
        return self.category_mapping.get(category, "Uncategorized Expense")

    def _create_expense_via_api(self, data: Dict[str, Any]) -> Dict[str, Any]:
        destination = classify_wave_destination(data)
        action_id = resolve_wave_action_for_document(data)
        wave_category = self._map_category_to_waveapps(data["category"])
        action_payload = build_wave_action_payload(data, wave_category, self.default_account)
        transaction = build_expense_transaction_input(
            data,
            business_id=self.business_id,
            anchor_account_id=self.anchor_account_id,
            category_mapping=self.category_mapping,
            category_account_ids=self.category_account_ids,
            default_category_account_id=self.default_category_account_id,
        )
        if not transaction["success"]:
            return {
                "status": "needs_review",
                "message": transaction["message"],
                "requires_manual_review": True,
                "missing_fields": transaction["missingFields"],
                "target_surface": destination["fallback_surface"],
                "action_plan": plan_wave_action(destination["target_surface"], action_id, action_payload),
            }

        rate_limit_result = self.acquire_outbound_slot("waveapps")
        if rate_limit_result:
            rate_limit_result.update({
                "target_surface": destination["target_surface"],
                "action_plan": plan_wave_action(destination["target_surface"], action_id, action_payload),
            })
            return rate_limit_result

        try:
            response = requests.post(
                self.api_url,
                headers=self.headers,
                json={"query": MONEY_TRANSACTION_CREATE_MUTATION, "variables": {"input": transaction["input"]}},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            result = response.json()
        except (requests.exceptions.RequestException, ValueError) as exc:
            return {
                **wave_request_failure(exc),
                "target_surface": destination["fallback_surface"],
                "action_plan": plan_wave_action(destination["target_surface"], action_id, action_payload),
            }

        operation = (result.get("data") or {}).get("moneyTransactionCreate") or {}
        if operation.get("didSucceed") and (operation.get("transaction") or {}).get("id"):
            transaction_id = operation["transaction"]["id"]
            return {
                "status": "success",
                "message": f"Money transaction created in Waveapps: {transaction_id}",
                "external_id": transaction_id,
                "target_surface": destination["target_surface"],
                "action_plan": plan_wave_action(destination["target_surface"], action_id, action_payload, allow_write=True),
            }
        return _failure(destination, action_id, action_payload, "Wave rejected the transaction. Review the provider response in Wave before retrying.")

    def _generate_csv_fallback(self, data: Dict[str, Any], filename: str) -> str:
        csv_dir = self.config.get("temp_dir") or tempfile.gettempdir()
        os.makedirs(csv_dir, exist_ok=True)
        csv_path = os.path.join(csv_dir, filename)
        with open(csv_path, "w", newline="") as csvfile:
            fieldnames = ["Date", "Amount", "Description", "Category", "Vendor", "Wave Surface", "Wave Action", "Wave Fallback"]
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow(neutralize_csv_row(
                build_wave_expense_import_row(data, self._map_category_to_waveapps(data["category"]))
            ))
        return csv_path

    def enter_data(self, categorized_data: Dict[str, Any]) -> Dict[str, Any]:
        if not self.access_token or not self.business_id:
            csv_filename = f"waveapps_business_import_{categorized_data['document_id']}.csv"
            csv_file_path = self._generate_csv_fallback(categorized_data, csv_filename)
            destination = classify_wave_destination(categorized_data)
            action_id = resolve_wave_action_for_document(categorized_data)
            wave_category = self._map_category_to_waveapps(categorized_data["category"])
            action_payload = build_wave_action_payload(categorized_data, wave_category, self.default_account)
            return {
                "status": "csv_generated",
                "message": f"CSV generated for manual upload: {csv_file_path}",
                "requires_manual_review": True,
                "target_surface": destination["target_surface"],
                "action_plan": plan_wave_action(destination["target_surface"], action_id, action_payload),
            }
        return self._create_expense_via_api(categorized_data)


def _failure(destination: Dict[str, Any], action_id: str, action_payload: Dict[str, Any], message: str) -> Dict[str, Any]:
    return {
        "status": "failure",
        "message": message,
        "requires_manual_review": True,
        "target_surface": destination["fallback_surface"],
        "action_plan": plan_wave_action(destination["target_surface"], action_id, action_payload),
    }


def _timeout_seconds(value: Any) -> float:
    try:
        return max(float(value), 1.0)
    except (TypeError, ValueError):
        return 30.0
