from __future__ import annotations

import copy
import importlib.util
import json
import os
import platform
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from src.utils.tesseract_runtime import (
    available_tesseract_languages,
    configured_tesseract_languages,
    resolve_poppler_path,
    resolve_tesseract_command,
)
from src.utils.runtime_identity import local_instance_id
from src.security.google_oauth_store import GoogleOAuthTokenStore
from src.operations.deployment_preflight import deployment_preflight


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
SECRET_MARKERS = ("token", "secret", "password", "api_key", "client_secret", "credential")
MAX_RUNTIME_METADATA_BYTES = 64 * 1024
MAX_RUNTIME_IDENTITY_BYTES = 32 * 1024
RUNTIME_IDENTITY_CACHE_SECONDS = 2.0
DEPENDENCY_CACHE_SECONDS = 5.0


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LocalReadinessService:
    """Summarize local FAB setup readiness without returning secret values."""

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        *,
        ledger_path: Optional[str] = None,
        api_host: Optional[str] = None,
        api_port: Optional[Any] = None,
        base_url: Optional[str] = None,
        api_token_configured: Optional[bool] = None,
        intake_paths: Optional[List[str]] = None,
        intake_extensions: Optional[List[str]] = None,
        operator_dashboard_url: Optional[str] = None,
        runtime_path: Optional[str] = None,
        instance_root: Optional[str] = None,
    ):
        self.config = config or {}
        self.ledger_path = ledger_path or str(
            _config_value(
                self.config,
                "fab_local_ledger_path",
                "operations_ledger_path",
                "ledger_path",
                default="data/fab_operations.sqlite3",
            )
        )
        self.api_host = api_host or str(
            _config_value(
                self.config,
                "fab_local_api_host",
                "operations_api_host",
                "api_host",
                default="127.0.0.1",
            )
        )
        self.api_port = _bounded_port(api_port or _config_value(
            self.config,
            "fab_local_api_port",
            "operations_api_port",
            "api_port",
            default=5001,
        ))
        self.base_url = _normalized_base_url(base_url or _config_value(
            self.config,
            "fab_local_api_base_url",
            "operations_api_url",
            "operations_api_base_url",
        ))
        self.api_token_configured = (
            api_token_configured
            if api_token_configured is not None
            else _has_value(
                self.config,
                "fab_local_api_token",
                "fab_operations_api_token",
                "operations_api_token",
            )
        )
        self.intake_paths = intake_paths if intake_paths is not None else _list_config(
            self.config,
            "fab_local_intake_paths",
            "operations_local_intake_paths",
            "operations_intake_paths",
            "operations_scanner_folder",
            "operations_scanner_watch_folder",
            "scanner_folder",
            "scanner_watch_folder",
        )
        self.intake_extensions = intake_extensions if intake_extensions is not None else _list_config(
            self.config,
            "fab_local_intake_extensions",
            "operations_local_intake_extensions",
            "operations_intake_extensions",
        )
        self.instance_root = os.path.realpath(os.path.abspath(str(
            instance_root
            or _config_value(self.config, "fab_instance_root", default=os.getcwd())
        )))
        runtime_value = runtime_path or _config_value(
            self.config,
            "fab_runtime_path",
            "operations_runtime_path",
            default=os.path.join(os.path.dirname(os.path.abspath(self.ledger_path)), "fab-runtime.json"),
        )
        self.runtime_path = os.path.realpath(os.path.abspath(str(runtime_value)))
        configured_dashboard_url = (
            operator_dashboard_url
            if operator_dashboard_url is not None
            else _config_value(
                self.config,
                "fab_operator_dashboard_url",
                "operations_operator_dashboard_url",
            )
        )
        self.operator_dashboard_url = _normalized_operator_dashboard_url(configured_dashboard_url)
        self._runtime_cache_lock = threading.Lock()
        self._runtime_cache_signature: Optional[tuple] = None
        self._runtime_cache_checked_at = 0.0
        self._runtime_cache_dashboard_url = ""
        self._dependency_cache_lock = threading.Lock()
        self._dependency_cache_checked_at = 0.0
        self._dependency_cache: Optional[List[Dict[str, Any]]] = None

    def summarize(self) -> Dict[str, Any]:
        dependencies = self._dependencies()
        paths = self._paths()
        credentials = self._credentials()
        sources = self._sources(credentials, paths, dependencies)
        security = self._security()
        local_access = self._local_access(security)
        issues = self._issues(dependencies, paths, credentials, sources, security)
        deployment = deployment_preflight(self.config)
        issues.extend(deployment["issues"])
        status = _overall_status(issues)
        return {
            "status": status,
            "deployment": deployment,
            "security": security,
            "localAccess": local_access,
            "paths": paths,
            "dependencies": dependencies,
            "credentials": credentials,
            "sources": sources,
            "issues": issues,
            "nextActions": _next_actions(issues, sources),
        }

    def compact(self) -> Dict[str, Any]:
        summary = self.summarize()
        return {
            "status": summary["status"],
            "issueCount": len(summary["issues"]),
            "blockedIssues": len([issue for issue in summary["issues"] if issue["severity"] == "blocked"]),
            "attentionIssues": len([issue for issue in summary["issues"] if issue["severity"] == "attention"]),
            "readySources": len([source for source in summary["sources"] if source["status"] == "ready"]),
            "dashboardUrl": summary["localAccess"]["dashboardUrl"],
            "apiBaseUrl": summary["localAccess"]["apiBaseUrl"],
            "authMode": summary["localAccess"]["authMode"],
            "remoteExposureSafe": summary["security"]["remoteExposureSafe"],
        }

    def _dependencies(self) -> List[Dict[str, Any]]:
        now = time.monotonic()
        with self._dependency_cache_lock:
            if (
                self._dependency_cache is not None
                and now - self._dependency_cache_checked_at < DEPENDENCY_CACHE_SECONDS
            ):
                return copy.deepcopy(self._dependency_cache)
            dependencies = self._discover_dependencies()
            self._dependency_cache = copy.deepcopy(dependencies)
            self._dependency_cache_checked_at = now
            return dependencies

    def _discover_dependencies(self) -> List[Dict[str, Any]]:
        tesseract_cmd = str(_config_value(self.config, "tesseract_cmd", "ocr_tesseract_cmd", default="tesseract"))
        tesseract_resolved = resolve_tesseract_command(self.config)
        configured_languages = configured_tesseract_languages(self.config)
        available_languages = available_tesseract_languages(self.config)
        missing_languages = sorted(set(configured_languages) - set(available_languages))
        google_connector_required = any(
            _truthy(_config_value(self.config, key, nested_key, default=False))
            for key, nested_key in (
                ("gmail_enabled", "gmail.enabled"),
                ("google_drive_enabled", "google_drive.enabled"),
                ("google_photos_enabled", "google_photos.enabled"),
            )
        ) or _truthy(_config_value(self.config, "gmail_scanner_mode", default=False))
        poppler_path = resolve_poppler_path(self.config)
        ml_model_value = str(
            _config_value(self.config, "ml_model_path", default="data/models/ml_categorizer_model.joblib")
        )
        ml_vectorizer_value = str(
            _config_value(self.config, "ml_vectorizer_path", default="data/models/tfidf_vectorizer.joblib")
        )
        ml_model_path = os.path.abspath(os.path.expanduser(os.path.expandvars(ml_model_value)))
        ml_vectorizer_path = os.path.abspath(os.path.expanduser(os.path.expandvars(ml_vectorizer_value)))
        ml_model_ready = os.path.isfile(ml_model_path) and os.path.isfile(ml_vectorizer_path)
        return [
            {
                "id": "python",
                "label": "Python",
                "status": "ok",
                "configured": True,
                "version": platform.python_version(),
                "details": f"{platform.system()} {platform.release()}",
            },
            {
                "id": "sqlite",
                "label": "SQLite",
                "status": "ok",
                "configured": True,
                "version": sqlite3.sqlite_version,
                "details": "Local operations ledger backend.",
            },
            _python_dependency("flask", "Flask", "Local dashboard/API"),
            _python_dependency("waitress", "Waitress", "Production local API server"),
            _python_dependency("pytesseract", "pytesseract", "Python wrapper for Tesseract OCR"),
            {
                "id": "tesseract",
                "label": "Tesseract executable",
                "status": "ok" if tesseract_resolved else "attention",
                "configured": bool(tesseract_cmd),
                "command": tesseract_cmd,
                "resolved": tesseract_resolved,
                "details": "Required for local OCR when Tesseract is selected.",
            },
            {
                "id": "tesseract_languages",
                "label": "Tesseract language data",
                "status": "ok" if available_languages and not missing_languages else "attention",
                "configured": bool(configured_languages),
                "configuredLanguages": configured_languages,
                "availableLanguages": available_languages,
                "missingLanguages": missing_languages,
                "details": "Dutch and English trained-data files are required for local bookkeeping OCR.",
            },
            _python_dependency("pdf2image", "pdf2image", "PDF page rendering before local OCR"),
            {
                "id": "poppler",
                "label": "Poppler PDF tools",
                "status": "ok" if poppler_path else "attention",
                "configured": bool(poppler_path),
                "resolved": poppler_path,
                "details": "Required to render PDF receipts before Tesseract OCR.",
            },
            _python_dependency("PIL", "Pillow", "Image loading for OCR processors"),
            _python_dependency(
                "sklearn",
                "scikit-learn",
                "Optional approved-feedback category model",
                required=False,
            ),
            _python_dependency(
                "joblib",
                "joblib",
                "Optional category model persistence",
                required=False,
            ),
            {
                "id": "category_model",
                "label": "Category model artifacts",
                "status": "ok" if ml_model_ready else "attention",
                "configured": ml_model_ready,
                "required": False,
                "modelPath": ml_model_path,
                "vectorizerPath": ml_vectorizer_path,
                "details": (
                    "Local model and vectorizer files are available; training provenance remains review-controlled."
                    if ml_model_ready
                    else "No approved model is trained yet; deterministic and approved vendor rules remain available."
                ),
            },
            _python_dependency(
                "googleapiclient",
                "Google API client",
                "Gmail, Drive, Photos, and Vision integrations",
                required=google_connector_required,
            ),
            _python_dependency(
                "playwright",
                "Playwright",
                "Optional supervised browser tooling",
                required=False,
            ),
        ]

    def _paths(self) -> Dict[str, Any]:
        backup_dir = str(
            _config_value(
                self.config,
                "fab_local_backup_dir",
                "operations_backup_dir",
                "backup_dir",
                default="data/backups",
            )
        )
        return {
            "ledger": _path_status("ledger", "Operations ledger", self.ledger_path, kind="file"),
            "backupDir": _path_status("backupDir", "Backup directory", backup_dir, kind="directory"),
            "mijngeldzakenExportDir": _path_status(
                "mijngeldzakenExportDir",
                "MijnGeldzaken supervised exports",
                _config_value(
                    self.config,
                    "mijngeldzaken_export_dir",
                    "operations_mijngeldzaken_export_dir",
                    default="data/exports/mijngeldzaken",
                ),
                kind="directory",
            ),
            "intake": [
                _path_status(f"intake_{index + 1}", f"Intake folder {index + 1}", path, kind="directory")
                for index, path in enumerate(self.intake_paths)
            ],
            "intakeExtensions": list(self.intake_extensions),
        }

    def _credentials(self) -> List[Dict[str, Any]]:
        gmail_required = _truthy(
            _config_value(self.config, "gmail_enabled", "gmail.enabled", default=False)
        ) or _truthy(_config_value(self.config, "gmail_scanner_mode", default=False))
        drive_required = _truthy(
            _config_value(
                self.config,
                "google_drive_enabled",
                "google_drive.enabled",
                default=False,
            )
        )
        photos_required = _truthy(
            _config_value(
                self.config,
                "google_photos_enabled",
                "google_photos.enabled",
                default=False,
            )
        )
        vision_required = _truthy(
            _config_value(
                self.config,
                "google_vision_enabled",
                "google_vision.enabled",
                default=False,
            )
        )
        return [
            _credential_file("gmail_credentials", "Gmail OAuth credentials", self.config, "gmail_credentials_file", "gmail.credentials_file", "gmail_credentials_path", required=gmail_required),
            _google_oauth_token_file("gmail_token", "Gmail OAuth token", self.config, "gmail_token_file", "gmail.token_file", "gmail_token_path", required=gmail_required),
            _credential_file("drive_credentials", "Google Drive OAuth credentials", self.config, "google_drive_credentials_file", "google_drive.credentials_file", "drive.credentials_file", "drive_credentials_path", required=drive_required),
            _google_oauth_token_file("drive_token", "Google Drive OAuth token", self.config, "google_drive_token_file", "google_drive.token_file", "drive.token_file", "drive_token_path", required=drive_required),
            _credential_file("photos_credentials", "Google Photos OAuth credentials", self.config, "google_photos_credentials_file", "google_photos.credentials_file", "photos.credentials_file", "photos_credentials_path", required=photos_required),
            _google_oauth_token_file("photos_token", "Google Photos Picker OAuth token", self.config, "google_photos_picker_token_file", "google_photos.picker_token_file", "google_photos_token_file", "google_photos.token_file", "photos.token_file", "photos_token_path", required=photos_required),
            _credential_file("vision_credentials", "Google Vision credentials", self.config, "google_vision_credentials_file", "google_vision.credentials_file", required=vision_required),
            _credential_value("freshdesk_api_key", "Freshdesk API key", self.config, "freshdesk_api_key", "freshdesk.api_key"),
            _credential_value("freshdesk_domain", "Freshdesk domain", self.config, "freshdesk_domain", "freshdesk.domain", secret=False),
            _credential_value("freshdesk_api_url", "Freshdesk API URL", self.config, "freshdesk_api_url", "freshdesk.api_url", secret=False),
            _credential_value("wave_business_token", "Waveapps Business token", self.config, "waveapps_business_access_token", "waveapps_business.access_token"),
            _credential_value("wave_business_id", "Waveapps Business ID", self.config, "waveapps_business_id", "waveapps_business.business_id", secret=False),
            _credential_value("wave_personal_token", "Waveapps Personal token", self.config, "waveapps_personal_access_token", "waveapps_personal.access_token"),
            _credential_value("wave_personal_id", "Waveapps Personal ID", self.config, "waveapps_personal_id", "waveapps_personal.personal_id", secret=False),
            _credential_value("mijngeldzaken_username", "Legacy MijnGeldzaken username (ignored)", self.config, "mijngeldzaken_username", "mijngeldzaken.username", secret=False),
            _credential_value("mijngeldzaken_password", "Legacy MijnGeldzaken password (ignored)", self.config, "mijngeldzaken_password", "mijngeldzaken.password"),
            _credential_value("banking_credentials", "Banking API credentials", self.config, "banking_api_credentials", "banking.api_credentials", "banking_api_client_secret", "banking.client_secret"),
            _credential_value("api_token", "FAB local API token", self.config, "fab_local_api_token", "fab_operations_api_token", "operations_api_token", "operations.api_token", "api_token"),
        ]

    def _sources(
        self,
        credentials: List[Dict[str, Any]],
        paths: Dict[str, Any],
        dependency_items: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        credential_map = {item["id"]: item for item in credentials}
        dependencies = {item["id"]: item for item in dependency_items}
        return [
            _source_status(
                "local_folder",
                "Local/scanner folders",
                configured=bool(paths["intake"]),
                ready=any(path["exists"] for path in paths["intake"]),
                details="Reads configured local_intake_paths into the local ledger.",
            ),
            _gmail_source(
                credential_map["gmail_credentials"],
                credential_map["gmail_token"],
                self.config,
            ),
            _drive_source(
                credential_map["drive_credentials"],
                credential_map["drive_token"],
                self.config,
            ),
            _photos_picker_source(
                credential_map["photos_credentials"],
                credential_map["photos_token"],
                enabled=_truthy(
                    _config_value(
                        self.config,
                        "google_photos_enabled",
                        "google_photos.enabled",
                        default=False,
                    )
                ),
            ),
            _freshdesk_source(
                credential_map["freshdesk_api_key"],
                credential_map["freshdesk_domain"],
                credential_map["freshdesk_api_url"],
            ),
            _source_status(
                "tesseract_ocr",
                "Tesseract OCR",
                configured=dependencies["pytesseract"]["configured"] or dependencies["tesseract"]["configured"],
                ready=all(
                    dependencies[item]["status"] == "ok"
                    for item in ("pytesseract", "tesseract", "tesseract_languages", "pdf2image", "poppler")
                ),
                details="Local OCR path for PDFs/images converted to images.",
            ),
            _mijngeldzaken_source(paths["mijngeldzakenExportDir"]),
            _pair_source(
                "waveapps_business",
                "Waveapps Business",
                credential_map["wave_business_token"],
                credential_map["wave_business_id"],
                details="API target for business bookkeeping drafts and approved exports.",
            ),
            _pair_source(
                "waveapps_personal",
                "Waveapps Personal",
                credential_map["wave_personal_token"],
                credential_map["wave_personal_id"],
                details="API target for personal or special-administration drafts and approved exports.",
            ),
            _source_status(
                "banking_api",
                "Banking API",
                configured=_has_value(self.config, "banking_api_endpoint") or credential_map["banking_credentials"]["configured"],
                ready=_has_value(self.config, "banking_api_endpoint") and credential_map["banking_credentials"]["configured"],
                details="Feeds reconciliation; missing credentials should keep matching manual/import-based.",
            ),
        ]

    def _security(self) -> Dict[str, Any]:
        host_is_loopback = self.api_host in LOOPBACK_HOSTS
        remote_blocked = not host_is_loopback and not self.api_token_configured
        secret_config_keys = _configured_secret_keys(self.config)
        return {
            "apiHost": self.api_host,
            "loopbackOnly": host_is_loopback,
            "apiTokenConfigured": bool(self.api_token_configured),
            "remoteExposureSafe": not remote_blocked,
            "remoteExposureBlocked": remote_blocked,
            "secretValuesRedacted": True,
            "configuredSecretKeys": secret_config_keys,
        }

    def _local_access(self, security: Dict[str, Any]) -> Dict[str, Any]:
        ledger_dashboard_url = self.base_url or f"http://{self.api_host}:{self.api_port}/"
        api_origin = ledger_dashboard_url.rstrip("/")
        dashboard_url = self.operator_dashboard_url
        dashboard_source = "configured_operator_dashboard" if dashboard_url else "ledger_api"
        if not dashboard_url:
            runtime_dashboard_url = self._runtime_operator_dashboard_url(api_origin)
            if runtime_dashboard_url:
                dashboard_url = runtime_dashboard_url
                dashboard_source = "verified_launcher_runtime"
        dashboard_url = dashboard_url or ledger_dashboard_url
        api_base_url = api_origin + "/api"
        token_required = bool(self.api_token_configured)
        ngrok_ready = bool(token_required and security["remoteExposureSafe"])
        return {
            "dashboardUrl": dashboard_url,
            "dashboardSource": dashboard_source,
            "ledgerDashboardUrl": ledger_dashboard_url,
            "apiBaseUrl": api_base_url,
            "authMode": "bearer_token_or_dashboard_login" if token_required else "loopback_no_token",
            "authHeaderRequired": token_required,
            "authHeaderExample": "Authorization: Bearer <FAB_LOCAL_API_TOKEN>" if token_required else "",
            "ngrokReady": ngrok_ready,
            "ngrokSafety": (
                "safe_with_token" if ngrok_ready
                else "blocked_without_token" if security["remoteExposureBlocked"]
                else "loopback_only"
            ),
            "windows": {
                "startCommand": "python -m src.operations.local_api",
                "workingDirectory": "019-FAB repository root",
                "taskScheduler": "Run the start command from Windows Task Scheduler after logon; keep host on 127.0.0.1 unless a token is configured.",
                "recommendedEnvironment": [
                    "FAB_LOCAL_LEDGER_PATH",
                    "FAB_LOCAL_API_HOST",
                    "FAB_LOCAL_API_PORT",
                    "FAB_LOCAL_API_TOKEN",
                    "FAB_LOCAL_INTAKE_PATHS",
                ],
            },
            "safeRemoteChecklist": [
                "Set FAB_LOCAL_API_TOKEN before any ngrok or non-loopback exposure.",
                "Keep the dashboard on 127.0.0.1 for normal local use.",
                "Do not put credentials, tokens, or financial documents in Git-tracked folders.",
                "Use approval phrases for export, restore, and other irreversible actions.",
            ],
            "externalSubmission": "not_executed",
        }

    def _runtime_operator_dashboard_url(self, api_origin: str) -> str:
        if not os.path.isfile(self.runtime_path) or os.path.islink(self.runtime_path):
            return ""
        try:
            runtime_stat = os.stat(self.runtime_path)
            if runtime_stat.st_size > MAX_RUNTIME_METADATA_BYTES:
                return ""
            signature = (
                runtime_stat.st_mtime_ns,
                runtime_stat.st_size,
                api_origin,
                self.instance_root,
            )
        except OSError:
            return ""
        now = time.monotonic()
        with self._runtime_cache_lock:
            if (
                signature == self._runtime_cache_signature
                and now - self._runtime_cache_checked_at < RUNTIME_IDENTITY_CACHE_SECONDS
            ):
                return self._runtime_cache_dashboard_url
            dashboard_url = self._read_runtime_operator_dashboard_url(api_origin)
            self._runtime_cache_signature = signature
            self._runtime_cache_checked_at = now
            self._runtime_cache_dashboard_url = dashboard_url
            return dashboard_url

    def _read_runtime_operator_dashboard_url(self, api_origin: str) -> str:
        try:
            with open(self.runtime_path, "r", encoding="utf-8-sig") as handle:
                runtime = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return ""
        if not isinstance(runtime, dict):
            return ""
        runtime_root = str(runtime.get("root") or "").strip()
        if not runtime_root or not _same_path(runtime_root, self.instance_root):
            return ""
        runtime_api_origin = _normalized_service_origin(runtime.get("apiBaseUrl"), require_loopback=True)
        expected_api_origin = _normalized_service_origin(api_origin, require_loopback=True)
        if not runtime_api_origin or runtime_api_origin != expected_api_origin:
            return ""
        dashboard_url = _normalized_operator_dashboard_url(
            runtime.get("dashboardUrl"),
            require_loopback=True,
        )
        identity_url = _normalized_identity_url(
            runtime.get("webIdentityUrl"),
            require_loopback=True,
        )
        if not dashboard_url or not identity_url or _url_origin(dashboard_url) != _url_origin(identity_url):
            return ""
        identity = _fetch_runtime_identity(identity_url)
        if not isinstance(identity, dict):
            return ""
        if str(identity.get("service") or "") != "fab-operator-dashboard":
            return ""
        identity_api_origin = _normalized_service_origin(
            identity.get("localApiEndpoint"),
            require_loopback=True,
        )
        if identity_api_origin != expected_api_origin:
            return ""
        if str(identity.get("instanceId") or "") != local_instance_id(Path(self.instance_root)):
            return ""
        return dashboard_url

    def _issues(
        self,
        dependencies: List[Dict[str, Any]],
        paths: Dict[str, Any],
        credentials: List[Dict[str, Any]],
        sources: List[Dict[str, Any]],
        security: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        issues: List[Dict[str, Any]] = []
        if security["remoteExposureBlocked"]:
            issues.append({
                "severity": "blocked",
                "type": "remote_api_without_token",
                "message": "FAB refuses non-loopback API exposure without an API token.",
                "nextAction": "Set operations.api_token before using a tunnel or non-loopback host.",
            })
        for dependency in dependencies:
            if dependency["status"] != "ok" and dependency.get("required", True):
                issues.append({
                    "severity": "attention",
                    "type": "dependency_missing",
                    "entity": dependency["id"],
                    "message": f"{dependency['label']} is not ready.",
                    "nextAction": dependency["details"],
                })
        for path in [
            paths["ledger"],
            paths["backupDir"],
            paths["mijngeldzakenExportDir"],
            *paths["intake"],
        ]:
            if path["configured"] and not path["parentWritable"]:
                issues.append({
                    "severity": "blocked",
                    "type": "path_not_writable",
                    "entity": path["id"],
                    "message": f"{path['label']} is not writable.",
                    "nextAction": "Move this path to a private writable local folder.",
                })
            elif path["configured"] and not path["exists"] and path["kind"] == "directory":
                issues.append({
                    "severity": "attention",
                    "type": "path_missing",
                    "entity": path["id"],
                    "message": f"{path['label']} does not exist yet.",
                    "nextAction": "Create the folder or update the configured path.",
                })
        for credential in credentials:
            if credential.get("partial") and credential.get("required", False):
                issues.append({
                    "severity": "attention",
                    "type": "credential_path_missing",
                    "entity": credential["id"],
                    "message": f"{credential['label']} is configured but the file is missing.",
                    "nextAction": "Create the file, finish OAuth login, or update config.ini.",
                })
        if not any(source["status"] == "ready" for source in sources):
            issues.append({
                "severity": "attention",
                "type": "no_ready_sources",
                "message": "No document source is fully ready.",
                "nextAction": "Configure a local intake folder or finish one external source credential setup.",
            })
        return issues


def _python_dependency(
    module_name: str,
    label: str,
    details: str,
    *,
    required: bool = True,
) -> Dict[str, Any]:
    found = importlib.util.find_spec(module_name) is not None
    return {
        "id": module_name.lower(),
        "label": label,
        "status": "ok" if found else "attention",
        "configured": found,
        "required": required,
        "details": details,
    }


def _path_status(path_id: str, label: str, path: Any, kind: str) -> Dict[str, Any]:
    raw_path = str(path or "").strip()
    configured = bool(raw_path)
    resolved = os.path.abspath(os.path.expanduser(raw_path)) if configured else ""
    exists = os.path.exists(resolved) if configured else False
    parent = resolved if kind == "directory" and exists else _nearest_existing_parent(os.path.dirname(resolved))
    parent_writable = bool(configured and parent and os.path.isdir(parent) and os.access(parent, os.W_OK))
    return {
        "id": path_id,
        "label": label,
        "kind": kind,
        "configured": configured,
        "path": raw_path,
        "resolvedPath": resolved,
        "exists": exists,
        "parentWritable": parent_writable,
        "status": "ok" if configured and (exists or parent_writable) else "attention",
    }


def _nearest_existing_parent(path: str) -> str:
    current = path
    while current and not os.path.exists(current):
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return current


def _credential_file(
    identifier: str,
    label: str,
    config: Dict[str, Any],
    *keys: str,
    required: bool = False,
) -> Dict[str, Any]:
    value = _config_value(config, *keys)
    configured = value not in (None, "")
    path = str(value or "")
    exists = os.path.exists(os.path.expanduser(path)) if configured else False
    return {
        "id": identifier,
        "label": label,
        "kind": "file",
        "configured": configured,
        "exists": exists,
        "partial": configured and not exists,
        "required": required,
        "path": path if configured else "",
        "secret": True,
    }


def _google_oauth_token_file(
    identifier: str,
    label: str,
    config: Dict[str, Any],
    *keys: str,
    required: bool = False,
) -> Dict[str, Any]:
    value = _config_value(config, *keys)
    configured = value not in (None, "")
    if not configured:
        return {
            "id": identifier,
            "label": label,
            "kind": "file",
            "configured": False,
            "exists": False,
            "partial": False,
            "required": required,
            "path": "",
            "secret": True,
            "legacyTokenPresent": False,
            "reauthorizationRequired": False,
            "reauthorizationReason": None,
        }
    status = GoogleOAuthTokenStore(value, []).status()
    return {
        "id": identifier,
        "label": label,
        "kind": "file",
        "configured": True,
        "exists": status["tokenPresent"],
        "partial": not status["tokenPresent"],
        "required": required,
        "path": status["tokenPath"],
        "secret": True,
        "legacyTokenPresent": status["legacyTokenPresent"],
        "reauthorizationRequired": status["reauthorizationRequired"],
        "reauthorizationReason": status["reauthorizationReason"],
    }


def _credential_value(identifier: str, label: str, config: Dict[str, Any], *keys: str, secret: bool = True) -> Dict[str, Any]:
    configured = _has_value(config, *keys)
    return {
        "id": identifier,
        "label": label,
        "kind": "value",
        "configured": configured,
        "exists": configured,
        "partial": False,
        "source": "configured" if configured else "missing",
        "secret": secret,
    }


def _source_status(identifier: str, label: str, configured: bool, ready: bool, details: str) -> Dict[str, Any]:
    if ready:
        status = "ready"
    elif configured:
        status = "needs_attention"
    else:
        status = "not_configured"
    return {
        "id": identifier,
        "label": label,
        "configured": bool(configured),
        "status": status,
        "ready": bool(ready),
        "details": details,
    }


def _oauth_source(identifier: str, label: str, credentials: Dict[str, Any], token: Dict[str, Any]) -> Dict[str, Any]:
    configured = credentials["configured"] or token["configured"]
    ready = credentials["exists"] and token["exists"]
    source = _source_status(
        identifier,
        label,
        configured=configured,
        ready=ready,
        details="OAuth credentials and a token file are required for unattended fetches.",
    )
    if credentials["exists"] and not token["exists"]:
        source["status"] = "needs_auth"
        source["details"] = "Credentials exist; finish OAuth login to create the token file."
    return source


def _gmail_source(
    credentials: Dict[str, Any],
    token: Dict[str, Any],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    scanner_mode = _truthy(_config_value(config, "gmail_scanner_mode", default=False))
    label = "Gmail scanner inbox" if scanner_mode else "Gmail"
    source = _oauth_source("gmail", label, credentials, token)
    trusted_senders = [
        item.lower()
        for item in _list_config(config, "gmail_trusted_senders")
    ]
    reauthorization_required = bool(token.get("reauthorizationRequired"))
    source["scannerMode"] = scanner_mode
    source["trustedSenders"] = trusted_senders
    source["query"] = str(
        _config_value(config, "gmail_query", "gmail_search_query", default="has:attachment")
    )
    source["documentPolicy"] = "pdf_only_magic_verified" if scanner_mode else "configured_query"
    if scanner_mode and not trusted_senders:
        source["ready"] = False
        source["status"] = "needs_attention"
        source["details"] = "Scanner mode needs at least one exact trusted sender address."
    elif reauthorization_required:
        source["ready"] = False
        source["status"] = "needs_auth"
        source["details"] = (
            "Legacy pickle tokens are never loaded; complete fresh Gmail consent to create a safe JSON token."
            if token.get("reauthorizationReason") == "legacy_pickle_token_unsupported"
            else "OAuth client changed; complete fresh Gmail consent before unattended intake."
        )
    elif source["ready"] and scanner_mode:
        source["details"] = (
            "Read-only scanner intake accepts only PDF attachments from the configured trusted sender; "
            "the source email remains unchanged."
        )
    return source


def _photos_picker_source(
    credentials: Dict[str, Any],
    token: Dict[str, Any],
    *,
    enabled: bool,
) -> Dict[str, Any]:
    if not enabled:
        return {
            **_source_status(
                "google_photos",
                "Google Photos Picker",
                configured=False,
                ready=False,
                details="Optional supervised Picker intake is disabled.",
            ),
            "enabled": False,
            "status": "disabled",
        }
    source = _oauth_source(
        "google_photos",
        "Google Photos Picker",
        credentials,
        token,
    )
    source["enabled"] = True
    if token.get("legacyTokenPresent") and not token["exists"]:
        source["ready"] = False
        source["status"] = "needs_attention"
        source["details"] = "Picker tokens must be JSON; unsafe pickle token files are not loaded."
    elif source["ready"]:
        source["status"] = "supervision_required"
        source["details"] = (
            "Credentials are ready for a user-owned receipt selection session; background whole-library access is unavailable."
        )
    return source


def _mijngeldzaken_source(export_path: Dict[str, Any]) -> Dict[str, Any]:
    artifact_ready = export_path["status"] == "ok"
    source = _source_status(
        "mijngeldzaken",
        "MijnGeldzaken",
        configured=export_path["configured"],
        ready=False,
        details=(
            "FAB can prepare checksum-bound import artifacts. External submission "
            "requires a supervised user-owned session; stored passwords are ignored."
        ),
    )
    if artifact_ready:
        source["status"] = "supervision_required"
        source["localArtifactReady"] = True
    return source


def _drive_source(
    credentials: Dict[str, Any],
    token: Dict[str, Any],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    source = _oauth_source("google_drive", "Google Drive", credentials, token)
    reauthorization_required = bool(token.get("reauthorizationRequired"))
    folder_id = str(
        _config_value(
            config,
            "google_drive_folder_id",
            "google_drive.folder_id",
            "drive_folder_id",
            default="",
        )
        or ""
    ).strip()
    if reauthorization_required:
        source["ready"] = False
        source["status"] = "needs_authorization"
        source["details"] = (
            "Legacy pickle tokens are never loaded; complete fresh Google consent to create a safe JSON token."
            if token.get("reauthorizationReason") == "legacy_pickle_token_unsupported"
            else "OAuth client credentials changed; complete fresh Google consent before Drive sync resumes."
        )
    elif source["ready"] and not folder_id:
        source["ready"] = False
        source["status"] = "needs_attention"
        source["details"] = (
            "OAuth is ready, but an approved folder_id is required; FAB will not scan the whole Drive."
        )
    elif folder_id:
        source["details"] = "Read-only ingestion is restricted to the configured Drive folder_id."
    return source


def _pair_source(identifier: str, label: str, first: Dict[str, Any], second: Dict[str, Any], details: str) -> Dict[str, Any]:
    return _source_status(
        identifier,
        label,
        configured=first["configured"] or second["configured"],
        ready=first["configured"] and second["configured"],
        details=details,
    )


def _freshdesk_source(
    api_key: Dict[str, Any],
    domain: Dict[str, Any],
    api_url: Dict[str, Any],
) -> Dict[str, Any]:
    locator_configured = domain["configured"] or api_url["configured"]
    return _source_status(
        "freshdesk",
        "Freshdesk",
        configured=api_key["configured"] or locator_configured,
        ready=api_key["configured"] and locator_configured,
        details="Requires an API key plus either a domain or API URL.",
    )


def _overall_status(issues: List[Dict[str, Any]]) -> str:
    if any(issue["severity"] == "blocked" for issue in issues):
        return "blocked"
    if issues:
        return "attention"
    return "ok"


def _next_actions(issues: List[Dict[str, Any]], sources: List[Dict[str, Any]]) -> List[str]:
    actions = list(dict.fromkeys([issue["nextAction"] for issue in issues if issue.get("nextAction")]))
    if any(source["status"] == "ready" for source in sources):
        actions.append("Run folder intake or the configured workflow from the dashboard.")
    return actions[:8]


def _bounded_port(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 5001
    if parsed < 1 or parsed > 65535:
        return 5001
    return parsed


def _normalized_base_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text if text.endswith("/") else f"{text}/"


def _normalized_operator_dashboard_url(value: Any, *, require_loopback: bool = False) -> str:
    return _normalized_url(
        value,
        expected_path="/admin/operations",
        require_loopback=require_loopback,
    )


def _normalized_identity_url(value: Any, *, require_loopback: bool = False) -> str:
    return _normalized_url(
        value,
        expected_path="/api/fab/runtime",
        require_loopback=require_loopback,
    )


def _normalized_service_origin(value: Any, *, require_loopback: bool = False) -> str:
    return _normalized_url(value, expected_path="", require_loopback=require_loopback)


def _normalized_url(value: Any, *, expected_path: str, require_loopback: bool) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 2048:
        return ""
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        return ""
    hostname = str(parsed.hostname or "").lower()
    normalized_path = parsed.path.rstrip("/")
    path_matches = normalized_path == expected_path if expected_path else parsed.path in {"", "/"}
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or not path_matches
        or parsed.query
        or parsed.fragment
        or (parsed.scheme == "http" and hostname not in LOOPBACK_HOSTS)
        or (require_loopback and hostname not in LOOPBACK_HOSTS)
    ):
        return ""
    host = f"[{hostname}]" if ":" in hostname else hostname
    authority = f"{host}:{port}" if port is not None else host
    return f"{parsed.scheme}://{authority}{expected_path}"


def _url_origin(value: str) -> str:
    parsed = urlsplit(value)
    return f"{parsed.scheme}://{parsed.netloc}".lower()


def _same_path(first: str, second: str) -> bool:
    try:
        return os.path.normcase(os.path.realpath(os.path.abspath(first))) == os.path.normcase(
            os.path.realpath(os.path.abspath(second))
        )
    except (OSError, TypeError, ValueError):
        return False


def _fetch_runtime_identity(url: str) -> Optional[Dict[str, Any]]:
    try:
        request = Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "FAB-readiness/1"},
        )
        opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
        with opener.open(request, timeout=0.5) as response:
            payload = response.read(MAX_RUNTIME_IDENTITY_BYTES + 1)
        if len(payload) > MAX_RUNTIME_IDENTITY_BYTES:
            return None
        parsed = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _configured_secret_keys(config: Dict[str, Any]) -> List[str]:
    flattened = _flatten_keys(config)
    return sorted(
        key
        for key, value in flattened.items()
        if value not in (None, "") and any(marker in key.lower() for marker in SECRET_MARKERS)
    )


def _flatten_keys(config: Dict[str, Any]) -> Dict[str, Any]:
    flattened: Dict[str, Any] = {}
    for key, value in config.items():
        if isinstance(value, dict):
            for nested_key, nested_value in value.items():
                flattened[f"{key}_{nested_key}"] = nested_value
        else:
            flattened[key] = value
    return flattened


def _list_config(config: Dict[str, Any], *keys: str) -> List[str]:
    value = _config_value(config, *keys)
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw_items = value
    else:
        raw_items = str(value).replace("\n", ",").replace(";", ",").split(",")
    return [str(item).strip() for item in raw_items if str(item).strip()]


def _has_value(config: Dict[str, Any], *keys: str) -> bool:
    return _config_value(config, *keys) not in (None, "")


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _config_value(config: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = config.get(key)
        if value not in (None, ""):
            return value
    for key in keys:
        if "." in key:
            section, option = key.split(".", 1)
        elif "_" in key:
            section, option = key.split("_", 1)
        else:
            continue
        section_values = config.get(section)
        if isinstance(section_values, dict):
            value = section_values.get(option)
            if value not in (None, ""):
                return value
    return default
