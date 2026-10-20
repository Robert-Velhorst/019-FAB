import json
import os
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def compose():
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Docker CLI is required for Compose configuration checks")
    version = subprocess.run([docker, "compose", "version"], capture_output=True, text=True, timeout=30)
    if version.returncode:
        pytest.skip("Docker Compose is unavailable")
    return docker


def rendered(compose, tmp_path, *, hai=True, token_file=True, commands=None):
    keep = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "APPDATA", "USERPROFILE", "HOME",
            "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "HOMEDRIVE", "HOMEPATH"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in keep}
    environment.update({
        "FAB_OPERATOR_PUBLIC_ORIGIN": "https://fab.example.test",
        "FAB_LEDGER_PUBLIC_ORIGIN": "https://ledger.example.test",
        "FAB_TRUSTED_PROXY_ADDRESSES": "127.0.0.1",
        "FAB_INTAKE_DIR": str(tmp_path / "intake"),
        "FAB_API_TOKEN_FILE": str(tmp_path / "operator-api-token"),
        "FAB_SESSION_SECRET_FILE": str(tmp_path / "session-secret"),
        "FAB_OPERATOR_TOKEN_FILE": str(tmp_path / "operator-login-token"),
    })
    if token_file:
        environment["FAB_HAI_TOKEN_FILE"] = str(tmp_path / "hai-token")
    if commands is not None:
        environment["FAB_HAI_ALLOWED_COMMANDS"] = commands
    command = [compose, "compose", "--env-file", os.devnull, "-f", "docker-compose.vm.yml"]
    if hai:
        command.extend(["-f", "docker-compose.vm.hai.yml"])
    return subprocess.run([*command, "config", "--format", "json"], cwd=ROOT,
                          env=environment, capture_output=True, text=True, timeout=30)


def test_hai_is_explicit_opt_in_and_has_no_default_mount(compose, tmp_path):
    result = rendered(compose, tmp_path, hai=False, token_file=False)
    assert result.returncode == 0, result.stderr
    model = json.loads(result.stdout)
    assert "hai_token" not in model["secrets"]
    for service in model["services"].values():
        assert "FAB_HAI_API_TOKEN_FILE" not in service["environment"]
        assert all(secret["source"] != "hai_token" for secret in service["secrets"])


@pytest.mark.parametrize("commands", [None, "refresh_notifications"])
def test_hai_override_preserves_vm_security_and_limits_secret_to_api(compose, tmp_path, commands):
    result = rendered(compose, tmp_path, commands=commands)
    assert result.returncode == 0, result.stderr
    model = json.loads(result.stdout)
    api = model["services"]["api"]
    assert {secret["source"] for secret in api["secrets"]} == {"api_token", "hai_token"}
    assert api["environment"]["FAB_HAI_API_TOKEN_FILE"] == "/run/secrets/hai_token"
    assert api["environment"]["FAB_HAI_CONNECTOR_ENABLED"] == "true"
    assert api["environment"]["FAB_HAI_ALLOWED_COMMANDS"] == (commands or "refresh_notifications,engage_emergency_stop")
    assert api["environment"]["FAB_LOCAL_API_TOKEN_FILE"] == "/run/secrets/api_token"
    assert api["environment"]["FAB_STORAGE_ENCRYPTION_CONFIRMED"] == "false"
    assert api["cap_drop"] == ["ALL"]
    assert api["security_opt"] == ["no-new-privileges:true"]
    assert all(port["host_ip"] == "127.0.0.1" for port in api["ports"])
    for name in ("worker", "web"):
        service = model["services"][name]
        assert "FAB_HAI_API_TOKEN_FILE" not in service["environment"]
        assert all(secret["source"] != "hai_token" for secret in service["secrets"])
    assert Path(model["secrets"]["hai_token"]["file"]).name == "hai-token"


def test_hai_override_requires_a_dedicated_secret_file(compose, tmp_path):
    result = rendered(compose, tmp_path, token_file=False)
    assert result.returncode != 0
    assert "FAB_HAI_TOKEN_FILE" in result.stderr


@pytest.mark.parametrize("credential", ["dedicated", "operator", "weak"])
def test_rendered_settings_load_secret_files_and_enforce_real_api_scope(compose, tmp_path, credential):
    from src.config_loader import ConfigLoader
    from src.operations.local_api import create_app
    from src.operations.local_ledger import LocalOperationsLedger

    result = rendered(compose, tmp_path)
    assert result.returncode == 0, result.stderr
    environment = json.loads(result.stdout)["services"]["api"]["environment"]
    operator = "T4qR9sP7vM2aK8wE5dG1hJ6cN0yU3bZx"
    dedicated = "R8mK2pQ7sT4vW9xA5bC1dE6fG0hJ3nLy"
    hai = dedicated if credential == "dedicated" else operator if credential == "operator" else "weak"
    operator_file = tmp_path / "operator-api-token"
    hai_file = tmp_path / "hai-token"
    operator_file.write_text(operator)
    hai_file.write_text(hai)
    ledger_path = tmp_path / "data" / "ledger.sqlite3"
    environment.update({
        "FAB_LOCAL_API_TOKEN_FILE": str(operator_file),
        "FAB_HAI_API_TOKEN_FILE": str(hai_file),
        "FAB_INSTANCE_ROOT": str(tmp_path),
        "FAB_LOCAL_LEDGER_PATH": str(ledger_path),
        "FAB_LOCAL_BACKUP_DIR": str(tmp_path / "backups"),
        "FAB_LOCAL_INTAKE_PATHS": str(tmp_path / "intake"),
        # Synthetic attestation exercises configuration, not storage encryption.
        "FAB_STORAGE_ENCRYPTION_CONFIRMED": "true",
    })
    with patch.dict(os.environ, environment, clear=True):
        config = ConfigLoader(str(tmp_path / "absent.ini")).get_all_config()
    assert config["fab_hai_api_token"] == hai
    assert config["fab_local_api_token"] == operator
    if credential != "dedicated":
        with pytest.raises(ValueError, match="startup blocked"):
            create_app(config)
        assert not ledger_path.exists()
        return
    client = create_app(config).test_client()
    headers = {"Authorization": "Bearer " + dedicated}
    assert client.get("/api/hai/manifest").status_code == 401
    response = client.get("/api/hai/manifest", headers=headers)
    assert response.status_code == 200
    manifest = response.get_json()
    assert manifest["remoteAuthenticationConfigured"] is True
    assert {command["commandId"] for command in manifest["commands"] if command["allowed"]} == {
        "refresh_notifications", "engage_emergency_stop",
    }
    assert dedicated not in response.get_data(as_text=True)
    assert operator not in response.get_data(as_text=True)
    assert client.get("/api/settings", headers=headers).status_code == 403
    assert client.post("/api/hai/commands/execute", headers=headers, json={
        "requestId": "vm-denied-command", "commandId": "run_safe_cycle",
    }).status_code == 403
    with patch("src.operations.local_api.LocalNotificationService.refresh", return_value={"status": "refreshed"}) as refresh:
        body = {"requestId": "vm-scoped-command", "commandId": "refresh_notifications"}
        assert client.post("/api/hai/commands/execute", headers=headers, json=body).status_code == 200
        replay = client.post("/api/hai/commands/execute", headers=headers, json=body)
        assert replay.status_code == 200 and replay.get_json()["status"] == "already_executed"
        refresh.assert_called_once_with(actor="fab_hai_api:hai")
    assert LocalOperationsLedger(str(ledger_path)).find_audit_event(
        "hai.command.completed", "hai_command_request", "vm-scoped-command",
    ) is not None
