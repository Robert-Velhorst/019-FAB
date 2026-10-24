import csv
import os
from typing import Any

from src.storage.database import Database
from src.utils.csv_safety import neutralize_csv_row

EXPORT_TABLE_QUERIES = {
    "documents": 'SELECT * FROM "documents"',
    "manual_review_items": 'SELECT * FROM "manual_review_items"',
    "audit_log": 'SELECT * FROM "audit_log"',
    "posting_attempts": 'SELECT * FROM "posting_attempts"',
    "vendors": 'SELECT * FROM "vendors"',
    "category_decisions": 'SELECT * FROM "category_decisions"',
    "exceptions": 'SELECT * FROM "exceptions"',
    "bank_transactions": 'SELECT * FROM "bank_transactions"',
    "reconciliation_results": 'SELECT * FROM "reconciliation_results"',
    "missing_receipt_alerts": 'SELECT * FROM "missing_receipt_alerts"',
    "outreach_reminders": 'SELECT * FROM "outreach_reminders"',
    "document_corrections": 'SELECT * FROM "document_corrections"',
}


class ReportingService:
    """Builds local FAB reports from the SQLite database."""

    def __init__(self, config: dict[str, Any]):
        self.config = config or {}
        self.database = Database(config)
        self.export_dir = self.config.get("report_export_dir", "data/reports")
        os.makedirs(self.export_dir, exist_ok=True)

    def summary(self) -> dict[str, Any]:
        return {
            "document_states": self.database.fetch_all("SELECT current_state, COUNT(*) AS count FROM documents GROUP BY current_state ORDER BY current_state"),
            "manual_review": self.database.fetch_all("SELECT status, COUNT(*) AS count FROM manual_review_items GROUP BY status ORDER BY status"),
            "posting_attempts": self.database.fetch_all("SELECT status, target_system, COUNT(*) AS count FROM posting_attempts GROUP BY status, target_system ORDER BY target_system, status"),
            "missing_receipts": self.database.fetch_all("SELECT status, COUNT(*) AS count FROM missing_receipt_alerts GROUP BY status ORDER BY status"),
            "reconciliation": self.database.fetch_all("SELECT result_type, matched, COUNT(*) AS count FROM reconciliation_results GROUP BY result_type, matched ORDER BY result_type"),
            "vendors": self.database.fetch_all("SELECT COUNT(*) AS count FROM vendors"),
            "exceptions": self.database.fetch_all("SELECT exception_type, active, COUNT(*) AS count FROM exceptions GROUP BY exception_type, active ORDER BY exception_type"),
        }

    def expense_by_vendor(self) -> list[dict[str, Any]]:
        return self.database.fetch_all(
            """
            SELECT vendor.field_value AS vendor_name,
                   SUM(CAST(amount.field_value AS REAL)) AS total_amount,
                   COUNT(DISTINCT amount.document_id) AS document_count
            FROM extracted_fields amount
            LEFT JOIN extracted_fields vendor
              ON vendor.document_id = amount.document_id
             AND vendor.field_name = 'vendor_name'
            WHERE amount.field_name = 'total_amount'
            GROUP BY vendor.field_value
            ORDER BY total_amount DESC
            """
        )

    def expense_by_category(self) -> list[dict[str, Any]]:
        return self.database.fetch_all(
            """
            SELECT cd.category AS category,
                   SUM(CAST(ef.field_value AS REAL)) AS total_amount,
                   COUNT(DISTINCT cd.document_id) AS document_count
            FROM category_decisions cd
            LEFT JOIN extracted_fields ef
              ON ef.document_id = cd.document_id
             AND ef.field_name = 'total_amount'
            GROUP BY cd.category
            ORDER BY total_amount DESC
            """
        )

    def export_table_csv(self, table_name: str) -> dict[str, Any]:
        query = EXPORT_TABLE_QUERIES.get(table_name)
        if query is None:
            return {"status": "failure", "message": f"Table is not exportable: {table_name}"}
        rows = self.database.fetch_all(query)
        output_path = os.path.join(self.export_dir, f"{table_name}.csv")
        with open(output_path, "w", newline="", encoding="utf-8") as handle:
            if rows:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(neutralize_csv_row(row) for row in rows)
            else:
                handle.write("")
        return {"status": "success", "path": output_path, "row_count": len(rows)}

    def export_all_core_csv(self) -> dict[str, Any]:
        tables = [
            "documents", "manual_review_items", "posting_attempts", "vendors",
            "category_decisions", "exceptions", "bank_transactions", "reconciliation_results",
            "missing_receipt_alerts", "outreach_reminders", "document_corrections",
        ]
        results = [self.export_table_csv(table) for table in tables]
        return {"status": "success", "exports": results}
