"""Read-only production configuration checks; no financial/provider actions."""

import os
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from src.security.deployment_secrets import strong_secret, valid_api_token
from src.operations.parent_operator_session import parent_validation_url, requires_parent_session, valid_validation_url

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _value(config, *keys, default=None):
    return next((config[k] for k in keys if k in config and config[k] not in (None, "")), default)


def deployment_preflight(config):
    profile = str(_value(config, "fab_deployment_profile", "operations_deployment_profile", default="local"))
    issues = []

    def add(code, message, severity="blocked"):
        issues.append({"id": code, "type": code, "code": code, "severity": severity, "message": message, "nextAction": message})

    if requires_parent_session(config) and not valid_validation_url(parent_validation_url(config), profile):
        add("deployment_parent_session", "Configure FAB_OPERATOR_SESSION_VALIDATION_URL for managed dashboard session checks.")

    token = _value(config, "fab_local_api_token", "fab_operations_api_token", "operations_api_token", default="")
    if token != "" and not valid_api_token(token):
        add("deployment_api_secret", "Configure an API token using printable ASCII without whitespace, at most 8192 characters.")
    elif (profile != "local" or requires_parent_session(config)) and not strong_secret(token):
        add("deployment_api_secret", "Configure a random API token of at least 32 characters.")
    hai_token = _value(config, "fab_hai_api_token", "operations_hai_api_token", "hai_api_token", default="")
    if hai_token != "" and (not valid_api_token(hai_token)
                            or (profile != "local" and (not strong_secret(hai_token) or hai_token == token))):
        add("deployment_hai_secret", "Configure a separate HAI token using printable ASCII without whitespace, at most 8192 characters; production requires a random token of at least 32 characters.")
    if profile not in {"local", "windows", "vm"}:
        add("deployment_profile", "Set FAB_DEPLOYMENT_PROFILE to local, windows, or vm.")
    elif profile != "local":
        host = str(_value(config, "fab_local_api_host", "operations_api_host", default="127.0.0.1"))
        if profile == "windows" and host not in LOOPBACK:
            add("deployment_bind", "The Windows profile requires a loopback API host.")
        if profile == "vm" and host not in LOOPBACK | {"0.0.0.0", "::"}:
            add("deployment_bind", "Use a loopback or container wildcard API host in the VM profile.")
        port = str(_value(config, "fab_local_api_port", "operations_api_port", default=5001))
        if not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535:
            add("deployment_port", "Set FAB_LOCAL_API_PORT to an integer between 1 and 65535.")
        if profile == "vm":
            public_url = str(_value(config, "fab_operator_dashboard_url", "operations_operator_dashboard_url", default=""))
            try:
                url = urlsplit(public_url)
                if (url.scheme != "https" or not url.hostname or url.username or url.password
                        or url.query or url.fragment or url.port == 0):
                    raise ValueError()
            except ValueError:
                add("deployment_https", "Configure FAB_OPERATOR_DASHBOARD_URL as the operator HTTPS URL.")
            try:
                url = urlsplit(str(_value(config, "fab_local_api_base_url", "operations_api_base_url", default="")))
                if (url.scheme != "https" or not url.hostname or url.username or url.password
                        or url.path not in {"", "/"} or url.query or url.fragment or url.port == 0):
                    raise ValueError()
            except ValueError:
                add("deployment_ledger_https", "Configure FAB_LOCAL_API_BASE_URL as the ledger HTTPS origin for secure sessions.")
        root = Path(str(_value(config, "fab_instance_root", default=os.getcwd()))).resolve()
        paths = {
            "ledger": _value(config, "fab_local_ledger_path", "operations_ledger_path"),
            "backup": _value(config, "fab_local_backup_dir", "operations_backup_dir", "backup_base_dir"),
        }
        for label, value in paths.items():
            path = Path(str(value or ""))
            if not value or not path.is_absolute() or ".." in path.parts:
                add("deployment_" + label + "_path", f"Configure an absolute {label} path without parent traversal.")
                continue
            path = path.resolve()
            if profile == "windows" and path.is_relative_to(root):
                add("deployment_" + label + "_location", f"Store the production {label} outside the source checkout.")
            directory = path.parent if label == "ledger" else path
            existing = directory
            while not existing.exists() and existing != existing.parent:
                existing = existing.parent
            if not existing.is_dir() or not os.access(existing, os.W_OK):
                add("deployment_" + label + "_writable", f"Ensure the {label} directory is writable by the service account.")
                continue
            if label == "backup" and path.exists() and not path.is_dir():
                add("deployment_backup_directory", "The backup location must be a directory.")
            try:
                if shutil.disk_usage(existing).free < 256 * 1024 * 1024:
                    add("deployment_" + label + "_disk", f"Free at least 256 MiB on the {label} volume.")
            except OSError:
                add("deployment_" + label + "_disk_unknown", f"Check available storage for the {label} volume.", "attention")
        ledger_enabled = config.get("fab_local_ledger_enabled", config.get("operations_local_ledger_enabled"))
        if ledger_enabled is not None and str(ledger_enabled).strip().lower() in {"", "false", "0", "no", "off"}:
            add("deployment_ledger_disabled", "Enable the authoritative operations ledger before starting production services.")
        scheduled = config.get("worker_create_scheduled_backups", True)
        if scheduled is None or str(scheduled).strip().lower() in {"", "false", "0", "no", "off"}:
            add("deployment_backups_disabled", "Enable scheduled source-complete recovery backups.")
        complete = _value(config, "backup_require_complete_source_evidence", "fab_backup_require_complete_source_evidence", default=True)
        if str(complete).strip().lower() not in {"true", "1", "yes", "on", "enabled"}:
            add("deployment_backup_evidence", "Require complete source evidence in scheduled recovery backups.")
        encryption = _value(config, "fab_storage_encryption_confirmed", "operations_storage_encryption_confirmed", default=False)
        if str(encryption).lower() not in {"true", "1", "yes", "on"}:
            add("deployment_storage_encryption", "Confirm host encryption for the ledger and backup volumes after checking the host.", "attention")

    blocked = any(i["severity"] == "blocked" for i in issues)
    return {"profile": profile, "status": "blocked" if blocked else "degraded" if issues else "ready",
            "issues": issues, "storageEncryptionVerification": "operator_attestation_only",
            "providerAcceptance": "assessed_separately"}


def require_deployment_ready(config):
    report = deployment_preflight(config)
    if report["status"] == "blocked":
        raise ValueError("FAB startup blocked: " + "; ".join(i["message"] for i in report["issues"] if i["severity"] == "blocked"))
    return report
