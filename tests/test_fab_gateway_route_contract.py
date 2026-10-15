import re
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.parse import urlsplit

from src.operations import local_api
from src.operations.local_api import create_app
from src.operations.local_ledger import LocalOperationsLedger


ROOT = Path(__file__).resolve().parents[1]
GATEWAY_SOURCE = ROOT / "web/server/fabLocalGateway.ts"


class TestFabGatewayRouteContract(unittest.TestCase):
    def test_control_center_and_operator_routes_exist_with_expected_methods(self):
        source = GATEWAY_SOURCE.read_text(encoding="utf-8")
        read_block = re.search(r"const READ_PATHS = \{(.*?)\} as const;", source, re.S)
        command_block = re.search(
            r"const COMMAND_PATHS:.*?= \{(.*?)\n\};",
            source,
            re.S,
        )
        self.assertIsNotNone(read_block, "Could not locate gateway READ_PATHS contract")
        self.assertIsNotNone(command_block, "Could not locate gateway COMMAND_PATHS contract")

        read_paths = re.findall(
            r'(?m)^\s+\w+:\s*"(/api/[^"?#]+)',
            read_block.group(1),
        )
        command_entries = re.findall(
            r'(?m)^\s{2}\w+:\s*\{\s*path:\s*"(/api/[^"?#]+)"([^\n]*)',
            command_block.group(1),
        )
        self.assertGreaterEqual(len(read_paths), 20)
        self.assertGreaterEqual(len(command_entries), 10)

        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({
                "fab_local_ledger_path": str(Path(temp_dir) / "fab.sqlite3"),
            })
            route_methods = {}
            for rule in app.url_map.iter_rules():
                if rule.rule.startswith("/api/"):
                    route_methods.setdefault(rule.rule, set()).update(rule.methods or ())

        missing_reads = sorted({
            path for path in read_paths
            if "GET" not in route_methods.get(urlsplit(path).path, set())
        })
        missing_commands = []
        for path, options in command_entries:
            method_match = re.search(r'\bmethod:\s*"(POST|DELETE)"', options)
            method = method_match.group(1) if method_match else "POST"
            if method not in route_methods.get(urlsplit(path).path, set()):
                missing_commands.append(f"{method} {path}")

        self.assertEqual(missing_reads, [], f"Gateway read routes are not registered: {missing_reads}")
        self.assertEqual(
            missing_commands,
            [],
            f"Gateway operator commands are not registered: {missing_commands}",
        )

    def test_control_center_batch_contains_every_batched_gateway_resource(self):
        source = GATEWAY_SOURCE.read_text(encoding="utf-8")
        read_block = re.search(r"const READ_PATHS = \{(.*?)\} as const;", source, re.S)
        direct_block = re.search(
            r"const CONTROL_CENTER_DIRECT_RESOURCES[^\[]*\[(.*?)\]\);",
            source,
            re.S,
        )
        self.assertIsNotNone(read_block, "Could not locate gateway READ_PATHS contract")
        self.assertIsNotNone(direct_block, "Could not locate direct control-center resources")

        read_keys = set(re.findall(r'(?m)^\s+(\w+):\s*"/api/', read_block.group(1)))
        direct_keys = set(re.findall(r'"(\w+)"', direct_block.group(1)))
        expected = read_keys - direct_keys
        self.assertGreaterEqual(len(expected), 20)

        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({
                "fab_local_ledger_path": str(Path(temp_dir) / "fab.sqlite3"),
            })
            response = app.test_client().get("/api/control-center/resources")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["version"], 1)
        resources = payload["resources"]
        self.assertEqual(set(resources), expected)
        for resource, envelope in resources.items():
            self.assertIsInstance(envelope.get("ok"), bool, resource)
            if envelope["ok"] is False:
                self.assertTrue(envelope.get("error"), resource)

    def test_failed_shared_metrics_does_not_retry_compliance_projection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({
                "fab_local_ledger_path": str(Path(temp_dir) / "fab.sqlite3"),
            })
            with (
                patch.object(
                    LocalOperationsLedger,
                    "dashboard_metrics",
                    side_effect=RuntimeError("synthetic projection failure"),
                ),
                patch.object(
                    local_api,
                    "_read_internal_json_resource",
                    wraps=local_api._read_internal_json_resource,
                ) as read_resource,
            ):
                response = app.test_client().get("/api/control-center/resources")

        self.assertEqual(response.status_code, 200)
        compliance_reads = [
            call.args[1]
            for call in read_resource.call_args_list
            if "/api/compliance/assessments" in call.args[1]
        ]
        self.assertEqual(compliance_reads, [])
        self.assertEqual(
            response.get_json()["resources"]["compliance"],
            {"ok": False, "error": "FAB resource could not be read"},
        )


if __name__ == "__main__":
    unittest.main()
