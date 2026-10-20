import tempfile
import unittest
from unittest.mock import Mock

from src.reports.reporting_service import ReportingService


class TestReportingServiceSql(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = ReportingService.__new__(ReportingService)
        self.service.database = Mock()
        self.service.export_dir = self.temp_dir.name

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_export_uses_fixed_query_for_allowed_table(self):
        self.service.database.fetch_all.return_value = []

        result = self.service.export_table_csv("documents")

        self.assertEqual(result["status"], "success")
        self.service.database.fetch_all.assert_called_once_with('SELECT * FROM "documents"')

    def test_export_rejects_sql_fragment_before_database_call(self):
        result = self.service.export_table_csv('documents; DROP TABLE documents;--')

        self.assertEqual(result["status"], "failure")
        self.service.database.fetch_all.assert_not_called()


if __name__ == "__main__":
    unittest.main()
