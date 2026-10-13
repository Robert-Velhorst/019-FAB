import json
from unittest.mock import patch

import pytest

from src.config_loader import ConfigLoader
from src.operations.deployment_preflight import deployment_preflight, require_deployment_ready
from src.security.deployment_secrets import read_secret, strong_secret

TOKEN = "D7pH8sAk3C5nZ9wL2tB6eQ0rV1mF4yUj"


def production(tmp_path, profile="windows"):
    return {"fab_deployment_profile": profile, "fab_local_api_token": TOKEN,
            "fab_instance_root": str(tmp_path / "code"),
            "fab_local_ledger_path": str(tmp_path / "data" / "ledger.sqlite3"),
            "fab_local_backup_dir": str(tmp_path / "backups"),
            "fab_operator_dashboard_url": "https://fab.example.org/admin/operations",
            "fab_local_api_base_url": "https://ledger.example.org",
            "fab_operator_session_validation_url": "https://fab.example.org/api/fab/operator-session/status",
            "fab_storage_encryption_confirmed": True}


def test_profiles_and_redaction(tmp_path):
    for profile in ("windows", "vm"):
        config = production(tmp_path, profile)
        report = require_deployment_ready(config)
        assert report["status"] == "ready"
        assert TOKEN not in json.dumps(report)
    assert deployment_preflight({})["profile"] == "local"
    assert deployment_preflight({"fab_deployment_profile": "typo"})["status"] == "blocked"


def test_vm_requires_explicit_parent_session_authority(tmp_path):
    config = production(tmp_path, "vm")
    config.pop("fab_operator_session_validation_url")
    assert deployment_preflight(config)["status"] == "blocked"
    config["fab_operator_session_validation_url"] = "http://web:3000/api/fab/operator-session/status"
    assert deployment_preflight(config)["status"] == "ready"


def test_vm_alias_cannot_skip_parent_session_authority(tmp_path):
    config = production(tmp_path, "vm")
    config.pop("fab_deployment_profile")
    config.pop("fab_operator_session_validation_url")
    config["operations_deployment_profile"] = "vm"
    assert any(issue["id"] == "deployment_parent_session" for issue in deployment_preflight(config)["issues"])


@pytest.mark.parametrize("token", ["", "weak"])
def test_managed_local_requires_api_authority_before_creating_ledger(tmp_path, token):
    from src.operations.local_api import create_app

    config = {"fab_local_api_token": token, "fab_operator_access_token": TOKEN,
              "fab_operator_session_validation_url": "http://127.0.0.1:3000/api/fab/operator-session/status",
              "fab_local_ledger_path": str(tmp_path / "must-not-create.sqlite3")}
    assert any(issue["id"] == "deployment_api_secret" for issue in deployment_preflight(config)["issues"])
    with pytest.raises(ValueError, match="startup blocked"):
        create_app(config)
    assert not (tmp_path / "must-not-create.sqlite3").exists()


@pytest.mark.parametrize("value", ["", "A" * 43, "changeme" * 10, "<random secret replace me here please>", "abc\n" * 12])
def test_weak_tokens_block_startup_before_ledger_creation(tmp_path, value):
    from src.operations.local_api import create_app
    config = production(tmp_path)
    config["fab_local_api_token"] = value
    with pytest.raises(ValueError, match="startup blocked"):
        create_app(config)
    assert not (tmp_path / "data").exists()
    assert not strong_secret(value)


@pytest.mark.parametrize("key,value", [
    ("fab_local_api_host", "0.0.0.0"), ("fab_local_api_port", "3000oops"),
    ("fab_local_api_port", "0"), ("fab_local_api_port", 65536),
    ("fab_local_backup_dir", "relative/backups"), ("worker_create_scheduled_backups", False),
    ("backup_require_complete_source_evidence", False),
    ("fab_backup_require_complete_source_evidence", False),
    ("backup_require_complete_source_evidence", "disabled"),
    ("fab_local_ledger_enabled", False),
    ("operations_local_ledger_enabled", False),
    ("worker_create_scheduled_backups", None),
])
def test_invalid_configuration_blocks(tmp_path, key, value):
    config = {**production(tmp_path), key: value}
    assert deployment_preflight(config)["status"] == "blocked"


def test_locations_disk_and_encryption(tmp_path):
    config = production(tmp_path)
    config["fab_local_backup_dir"] = str(tmp_path / "code" / "backups")
    assert deployment_preflight(config)["status"] == "blocked"
    config = production(tmp_path)
    config["fab_storage_encryption_confirmed"] = False
    assert require_deployment_ready(config)["status"] == "degraded"
    with patch("src.operations.deployment_preflight.shutil.disk_usage") as usage:
        usage.return_value.free = 12
        assert deployment_preflight(config)["status"] == "blocked"


@pytest.mark.parametrize("url", ["http://fab.example.org", "https://user:pass@host", "https://[", "", "https://host/?token=x"])
def test_vm_https_origin_required(tmp_path, url):
    config = {**production(tmp_path, "vm"), "fab_operator_dashboard_url": url}
    assert deployment_preflight(config)["status"] == "blocked"


def test_secret_file_loader_preserves_values_and_redacts_failures(tmp_path, monkeypatch):
    source = tmp_path / "token"
    source.write_text(TOKEN + "\n", encoding="utf-8")
    monkeypatch.delenv("FAB_LOCAL_API_TOKEN", raising=False)
    monkeypatch.setenv("FAB_LOCAL_API_TOKEN_FILE", str(source))
    config = ConfigLoader(str(tmp_path / "absent.ini")).get_all_config()
    assert config["fab_local_api_token"] == TOKEN
    monkeypatch.setenv("FAB_LOCAL_API_TOKEN", TOKEN)
    with pytest.raises(ValueError) as failure:
        ConfigLoader(str(tmp_path / "absent.ini"))
    assert TOKEN not in str(failure.value)
    for content in ("", "x" * 8193, "abc\x00def"):
        source.write_text(content, encoding="utf-8")
        with pytest.raises(ValueError):
            read_secret("FAB_LOCAL_API_TOKEN", {"FAB_LOCAL_API_TOKEN_FILE": str(source)})


def test_readiness_includes_deployment_blockers(tmp_path):
    from src.operations.local_readiness import LocalReadinessService
    config = {**production(tmp_path), "fab_local_api_token": "weak"}
    readiness = LocalReadinessService(config)
    report = readiness.summarize()
    assert any(i.get("id") == "deployment_api_secret" for i in report["issues"])
    assert report["status"] == "blocked"
