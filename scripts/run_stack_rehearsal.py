"""Exercise the built dashboard and real API with disposable, synthetic data."""

import argparse
import base64
import ctypes
import faulthandler
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import site
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SOURCE = b"Synthetic FAB receipt\nVendor: Rehearsal Shop\nDate: 2026-09-30\nTotal: EUR 4.28\n"


def require(condition, label):
    if not condition:
        raise RuntimeError(label)


def fixture_api(directory, web_port):
    from flask import request
    from src.operations.local_api import create_app
    from src.operations.local_ledger import LocalOperationsLedger
    from waitress import create_server

    root = Path(directory).resolve()
    source = root / "synthetic-receipt.txt"
    ledger_path = root / "ledger.sqlite3"
    require(root.name.startswith("fab-stack-rehearsal-")
            and (root / ".rehearsal").read_text() == "synthetic-only-v1"
            and not ledger_path.exists() and not source.exists()
            and isinstance(web_port, int) and 1 <= web_port <= 65_535,
            "Fixture API requires a fresh owned rehearsal directory")
    source.write_bytes(SOURCE)
    ledger = LocalOperationsLedger(str(ledger_path))
    document_id = ledger.register_document({
        "source": "google_drive", "sourceDocumentId": "synthetic-stack-rehearsal",
        "originalFilename": source.name, "mimeType": "text/plain", "storagePath": str(source),
        "contentSha256": hashlib.sha256(SOURCE).hexdigest(), "processingStatus": "needs_review",
        "vendorName": "Uncorrected synthetic vendor", "category": "Manual Review",
        "metadata": {
            "sourceIdentifier": "synthetic-google-drive-folder",
            "providerMetadata": {
                "folder_id": "synthetic-google-drive-folder",
                "size": len(SOURCE),
            },
        },
    })
    review_id = ledger.create_review_item({
        "documentId": document_id, "reason": "validation_failed", "details": "Synthetic correction check",
    })
    app = create_app({
        "fab_local_ledger_path": str(ledger_path),
        "fab_local_api_token": (root / "api-token").read_text(),
        "fab_hai_api_token": (root / "hai-token").read_text(),
        "fab_hai_connector_enabled": True,
        "fab_hai_allowed_commands": "refresh_notifications,record_wave_attachment_verification",
        "google_drive_wave_source_folder_id": "synthetic-google-drive-folder",
        "fab_operator_session_validation_url": f"http://127.0.0.1:{web_port}/api/fab/operator-session/status",
        "gmail_enabled": False, "google_drive_enabled": False, "freshdesk_enabled": False,
        "google_photos_enabled": False, "fab_local_intake_paths": [],
    })

    @app.before_request
    def trace_stalled_synthetic_mutation():
        if request.endpoint in {"resolve_review", "run_reconciliation"}:
            faulthandler.dump_traceback_later(7)

    @app.teardown_request
    def stop_mutation_trace(_error):
        if request.endpoint in {"resolve_review", "run_reconciliation"}:
            faulthandler.cancel_dump_traceback_later()

    server = create_server(app, host="127.0.0.1", port=0, threads=4)
    (root / "ready.json").write_text(json.dumps({
        "port": server.effective_port, "documentId": document_id, "reviewId": review_id,
    }))
    try:
        server.run()
    finally:
        server.close()
        server.task_dispatcher.shutdown()


def isolated_environment():
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "APPDATA",
               "USERPROFILE", "HOME", "LANG", "TZ"}
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def stop_owned_child(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)
    require(child.poll() is not None, "Rehearsal child did not stop")


def _read_process_resources(pid):
    """Return cumulative CPU seconds and current working-set bytes for one child."""
    if os.name == "nt":
        from ctypes import wintypes

        class _FileTime(ctypes.Structure):
            _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

        class _ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("page_fault_count", wintypes.DWORD),
                ("peak_working_set_size", ctypes.c_size_t),
                ("working_set_size", ctypes.c_size_t),
                ("quota_peak_paged_pool_usage", ctypes.c_size_t),
                ("quota_paged_pool_usage", ctypes.c_size_t),
                ("quota_peak_nonpaged_pool_usage", ctypes.c_size_t),
                ("quota_nonpaged_pool_usage", ctypes.c_size_t),
                ("pagefile_usage", ctypes.c_size_t),
                ("peak_pagefile_usage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_FileTime), ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime), ctypes.POINTER(_FileTime),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_ProcessMemoryCounters), wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x0410, False, int(pid))
        if not handle:
            raise OSError(ctypes.get_last_error(), "OpenProcess failed")
        try:
            creation, exit_time, kernel, user = (_FileTime() for _ in range(4))
            if not kernel32.GetProcessTimes(
                handle, ctypes.byref(creation), ctypes.byref(exit_time),
                ctypes.byref(kernel), ctypes.byref(user),
            ):
                raise OSError(ctypes.get_last_error(), "GetProcessTimes failed")
            counters = _ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo failed")
            cpu_ticks = (kernel.high << 32) + kernel.low + (user.high << 32) + user.low
            return {"cpuSeconds": cpu_ticks / 10_000_000, "workingSetBytes": counters.working_set_size}
        finally:
            kernel32.CloseHandle(handle)

    if os.path.isfile(f"/proc/{int(pid)}/stat"):
        with open(f"/proc/{int(pid)}/stat", encoding="ascii") as stat_file:
            stat = stat_file.read()
        fields = stat[stat.rfind(")") + 2:].split()
        ticks_per_second = os.sysconf("SC_CLK_TCK")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return {
            "cpuSeconds": (int(fields[11]) + int(fields[12])) / ticks_per_second,
            "workingSetBytes": int(fields[21]) * page_size,
        }
    raise OSError("Process resource sampling is unsupported on this platform")


class _ProcessResourceSampler:
    def __init__(self, processes, interval_seconds=0.05, reader=None):
        self.processes = dict(processes)
        self.interval_seconds = max(0.01, float(interval_seconds))
        self.reader = reader or _read_process_resources
        self.started = {}
        self.peak_working_set = {name: 0 for name in self.processes}
        self.peak_total_working_set = 0
        self.sample_count = 0
        self.unavailable = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="fab-stack-resource-sampler", daemon=True)

    def __enter__(self):
        self._sample()
        self._thread.start()
        return self

    def __exit__(self, *_exc_info):
        self._stop.set()
        self._thread.join(timeout=max(1.0, self.interval_seconds * 4))
        self._sample()

    def _run(self):
        while not self._stop.wait(self.interval_seconds):
            self._sample()

    def _sample(self):
        values = {}
        for name, pid in self.processes.items():
            try:
                values[name] = self.reader(pid)
            except (OSError, ValueError, IndexError):
                self.unavailable.add(name)
        with self._lock:
            for name, metrics in values.items():
                self.started.setdefault(name, metrics)
                self.peak_working_set[name] = max(
                    self.peak_working_set[name], int(metrics["workingSetBytes"]),
                )
            if len(values) == len(self.processes):
                self.peak_total_working_set = max(
                    self.peak_total_working_set,
                    sum(int(metrics["workingSetBytes"]) for metrics in values.values()),
                )
            self.sample_count += 1

    def result(self):
        latest = {}
        for name, pid in self.processes.items():
            try:
                latest[name] = self.reader(pid)
            except (OSError, ValueError, IndexError):
                self.unavailable.add(name)
        with self._lock:
            details = {}
            for name in self.processes:
                initial = self.started.get(name)
                final = latest.get(name)
                details[name] = {
                    "measured": initial is not None and final is not None,
                    "cpuSeconds": round(max(0.0, final["cpuSeconds"] - initial["cpuSeconds"]), 6)
                    if initial is not None and final is not None else None,
                    "baselineWorkingSetBytes": int(initial["workingSetBytes"])
                    if initial is not None else None,
                    "peakSampledWorkingSetBytes": self.peak_working_set[name] or None,
                    "finalWorkingSetBytes": int(final["workingSetBytes"])
                    if final is not None else None,
                }
            return {
                "samplingIntervalMs": round(self.interval_seconds * 1000, 1),
                "sampleCount": self.sample_count,
                "peakSampledTotalWorkingSetBytes": self.peak_total_working_set or None,
                "processes": details,
                "unavailableProcesses": sorted(self.unavailable),
            }


def wait_for_ready(child, root):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        require(child.poll() is None, "Synthetic API exited during startup")
        try:
            return json.loads((root / "ready.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.1)
    raise RuntimeError("Synthetic API startup timed out")


def rehearse(node, bundle_dir=None, benchmark_requests=0, benchmark_concurrency=8):
    import requests

    bundle_dir = Path(bundle_dir).resolve() if bundle_dir else ROOT / "web" / "dist"
    bundle = bundle_dir / "fab-standalone.js"
    preload = ROOT / "web" / "scripts" / "production-env.mjs"
    require(bundle.is_file() and preload.is_file(), "Build the production web bundles before rehearsal")
    node = node or shutil.which("node")
    require(bool(node), "Node.js is required for the stack rehearsal")
    checks = []

    def check(condition, name):
        require(condition, name)
        checks.append(name)

    with tempfile.TemporaryDirectory(prefix="fab-stack-rehearsal-") as directory:
        root = Path(directory).resolve()
        (root / ".rehearsal").write_text("synthetic-only-v1")
        tokens = {name: secrets.token_urlsafe(40) for name in ("api-token", "hai-token", "operator-token", "jwt-token")}
        for name, value in tokens.items():
            (root / name).write_text(value)
            (root / name).chmod(0o600)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            web_port = reservation.getsockname()[1]
        children = []
        session = requests.Session()
        session.trust_env = False
        try:
            with (root / "api.log").open("wb") as api_log, (root / "web.log").open("wb") as web_log:
                python_env = isolated_environment()
                python_env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "runtime_bootstrap"), str(ROOT), *site.getsitepackages()])
                api = subprocess.Popen([
                    sys._base_executable, str(Path(__file__).resolve()), "--fixture-api", str(root),
                    "--web-port", str(web_port),
                ], cwd=root, env=python_env, stdout=api_log, stderr=api_log,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                children.append(api)
                ready = wait_for_ready(api, root)
                api_base = f"http://127.0.0.1:{ready['port']}"
                web_base = f"http://127.0.0.1:{web_port}"
                web_env = isolated_environment()
                web_env.update({
                    "DOTENV_CONFIG_PATH": str(root / "absent.env"), "FAB_DEPLOYMENT_PROFILE": "local",
                    "FAB_WEB_HOST": "127.0.0.1", "PORT": str(web_port), "FAB_INSTANCE_ROOT": str(root),
                    "FAB_LOCAL_API_URL": api_base, "FAB_LOCAL_API_PUBLIC_URL": api_base,
                    "FAB_LOCAL_API_TOKEN_FILE": str(root / "api-token"),
                    "FAB_OPERATOR_ACCESS_TOKEN_FILE": str(root / "operator-token"),
                    "JWT_SECRET_FILE": str(root / "jwt-token"), "FAB_OPERATOR_LOCAL_MODE": "false",
                    "FAB_OPERATOR_PUBLIC_ORIGIN": "https://fab-rehearsal.invalid",
                    "FAB_OPERATOR_TRUSTED_PROXY_ADDRESSES": "127.0.0.1",
                })
                web = subprocess.Popen([node, "--import", preload.as_uri(), str(bundle)],
                                       cwd=root, env=web_env, stdout=web_log, stderr=web_log,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                children.append(web)

                def request(base, path, *, method="GET", headers=None, **kwargs):
                    require(urlsplit(base).hostname == "127.0.0.1" and path.startswith("/")
                            and not path.startswith("//"), "Rehearsal request escaped loopback")
                    return session.request(method, base + path, headers=headers, timeout=(3, 30),
                                           allow_redirects=False, **kwargs)

                normalized = str(root).replace("\\", "/").rstrip("/")
                if os.name == "nt":
                    normalized = normalized.lower()
                identity_hash = hashlib.sha256(normalized.encode()).hexdigest()
                deadline = time.monotonic() + 45
                while True:
                    require(web.poll() is None, "Production dashboard exited during startup")
                    try:
                        identity = request(web_base, "/api/fab/runtime")
                        if identity.status_code == 200:
                            check(identity.json().get("instanceId") == identity_hash, "owned_dashboard_identity")
                            break
                    except requests.RequestException:
                        pass
                    require(time.monotonic() < deadline, "Production dashboard startup timed out")
                    time.sleep(0.1)

                check(request(api_base, "/api/live").status_code == 401, "api_anonymous_denied")
                hai_headers = {"Authorization": "Bearer " + tokens["hai-token"]}
                check(request(api_base, "/api/hai/manifest", headers=hai_headers).status_code == 200, "hai_manifest_allowed")
                check(request(api_base, "/api/health", headers=hai_headers).status_code == 403, "hai_operator_scope_denied")
                for name, payload in (("boolean_limit", {"commandId": "run_reconciliation",
                    "requestId": "synthetic-invalid-hai-limit", "payload": {"limit": True}}),
                    ("boolean_request_id", {"commandId": "refresh_notifications", "requestId": True})):
                    rejected_command = request(api_base, "/api/hai/commands/execute", method="POST",
                                               headers=hai_headers, json=payload)
                    check(rejected_command.status_code == 400
                          and rejected_command.json().get("status") in {"invalid", "invalid_request"},
                          "hai_invalid_command_contract_denied_" + name)
                oversized_hai = request(api_base, "/api/hai/commands/plan", method="POST",
                    headers={**hai_headers, "Content-Type": "application/json"},
                    data='{"commandId":"refresh_notifications","padding":"' + 'x' * (2 * 1024 * 1024) + '"}')
                check(oversized_hai.status_code == 413, "hai_oversize_envelope_denied")
                proxy = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "fab-rehearsal.invalid",
                         "Origin": "https://fab-rehearsal.invalid"}
                check(request(web_base, "/api/trpc/fab.access", headers=proxy).status_code == 403, "dashboard_anonymous_denied")
                wrong_origin = {**proxy, "Origin": "https://other-rehearsal.invalid"}
                check(request(web_base, "/operator/login", method="POST", headers=wrong_origin,
                              data={"accessToken": tokens["operator-token"]}).status_code == 403, "login_cross_origin_denied")
                login = request(web_base, "/operator/login", method="POST", headers=proxy,
                                data={"accessToken": tokens["operator-token"]})
                check(login.status_code == 303, "managed_operator_login")
                cookie = login.headers.get("Set-Cookie", "").split(";", 1)[0]
                check(cookie.startswith("__Host-fab_operator="), "managed_cookie_issued")
                browser = {**proxy, "Cookie": cookie}

                def rpc(name, payload=None, mutation=False, headers=browser):
                    response = request(web_base, "/api/trpc/fab." + name,
                                       method="POST" if mutation else "GET", headers=headers,
                                       **({"json": {"json": payload}} if mutation else {}))
                    if response.status_code != 200:
                        detail = ""
                        try:
                            detail = str(response.json().get("error", {}).get("json", {}).get("message", ""))
                        except (ValueError, AttributeError):
                            pass
                        for token in tokens.values():
                            detail = detail.replace(token, "[redacted]")
                        detail = detail.replace(str(root), "[rehearsal]")[:500]
                        raise RuntimeError(f"Dashboard RPC failed: {name} (HTTP {response.status_code}): {detail}")
                    return response.json()["result"]["data"]["json"]

                center = rpc("controlCenter")
                connected_to_ledger = (
                    center["connection"]["connected"] and center["metrics"]["documents"] == 1
                )
                ledger_detail = json.dumps({
                    "connected": center["connection"]["connected"],
                    "documents": center["metrics"]["documents"],
                    "liveness": center["resourceStates"]["liveness"],
                    "metrics": center["resourceStates"]["metrics"],
                }, sort_keys=True)
                check(
                    connected_to_ledger,
                    "dashboard_reads_real_ledger" if connected_to_ledger
                    else f"dashboard_reads_real_ledger: {ledger_detail}",
                )
                check(center["resourceStates"]["metrics"]["state"] == "live", "ledger_metrics_are_live")
                check(center["reviews"]["summary"]["reviewItems"] == 1, "synthetic_review_is_visible")
                source_path = f"/api/fab/source/{ready['documentId']}"
                source = request(web_base, source_path, headers=browser)
                check(source.status_code == 200 and source.content == SOURCE
                      and source.headers.get("X-FAB-Source-SHA256") == hashlib.sha256(SOURCE).hexdigest(),
                      "gateway_verified_source_bytes")
                (root / "synthetic-receipt.txt").write_bytes(SOURCE + b"tampered")
                check(request(web_base, source_path, headers=browser).status_code == 409, "tampered_source_denied")
                (root / "synthetic-receipt.txt").write_bytes(SOURCE)
                rpc("resolveReview", {"reviewItemId": ready["reviewId"], "status": "resolved",
                                      "resolution": "Synthetic stack rehearsal correction",
                                      "corrections": {"vendorName": "Corrected Rehearsal Shop",
                                                      "category": "Office Supplies", "transactionDate": "2026-09-30",
                                                      "totalAmount": 4.28},
                                      "learnRule": False}, mutation=True)
                from src.operations.local_ledger import LocalOperationsLedger
                ledger = LocalOperationsLedger(str(root / "ledger.sqlite3"))
                command_path = "/api/hai/commands/execute"
                command_body = {"commandId": "refresh_notifications", "requestId": "synthetic-hai-actor",
                                "actor": "fab_dashboard:admin:impersonated"}
                attributed = request(api_base, command_path, method="POST", headers=hai_headers, json=command_body)
                attributed_event = ledger.find_audit_event("hai.command.completed", "hai_command_request",
                                                          "synthetic-hai-actor")
                requested_event = ledger.find_audit_event("hai.command.requested", "hai_command_request",
                                                         "synthetic-hai-actor")
                check(attributed.status_code == 200 and attributed.json().get("status") == "completed"
                      and attributed_event and attributed_event["details"].get("actor") == "fab_hai_api:hai"
                      and requested_event and requested_event["details"].get("actor") == "fab_hai_api:hai",
                      "hai_command_actor_bound_to_verified_credential")
                command_body["actor"] = "fab_hai_api:operator"
                replayed = request(api_base, command_path, method="POST", headers=hai_headers, json=command_body)
                check(replayed.status_code == 200 and replayed.json().get("status") == "already_executed"
                      and replayed.json().get("auditEventId") == attributed_event["id"],
                      "hai_command_actor_spoof_replay_preserves_original_audit")
                blocked_payload = {
                    "commandId": "record_wave_attachment_verification",
                    "requestId": "synthetic-hai-blocked-attachment",
                    "payload": {"documentId": ready["documentId"], "evidence": {}},
                }
                blocked = request(api_base, command_path, method="POST", headers=hai_headers,
                                  json=blocked_payload)
                failed_event = ledger.find_audit_event(
                    "hai.command.failed", "hai_command_request",
                    "synthetic-hai-blocked-attachment",
                )
                check(blocked.status_code == 409 and blocked.json().get("status") == "blocked"
                      and blocked.json().get("result", {}).get("status") == "blocked"
                      and blocked.json().get("externalSubmission") == "not_executed"
                      and failed_event and failed_event["details"].get("externalSubmission") == "not_executed",
                      "hai_structured_attachment_block_preserved_and_audited")
                blocked_replay = request(api_base, command_path, method="POST", headers=hai_headers,
                                         json=blocked_payload)
                request_events = [
                    event for event in ledger.list_audit_events(limit=1000)
                    if event.get("entity_id") == "synthetic-hai-blocked-attachment"
                    and event.get("entity_type") == "hai_command_request"
                    and event.get("action") in {"hai.command.requested", "hai.command.failed"}
                ]
                check(blocked_replay.status_code == 409
                      and blocked_replay.json().get("status") == "previously_failed"
                      and blocked_replay.json().get("auditEventId") == failed_event["id"]
                      and len(request_events) == 2,
                      "hai_blocked_command_replay_does_not_retry_executor")
                check(ledger.get_document(ready["documentId"])["vendor_name"] == "Corrected Rehearsal Shop",
                      "dashboard_mutation_persisted_in_sqlite")
                check(ledger.get_review_item(ready["reviewId"])["status"] == "resolved", "review_resolution_persisted")
                check(rpc("controlCenter")["reviews"]["summary"]["reviewItems"] == 0,
                      "dashboard_refresh_after_mutation")

                statement = json.dumps([{
                    "id": "synthetic-rehearsal-bank", "date": "2026-09-30",
                    "amount": -4.28, "description": "Corrected Rehearsal Shop",
                }]).encode()
                imported = rpc("importBankStatement", {
                    "filename": "synthetic-statement.json", "format": "json",
                    "accountIdentifier": "synthetic-rehearsal-account",
                    "contentBase64": base64.b64encode(statement).decode("ascii"),
                }, mutation=True)
                bank_transactions = ledger.list_bank_transactions()
                check(imported["rowsImported"] == 1 and len(bank_transactions) == 1,
                      "dashboard_imports_synthetic_bank_statement")
                rejected_import = request(web_base, "/api/trpc/fab.importBankStatement", method="POST", headers=browser,
                    json={"json": {"filename": "synthetic-invalid.json", "format": "json",
                                   "accountIdentifier": "synthetic-rehearsal-account",
                                   "contentBase64": base64.b64encode(b"not-json").decode("ascii")}})
                rejected_error = rejected_import.json().get("error", {}).get("json", {})
                check(rejected_import.status_code == 400
                      and rejected_error.get("data", {}).get("code") == "BAD_REQUEST"
                      and ledger.list_bank_transactions() == bank_transactions
                      and not ledger.list_reconciliation_matches(),
                      "dashboard_preserves_backend_import_rejection_without_financial_changes")
                for invalid_payload in ({"bankTransactions": None}, {"bankTransactions": [{
                    "id": "synthetic-invalid", "date": "2026-09-30", "amount": float("nan"),
                }]}):
                    invalid_run = request(api_base, "/api/reconciliation/run", method="POST",
                                          headers={"Authorization": "Bearer " + tokens["api-token"],
                                                   "Content-Type": "application/json"},
                                          data=json.dumps(invalid_payload))
                    check(invalid_run.status_code == 400 and not ledger.list_reconciliation_matches()
                          and not ledger.list_review_items(status="pending"),
                          "invalid_reconciliation_payload_denied_without_financial_writes_"
                          + ("null" if invalid_payload["bankTransactions"] is None else "nonfinite"))
                reconciliation = rpc("runCommand", {"commandId": "run_reconciliation"}, mutation=True)
                check(reconciliation["matchedCandidates"] == 1 and reconciliation["reviewItemsCreated"] == 1,
                      "dashboard_records_reconciliation_candidate")
                match_id = reconciliation["results"][0]["reconciliationMatchId"]
                reviews = ledger.list_review_items(status="pending", document_id=ready["documentId"])
                check(len(reviews) == 1 and reviews[0]["corrected_data"]["reconciliationMatchId"] == match_id,
                      "reconciliation_review_is_linked")
                bank_id = bank_transactions[0]["id"]
                ledger.update_bank_transaction(bank_id, {"amount": -99})
                stale = request(web_base, "/api/trpc/fab.resolveReview", method="POST", headers=browser,
                                json={"json": {"reviewItemId": reviews[0]["id"], "status": "approved",
                                               "resolution": "Synthetic stale approval", "learnRule": False}})
                check(stale.status_code == 409
                      and "Reconciliation evidence changed" in stale.json()["error"]["json"]["message"],
                      "dashboard_reports_stale_approval_error")
                check(ledger.get_reconciliation_match(match_id)["status"] == "candidate"
                      and ledger.get_review_item(reviews[0]["id"])["status"] == "pending"
                      and ledger.get_bank_transaction(bank_id)["amount"] == -99,
                      "stale_dashboard_approval_preserves_evidence_and_review")
                ledger.update_bank_transaction(bank_id, {"amount": -4.28})
                rpc("resolveReview", {"reviewItemId": reviews[0]["id"], "status": "approved",
                                      "resolution": "Synthetic bank reconciliation approval",
                                      "learnRule": False}, mutation=True)
                check(ledger.get_reconciliation_match(match_id)["status"] == "approved",
                      "dashboard_reconciliation_approval_persisted")
                check(ledger.get_document(ready["documentId"])["reconciliation_status"] == "reconciled",
                      "reconciled_document_persisted")
                bank_id = bank_transactions[0]["id"]
                check(ledger.get_bank_transaction(bank_id)["reconciliation_status"] == "reconciled",
                      "reconciled_bank_transaction_persisted")
                record = ledger.get_bookkeeping_record_by_document(ready["documentId"])
                check(record["reconciliation_status"] == "reconciled" and not record["review_required"],
                      "normalized_reconciliation_gate_refreshed")
                check(rpc("controlCenter")["reviews"]["summary"]["reviewItems"] == 0,
                      "dashboard_refresh_after_reconciliation")

                missing_statement = json.dumps([{
                    "id": "synthetic-rehearsal-missing", "date": "2026-09-30",
                    "amount": -12.5, "description": "Synthetic Missing Shop",
                }]).encode()
                missing_import = rpc("importBankStatement", {
                    "filename": "synthetic-missing-statement.json", "format": "json",
                    "accountIdentifier": "synthetic-rehearsal-account",
                    "contentBase64": base64.b64encode(missing_statement).decode("ascii"),
                }, mutation=True)
                check(missing_import["rowsImported"] == 1, "dashboard_imports_missing_receipt_transaction")
                missing_run = rpc("runCommand", {"commandId": "run_reconciliation"}, mutation=True)
                check(missing_run["candidateDocuments"] == 0 and missing_run["missingReceipts"] == 1,
                      "completed_document_excluded_from_later_run")
                missing_match = missing_run["results"][0]["reconciliationMatchId"]
                operator_api = {"Authorization": "Bearer " + tokens["api-token"]}
                missing_path = f"/api/reconciliation/{missing_match}/resolve"
                rejected = request(api_base, missing_path, method="POST", headers=operator_api,
                                   json={"status": "approved"})
                check(rejected.status_code == 400 and ledger.get_reconciliation_match(missing_match)["status"] == "missing_receipt",
                      "confirmation_without_document_denied")
                missing_reviews = ledger.list_missing_receipt_review_items()
                require(len(missing_reviews) == 1, "Expected one owned synthetic missing receipt review")
                missing_bank = next(row["id"] for row in ledger.list_bank_transactions()
                                    if row["transaction_id"] == "synthetic-rehearsal-missing")
                ledger.update_bank_transaction(missing_bank, {"amount": -99})
                stale_disposition = request(web_base, "/api/trpc/fab.resolveReview", method="POST", headers=browser,
                                            json={"json": {"reviewItemId": missing_reviews[0]["id"], "status": "ignored",
                                                           "resolution": "Synthetic stale disposition", "learnRule": False}})
                check(stale_disposition.status_code == 409,
                      "dashboard_denies_stale_missing_receipt_disposition")
                check(ledger.get_review_item(missing_reviews[0]["id"])["status"] == "pending"
                      and ledger.get_bank_transaction(missing_bank)["amount"] == -99,
                      "stale_missing_receipt_disposition_preserves_open_gate")
                ledger.update_bank_transaction(missing_bank, {"amount": -12.5})
                rpc("resolveReview", {"reviewItemId": missing_reviews[0]["id"], "status": "ignored",
                                      "resolution": "Synthetic missing receipt disposition", "learnRule": False}, mutation=True)
                check(ledger.get_review_item(missing_reviews[0]["id"])["status"] == "ignored",
                      "dashboard_missing_receipt_disposition_persisted")
                reopened = request(api_base, missing_path, method="POST", headers=operator_api,
                                   json={"status": "needs_review"})
                check(reopened.status_code == 200 and len(ledger.list_review_items(status="pending")) == 1,
                      "missing_receipt_review_reopened")
                denied = request(api_base, missing_path, method="POST", headers=hai_headers,
                                 json={"status": "ignored"})
                check(denied.status_code == 403 and ledger.get_reconciliation_match(missing_match)["status"] == "needs_review",
                      "hai_financial_disposition_denied")
                disposed = request(api_base, missing_path, method="POST", headers=operator_api,
                                   json={"status": "ignored"})
                check(disposed.status_code == 200 and not ledger.list_review_items(status="pending"),
                      "operator_missing_receipt_gate_closed")
                repeat = rpc("runCommand", {"commandId": "run_reconciliation"}, mutation=True)
                check(repeat["unmatchedDocuments"] == 0
                      and ledger.get_document(ready["documentId"])["reconciliation_status"] == "reconciled"
                      and rpc("controlCenter")["reviews"]["summary"]["reviewItems"] == 0,
                      "later_run_preserves_completed_bookkeeping")

                arrival_statement = json.dumps([{
                    "id": "synthetic-rehearsal-arrival", "date": "2026-09-30",
                    "amount": -8.5, "description": "Synthetic Arrival Shop",
                }]).encode()
                arrival_import = rpc("importBankStatement", {
                    "filename": "synthetic-arrival-statement.json", "format": "json",
                    "accountIdentifier": "synthetic-rehearsal-account",
                    "contentBase64": base64.b64encode(arrival_statement).decode("ascii"),
                }, mutation=True)
                check(arrival_import["rowsImported"] == 1, "dashboard_imports_arriving_receipt_bank_row")
                arrival_run = rpc("runCommand", {"commandId": "run_reconciliation"}, mutation=True)
                arrival_missing = arrival_run["results"][0]["reconciliationMatchId"]
                arrival_review = next(row["id"] for row in ledger.list_missing_receipt_review_items()
                                      if row["corrected_data"]["reconciliationMatchId"] == arrival_missing)
                check(arrival_run["missingReceipts"] == 1
                      and ledger.get_review_item(arrival_review)["status"] == "pending",
                      "receipt_arrival_starts_with_open_missing_exception")
                arrival_bytes = b"Synthetic receipt\nVendor: Synthetic Arrival Shop\nDate: 2026-09-30\nTotal: EUR 8.50\n"
                arrival_path = root / "synthetic-arrival-receipt.txt"
                arrival_path.write_bytes(arrival_bytes)
                arrival_document = ledger.register_document({
                    "source": "manual", "sourceDocumentId": "synthetic-arriving-receipt",
                    "originalFilename": arrival_path.name, "mimeType": "text/plain", "storagePath": str(arrival_path),
                    "contentSha256": hashlib.sha256(arrival_bytes).hexdigest(), "processingStatus": "processed",
                    "vendorName": "Synthetic Arrival Shop", "transactionDate": "2026-09-30", "totalAmount": 8.5,
                })
                arrival_candidate = rpc("runCommand", {"commandId": "run_reconciliation"}, mutation=True)
                arrival_match = arrival_candidate["results"][0]["reconciliationMatchId"]
                check(arrival_candidate["matchedCandidates"] == 1
                      and ledger.get_review_item(arrival_review)["status"] == "pending",
                      "unconfirmed_arrival_keeps_exception_open")
                arrival_source = request(web_base, f"/api/fab/source/{arrival_document}", headers=browser)
                check(arrival_source.status_code == 200 and arrival_source.content == arrival_bytes,
                      "arriving_receipt_bytes_verified_through_gateway")
                approval_review = ledger.list_review_items(status="pending", document_id=arrival_document)[0]["id"]
                rpc("resolveReview", {"reviewItemId": approval_review, "status": "approved",
                                      "resolution": "Synthetic arriving receipt confirmation", "learnRule": False}, mutation=True)
                old_exception = ledger.get_reconciliation_match(arrival_missing)
                check(old_exception["status"] == "resolved"
                      and old_exception["metadata"]["supersededBy"]["reconciliationMatchId"] == arrival_match
                      and ledger.get_review_item(arrival_review)["status"] == "resolved",
                      "confirmed_arrival_supersedes_only_its_missing_exception")
                confirmed_arrival = ledger.get_reconciliation_match(arrival_match)
                old_review = ledger.get_review_item(arrival_review)
                ledger.resolve_review_item(arrival_review, "pending", corrected_data=old_review["corrected_data"])
                rpc("resolveReview", {"reviewItemId": arrival_review, "status": "approved",
                                      "resolution": "Synthetic orphaned review repair", "learnRule": False}, mutation=True)
                check(ledger.get_review_item(arrival_review)["status"] == "resolved"
                      and ledger.get_reconciliation_match(arrival_match) == confirmed_arrival
                      and ledger.get_reconciliation_match(arrival_missing) == old_exception,
                      "dashboard_repairs_verified_orphan_without_rewriting_confirmation")
                retry = request(api_base, f"/api/reconciliation/{arrival_match}/resolve", method="POST",
                                headers=operator_api, json={"status": "approved"})
                check(retry.status_code == 200 and retry.json().get("alreadyFinal") is True
                      and ledger.get_reconciliation_match(arrival_match) == confirmed_arrival,
                      "operator_confirmation_retry_preserves_original_decision")
                check(rpc("controlCenter")["reviews"]["summary"]["reviewItems"] == 0,
                      "dashboard_refresh_removes_superseded_exception_gate")

                shared_reference = {"id": "synthetic-shared-account-reference", "date": "2026-09-30", "amount": -13.37}
                direct_a = {**shared_reference, "account_identifier": "synthetic-direct-A"}
                direct_b = {**shared_reference, "accountIdentifier": "synthetic-direct-B", "amount": -99}
                first_account = request(api_base, "/api/reconciliation/run", method="POST", headers=operator_api,
                                        json={"bankTransactions": [direct_a]})
                check(first_account.status_code == 200, "operator_direct_account_request_accepted")
                first_account_id = first_account.json()["results"][0]["reconciliationMatchId"]
                original_account = ledger.get_reconciliation_match(first_account_id)
                second_account = request(api_base, "/api/reconciliation/run", method="POST", headers=operator_api,
                                         json={"bankTransactions": [direct_b]})
                check(second_account.status_code == 200
                      and second_account.json()["results"][0]["reconciliationMatchId"] != first_account_id
                      and ledger.get_reconciliation_match(first_account_id) == original_account,
                      "operator_shared_bank_reference_preserves_separate_accounts")
                account_matches = ledger.list_reconciliation_matches()
                account_reviews = ledger.list_review_items()
                duplicate_account = request(api_base, "/api/reconciliation/run", method="POST", headers=operator_api,
                                            json={"bankTransactions": [direct_a, {**direct_a, "amount": -99}]})
                check(duplicate_account.status_code == 400
                      and ledger.list_reconciliation_matches() == account_matches
                      and ledger.list_review_items() == account_reviews,
                      "operator_duplicate_account_batch_rejected_without_financial_writes")

                for name, reference_changes in (
                    ("conflicting", {"transaction_id": "synthetic-other-reference"}),
                    ("boolean", {"id": True}),
                ):
                    invalid_reference = request(api_base, "/api/reconciliation/run", method="POST", headers=operator_api,
                                                json={"bankTransactions": [{**direct_a, **reference_changes}]})
                    check(invalid_reference.status_code == 400
                          and ledger.list_reconciliation_matches() == account_matches
                          and ledger.list_review_items() == account_reviews,
                          f"operator_invalid_bank_reference_rejected_{name}")

                ambiguous_account = ledger.get_reconciliation_match(first_account_id)["metadata"]
                ambiguous_account["bankTransaction"]["ledgerBankTransactionId"] = None
                ledger.update_reconciliation_match(first_account_id, {"metadata": ambiguous_account})
                ambiguous_matches = ledger.list_reconciliation_matches()
                ambiguous_reviews = ledger.list_review_items()
                retained_reference = request(api_base, "/api/reconciliation/run", method="POST", headers=operator_api,
                                             json={"bankTransactions": [direct_a]})
                check(retained_reference.status_code == 400
                      and ledger.list_reconciliation_matches() == ambiguous_matches
                      and ledger.list_review_items() == ambiguous_reviews,
                      "operator_retained_null_bank_reference_not_overwritten_as_unlinked")

                bounded_headers = {**operator_api, "Content-Type": "application/json"}
                oversized = '{"bankTransactions":[],"padding":"' + 'x' * (5 * 1024 * 1024) + '"}'
                bounded_response = request(api_base, "/api/reconciliation/run", method="POST",
                                           headers=bounded_headers, data=oversized)
                check(bounded_response.status_code == 413
                      and ledger.list_reconciliation_matches() == ambiguous_matches
                      and ledger.list_review_items() == ambiguous_reviews,
                      "operator_oversize_reconciliation_denied_without_financial_changes")
                deep_json = '{"bankTransactions":[],"nested":' + '[' * 3000 + '0' + ']' * 3000 + '}'
                deep_response = request(api_base, "/api/reconciliation/run", method="POST",
                                        headers=bounded_headers, data=deep_json)
                check(deep_response.status_code == 400
                      and ledger.list_reconciliation_matches() == ambiguous_matches
                      and ledger.list_review_items() == ambiguous_reviews,
                      "operator_deep_json_denied_without_financial_changes")

                empty_selection = request(api_base, "/api/reconciliation/run", method="POST", headers=operator_api,
                                          json={"bankTransactions": [], "documentIds": []})
                check(empty_selection.status_code == 200
                      and empty_selection.json().get("candidateDocuments") == 0
                      and ledger.list_reconciliation_matches() == ambiguous_matches
                      and ledger.list_review_items() == ambiguous_reviews,
                      "operator_empty_document_selection_does_not_expand_scope")
                for name, selected_ids in (("boolean", [True]), ("oversized", [999999] * 501), ("missing", [999999])):
                    invalid_selection = request(api_base, "/api/reconciliation/run", method="POST", headers=operator_api,
                                                json={"bankTransactions": [], "documentIds": selected_ids})
                    check(invalid_selection.status_code == 400
                          and ledger.list_reconciliation_matches() == ambiguous_matches
                          and ledger.list_review_items() == ambiguous_reviews,
                          "operator_invalid_document_selection_denied_" + name)

                for name, gate in (("review", {"processingStatus": "needs_review"}),
                                   ("duplicate", {"processingStatus": "processed", "duplicateOfDocumentId": 1})):
                    gated_document = ledger.register_document({"source": "manual",
                        "sourceDocumentId": "synthetic-gated-" + name, "originalFilename": name + ".pdf", **gate})
                    gated_response = request(api_base, "/api/reconciliation/run", method="POST", headers=operator_api,
                                             json={"bankTransactions": [], "documentIds": [gated_document]})
                    check(gated_response.status_code == 400
                          and ledger.list_reconciliation_matches() == ambiguous_matches
                          and ledger.list_review_items() == ambiguous_reviews,
                          "operator_ineligible_document_selection_denied_" + name)

                benchmark = None
                if benchmark_requests:
                    thread_state = threading.local()
                    benchmark_sessions = []
                    sessions_lock = threading.Lock()

                    def refresh_sample(_index):
                        sample_session = getattr(thread_state, "session", None)
                        if sample_session is None:
                            sample_session = requests.Session()
                            sample_session.trust_env = False
                            thread_state.session = sample_session
                            with sessions_lock:
                                benchmark_sessions.append(sample_session)
                        started = time.perf_counter()
                        response = sample_session.get(
                            web_base + "/api/trpc/fab.controlCenter",
                            headers=browser,
                            timeout=(3, 30),
                            allow_redirects=False,
                        )
                        elapsed_ms = (time.perf_counter() - started) * 1000
                        if response.status_code != 200:
                            body = response.text[:500].replace("\r", " ").replace("\n", " ")
                            raise RuntimeError(
                                f"Concurrent dashboard refresh returned HTTP {response.status_code}: {body}"
                            )
                        result = response.json().get("result", {}).get("data", {}).get("json", {})
                        require(result.get("connection", {}).get("connected") is True,
                                "Concurrent dashboard refresh lost its ledger connection")
                        resource_bytes = {
                            name: len(json.dumps(value, separators=(",", ":")).encode("utf-8"))
                            for name, value in result.items()
                            if name not in {"connection", "resourceStates", "partialErrors"}
                        }
                        return elapsed_ms, len(response.content), resource_bytes

                    try:
                        with (
                            _ProcessResourceSampler({"api": api.pid, "dashboard": web.pid}) as sampler,
                            ThreadPoolExecutor(max_workers=benchmark_concurrency) as pool,
                        ):
                            samples = list(pool.map(refresh_sample, range(benchmark_requests)))
                        process_resources = sampler.result()
                    finally:
                        for sample_session in benchmark_sessions:
                            sample_session.close()
                    latencies = sorted(sample[0] for sample in samples)
                    payload_bytes = [sample[1] for sample in samples]
                    resource_bytes = {
                        name: round(statistics.median(sample[2].get(name, 0) for sample in samples))
                        for name in sorted({key for sample in samples for key in sample[2]})
                    }
                    benchmark = {
                        "route": "GET /api/trpc/fab.controlCenter",
                        "cacheState": "warm",
                        "requests": len(samples),
                        "concurrency": benchmark_concurrency,
                        "latencyMs": {
                            "median": round(statistics.median(latencies), 2),
                            "p95": round(latencies[math.ceil(0.95 * len(latencies)) - 1], 2),
                            "max": round(latencies[-1], 2),
                        },
                        "responseBytes": {
                            "median": round(statistics.median(payload_bytes)),
                            "max": max(payload_bytes),
                        },
                        "medianResourceBytes": resource_bytes,
                        "processResources": process_resources,
                    }
                    check(len(samples) == benchmark_requests,
                          "concurrent_dashboard_refresh_benchmark")

                handoff = request(web_base, "/api/fab/operator-session?next=/api/live", headers=browser)
                destination = urlsplit(handoff.headers.get("Location", ""))
                check(handoff.status_code == 302 and f"{destination.scheme}://{destination.netloc}" == api_base,
                      "ledger_handoff_targets_owned_api")
                ticket_path = destination.path + "?" + destination.query
                bootstrap = request(api_base, ticket_path)
                check(bootstrap.status_code == 302, "signed_ledger_handoff")
                check(request(api_base, "/api/live").status_code == 200, "parent_bound_ledger_session")
                check(request(api_base, ticket_path).status_code == 401, "handoff_replay_denied")
                check(request(web_base, "/operator/logout", method="POST", headers=browser).status_code == 303,
                      "managed_operator_logout")
                check(request(web_base, source_path, headers=browser).status_code == 403, "gateway_logout_revoked")
                check(request(api_base, "/api/live").status_code == 401, "ledger_parent_logout_revoked")

        except Exception as error:
            diagnostics = []
            for name in ("api.log", "web.log"):
                with (root / name).open("rb") as log:
                    log.seek(0, os.SEEK_END)
                    log.seek(max(0, log.tell() - 8_192))
                    tail = log.read(8_192).decode("utf-8", errors="replace")
                for token in tokens.values():
                    tail = tail.replace(token, "[redacted]")
                diagnostics.append(name + ": " + tail.replace(str(root), "[rehearsal]"))
            message = "Rehearsal HTTP transport failed" if isinstance(error, requests.RequestException) else str(error)
            raise RuntimeError(message + "\n" + "\n".join(diagnostics)) from None
        finally:
            session.close()
            failures = []
            for child in reversed(children):
                try:
                    stop_owned_child(child)
                except Exception:
                    failures.append(child.pid)
            require(not failures, "One or more owned rehearsal children did not stop")
    return {"syntheticOnly": True, "loopbackOnly": True, "simulatedTrustedProxy": True,
            "providerWrites": False, "checks": checks, "ownedChildrenStopped": True,
            "performanceBenchmark": benchmark}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", help="Node.js executable (defaults to PATH)")
    parser.add_argument("--bundle-dir", help="Production bundle directory (defaults to web/dist)")
    parser.add_argument("--benchmark-requests", type=int, default=0,
                        help="Measure concurrent authenticated dashboard refreshes (1-30; 0 disables)")
    parser.add_argument("--benchmark-concurrency", type=int, default=8,
                        help="Maximum concurrent dashboard refreshes (1-16)")
    parser.add_argument("--fixture-api", help=argparse.SUPPRESS)
    parser.add_argument("--web-port", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.benchmark_concurrency <= 16:
        parser.error("--benchmark-concurrency must be between 1 and 16")
    if not 0 <= args.benchmark_requests <= 30:
        parser.error("--benchmark-requests must be between 0 and 30; larger bursts exceed the combined rehearsal's rate-limit budget")
    if args.fixture_api:
        fixture_api(args.fixture_api, args.web_port)
    else:
        print(json.dumps(rehearse(args.node, args.bundle_dir,
                                  args.benchmark_requests, args.benchmark_concurrency)))


if __name__ == "__main__":
    main()
