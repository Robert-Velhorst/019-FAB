"""Opt-in synthetic SQLite -> HTTP operations API -> disposable MySQL rehearsal."""

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run(arguments, *, env=None, cwd=ROOT, timeout=60):
    result = subprocess.run(arguments, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("Synthetic rehearsal command failed: " + str(arguments[0]))
    return result.stdout.strip()


def rehearse():
    from src.operations.operations_client import OperationsClient

    node = shutil.which("node")
    docker = shutil.which("docker")
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not all((node, docker, npm)) or not (ROOT / "web/dist/index.js").is_file():
        raise RuntimeError("Requires Docker, npm, Node, installed web dependencies, and a built web/dist/index.js")
    try:
        if run([docker, "info", "--format", "{{.OSType}}"], timeout=15) != "linux":
            raise RuntimeError("Linux engine required")
        run([docker, "image", "inspect", "mysql:8.4"], timeout=15)
    except (RuntimeError, subprocess.TimeoutExpired):
        raise RuntimeError("Requires a running Docker Linux engine and an already-cached mysql:8.4 image; nothing was started") from None
    name = "fab-identity-rehearsal-" + uuid.uuid4().hex[:12]
    password = secrets.token_hex(24)
    token = secrets.token_hex(32)
    process = None
    clients = []
    started = False
    try:
        run([docker, "run", "--rm", "--pull=never", "--name", name, "--memory=768m", "--cpus=1",
             "--tmpfs", "/var/lib/mysql:rw,size=512m", "-e", "MYSQL_ROOT_PASSWORD=" + password,
             "-e", "MYSQL_DATABASE=fab_test", "-p", "127.0.0.1::3306", "-d", "mysql:8.4",
             "--performance-schema=OFF", "--innodb-buffer-pool-size=64M", "--mysqlx=0"])
        started = True
        deadline = time.monotonic() + 100
        while True:
            ready = subprocess.run([docker, "exec", "-e", "MYSQL_PWD=" + password, name,
                                    "mysqladmin", "ping", "-h", "127.0.0.1", "-uroot", "--silent"],
                                   capture_output=True, timeout=10)
            if ready.returncode == 0:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Disposable MySQL readiness timed out")
            time.sleep(1)
        address = run([docker, "port", name, "3306/tcp"])
        if not address.startswith("127.0.0.1:"):
            raise RuntimeError("Disposable MySQL must bind only to loopback")
        port = int(address.rsplit(":", 1)[1])
        env = {key: value for key, value in os.environ.items() if key.upper() in {
            "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
        }}
        env.update({"DATABASE_URL": f"mysql://root:{password}@127.0.0.1:{port}/fab_test",
                    "DOTENV_CONFIG_PATH": os.devnull, "NODE_ENV": "production", "FAB_DEPLOYMENT_PROFILE": "windows",
                    "FAB_WEB_HOST": "127.0.0.1", "FAB_OPERATOR_LOCAL_MODE": "true", "FAB_BILLING_ENABLED": "false",
                    "FAB_LOCAL_API_TOKEN": secrets.token_hex(32), "FAB_OPERATIONS_SERVICE_TOKEN": token,
                    "JWT_SECRET": secrets.token_hex(32)})
        migrate = [npm, "run", "db:migrate"]
        if os.name == "nt":
            migrate = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "npm.cmd run db:migrate"]
        run(migrate, env=env, cwd=ROOT / "web")
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            web_port = reservation.getsockname()[1]
        env["PORT"] = str(web_port)
        process = subprocess.Popen([node, "dist/index.js"], cwd=ROOT / "web", env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        base_url = f"http://127.0.0.1:{web_port}"
        deadline = time.monotonic() + 45
        while True:
            if process.poll() is not None:
                raise RuntimeError("Built full web server exited during startup")
            try:
                if requests.get(base_url, timeout=2).status_code == 200:
                    break
            except requests.ConnectionError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("Built full web server readiness timed out")
            time.sleep(0.2)
        with tempfile.TemporaryDirectory(prefix="fab-identity-rehearsal-") as directory:
            config = {"fab_operations_api_url": base_url, "fab_operations_api_token": token,
                      "fab_local_ledger_enabled": True, "fab_local_ledger_path": str(Path(directory) / "ledger.sqlite3")}
            client = OperationsClient(config)
            clients.append(client)
            existing = client.local_ledger.register_document({"source": "gmail", "sourceDocumentId": "old-local", "originalFilename": "old.pdf"})
            client.local_ledger.create_workflow_run({"triggerSource": "old-local"})
            workflow = client.create_workflow_run("identity-rehearsal")
            document = client.register_document("gmail", {"id": "new-remote", "filename": "new.pdf"})
            review = client.create_review_item(document, "identity-rehearsal")
            routing = client.create_routing_attempt(document, "waveapps_business", "requires_review", workflow_run_id=workflow)
            if document == existing or any(value is None for value in (workflow, document, review, routing)):
                raise RuntimeError("Local collision rehearsal did not retain distinct records")
            restarted = OperationsClient(config)
            clients.append(restarted)
            if restarted.update_document(document, processing_status="validated").get("status") != "success":
                raise RuntimeError("Restarted client could not update its mapped remote document")
            if restarted.update_workflow_run(workflow, status="completed").get("status") != "success":
                raise RuntimeError("Restarted client could not update its mapped remote workflow")
            query = """import mysql from 'mysql2/promise';
                const db=await mysql.createConnection(process.env.DATABASE_URL);
                try { const output={}; for(const table of ['bookkeeping_documents','workflow_runs','review_items','routing_attempts']) {
                    const [rows]=await db.query('SELECT * FROM `'+table+'`'); output[table]=rows; }
                    console.log(JSON.stringify(output)); } finally { await db.end(); }"""
            remote = json.loads(run([node, "--input-type=module", "-e", query], env=env, cwd=ROOT / "web"))
            remote_doc = remote["bookkeeping_documents"][0]
            remote_run = remote["workflow_runs"][0]
            if (remote_doc["id"] != 1 or remote_doc["sourceDocumentId"] != "new-remote" or remote_doc["processingStatus"] != "validated"
                    or remote_run["id"] != 1 or remote_run["status"] != "completed"
                    or remote["review_items"][0]["documentId"] != 1
                    or remote["routing_attempts"][0]["documentId"] != 1 or remote["routing_attempts"][0]["workflowRunId"] != 1
                    or client.local_ledger.get_document(existing)["source_document_id"] != "old-local"):
                raise RuntimeError("Remote/local relationship assertions failed")
            return {"success": True, "localDocumentId": document, "remoteDocumentId": 1,
                    "localWorkflowId": workflow, "remoteWorkflowId": 1, "restartMappingVerified": True,
                    "remoteReviewAndRoutingVerified": True, "originalLocalEvidencePreserved": True}
    finally:
        for client in clients:
            client.session.close()
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if started:
            run([docker, "stop", "--time", "10", name], timeout=30)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Start an isolated cached-MySQL rehearsal with temporary data")
    arguments = parser.parse_args()
    if not arguments.run:
        parser.error("Pass --run to explicitly start the disposable rehearsal")
    print(json.dumps(rehearse(), sort_keys=True))
