import json
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from src.utils.runtime_identity import local_instance_id


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_INSPECTOR_RESPONSE_BYTES = 256 * 1024
DEFAULT_SHARED_INSPECTOR_URL = "http://127.0.0.1:4040/api/tunnels"


InspectorFetch = Callable[[str, float], Mapping[str, Any]]


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LocalCloudAccessService:
    """Report only project-owned, authenticated FAB ngrok runtime state."""

    def __init__(
        self,
        *,
        project_root: Optional[Path] = None,
        runtime_path: Optional[Path] = None,
        api_token_configured: bool = False,
        inspector_fetch: Optional[InspectorFetch] = None,
        inspector_timeout_seconds: float = 0.75,
        shared_inspector_url: Optional[str] = None,
    ) -> None:
        self.project_root = (project_root or Path(__file__).resolve().parents[2]).resolve()
        self.runtime_path = (
            runtime_path or self.project_root / "data" / "fab-ngrok-runtime.json"
        )
        self.api_token_configured = bool(api_token_configured)
        self.inspector_fetch = inspector_fetch or _fetch_inspector_json
        self.shared_inspector_url = _loopback_inspector_url(
            shared_inspector_url or DEFAULT_SHARED_INSPECTOR_URL
        )
        self.inspector_timeout_seconds = max(
            0.1,
            min(float(inspector_timeout_seconds), 3.0),
        )

    def summarize(self) -> Dict[str, Any]:
        if not self.runtime_path.is_file():
            endpoint_count = self._shared_endpoint_count()
            if self.api_token_configured and endpoint_count:
                return self._result(
                    status="endpoint_required",
                    blocker_code="ngrok_endpoint_already_active",
                    detected_endpoint_count=endpoint_count,
                    next_action=(
                        "Reserve a separate FAB HTTPS endpoint in ngrok, then run "
                        "Start-FAB-Ngrok.cmd -Url https://<fab-endpoint>."
                    ),
                )
            return self._result(
                status="not_running",
                next_action=(
                    "Run Start-FAB-Ngrok.cmd after FAB is running."
                    if self.api_token_configured
                    else "Configure a strong FAB API token, restart FAB, then run Start-FAB-Ngrok.cmd."
                ),
            )

        runtime = self._read_runtime()
        validated = self._validate_runtime(runtime)
        if validated is None:
            return self._result(
                status="invalid_runtime",
                next_action="Stop the managed FAB tunnel, inspect local runtime ownership, and start it again.",
            )

        if not self.api_token_configured:
            return self._result(
                status="invalid_runtime",
                next_action="Stop remote access, configure a strong FAB API token, and restart FAB before exposing it.",
                started_at=validated["startedAt"],
                verified_at=validated["verifiedAt"],
                local_api_base_url=validated["localApiBaseUrl"],
            )

        inspector_url = f"http://127.0.0.1:{validated['inspectorPort']}/api/tunnels"
        try:
            inspector = self.inspector_fetch(inspector_url, self.inspector_timeout_seconds)
        except Exception:
            inspector = {}

        matching_tunnel = next(
            (
                tunnel
                for tunnel in _tunnels(inspector)
                if _matches_runtime_tunnel(tunnel, validated)
            ),
            None,
        )
        if matching_tunnel is None:
            return self._result(
                status="stale",
                next_action="The recorded FAB endpoint is not active. Stop the stale tunnel state and start it again.",
                started_at=validated["startedAt"],
                verified_at=validated["verifiedAt"],
                local_api_base_url=validated["localApiBaseUrl"],
            )

        if validated["dashboardUrl"]:
            matching_dashboard = next(
                (
                    tunnel
                    for tunnel in _tunnels(inspector)
                    if _matches_dashboard_tunnel(tunnel, validated)
                ),
                None,
            )
            if matching_dashboard is None:
                return self._result(
                    status="stale",
                    next_action="The recorded FAB dashboard endpoint is not active. Stop the stale tunnel state and start it again.",
                    started_at=validated["startedAt"],
                    verified_at=validated["verifiedAt"],
                    local_api_base_url=validated["localApiBaseUrl"],
                )

        return self._result(
            status="active",
            active=True,
            public_url=validated["publicUrl"],
            dashboard_url=validated["dashboardUrl"],
            local_api_base_url=validated["localApiBaseUrl"],
            hai_manifest_url=validated["haiManifestUrl"],
            started_at=validated["startedAt"],
            verified_at=validated["verifiedAt"],
            next_action=(
                "Authenticated FAB API and HAI access are available; the operator dashboard is protected by ngrok Basic Auth."
                if validated["dashboardUrl"]
                else "Authenticated FAB API and HAI access are available through the verified HTTPS endpoint."
            ),
        )

    def _shared_endpoint_count(self) -> int:
        if not self.shared_inspector_url:
            return 0
        try:
            inspector = self.inspector_fetch(
                self.shared_inspector_url,
                self.inspector_timeout_seconds,
            )
        except Exception:
            return 0
        return sum(
            1
            for tunnel in _tunnels(inspector)
            if _https_origin(str(tunnel.get("public_url") or "")) is not None
        )

    def _read_runtime(self) -> Optional[Dict[str, Any]]:
        try:
            if self.runtime_path.stat().st_size > MAX_INSPECTOR_RESPONSE_BYTES:
                return None
            payload = json.loads(self.runtime_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _validate_runtime(self, runtime: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not runtime:
            return None
        try:
            root = Path(str(runtime.get("instanceRoot") or "")).resolve()
            public_url = _https_origin(str(runtime.get("publicUrl") or ""))
            dashboard_value = runtime.get("dashboardUrl")
            dashboard_url = _https_origin(str(dashboard_value)) if dashboard_value else None
            local_api_base_url = _loopback_http_origin(
                str(runtime.get("localApiBaseUrl") or "")
            )
            inspector_port = int(runtime.get("inspectorPort"))
            process_id = int(runtime.get("processId"))
            dashboard_port = int(runtime.get("dashboardPort")) if dashboard_url else None
        except (OSError, TypeError, ValueError):
            return None
        if not _same_path(root, self.project_root):
            return None
        if str(runtime.get("instanceId") or "") != local_instance_id(self.project_root):
            return None
        if (
            runtime.get("version") != 1
            or runtime.get("service") != "fab-ngrok-tunnel"
            or runtime.get("status") != "active"
        ):
            return None
        if runtime.get("authRequired") is not True:
            return None
        if public_url is None or local_api_base_url is None:
            return None
        if not 1024 <= inspector_port <= 65535 or process_id <= 0:
            return None
        if dashboard_value:
            if (
                dashboard_url is None
                or dashboard_url == public_url
                or runtime.get("dashboardAuthRequired") is not True
                or dashboard_port is None
                or not 1 <= dashboard_port <= 65535
            ):
                return None
        expected_manifest_url = f"{public_url}/api/hai/manifest"
        if str(runtime.get("haiManifestUrl") or "").rstrip("/") != expected_manifest_url:
            return None
        return {
            "publicUrl": public_url,
            "dashboardUrl": dashboard_url,
            "dashboardPort": dashboard_port,
            "localApiBaseUrl": local_api_base_url,
            "localApiPort": urlsplit(local_api_base_url).port,
            "inspectorPort": inspector_port,
            "haiManifestUrl": expected_manifest_url,
            "startedAt": _safe_timestamp(runtime.get("startedAt")),
            "verifiedAt": _safe_timestamp(runtime.get("verifiedAt")),
        }

    def _result(
        self,
        *,
        status: str,
        next_action: str,
        active: bool = False,
        public_url: Optional[str] = None,
        dashboard_url: Optional[str] = None,
        local_api_base_url: Optional[str] = None,
        hai_manifest_url: Optional[str] = None,
        started_at: Optional[str] = None,
        verified_at: Optional[str] = None,
        blocker_code: Optional[str] = None,
        detected_endpoint_count: int = 0,
    ) -> Dict[str, Any]:
        return {
            "service": "fab-ngrok-cloud-access",
            "status": status,
            "active": bool(active),
            "configured": self.api_token_configured,
            "publicUrl": public_url if active else None,
            "dashboardUrl": dashboard_url if active else None,
            "localApiBaseUrl": local_api_base_url,
            "authMode": "bearer_token" if self.api_token_configured else "blocked_without_token",
            "dashboardAuthMode": "ngrok_basic_auth" if active and dashboard_url else None,
            "haiManifestUrl": hai_manifest_url if active else None,
            "startedAt": started_at,
            "verifiedAt": verified_at,
            "blockerCode": blocker_code,
            "detectedEndpointCount": max(0, min(int(detected_endpoint_count), 100)),
            "nextAction": next_action,
            "externalSubmission": "not_executed",
        }


def _fetch_inspector_json(url: str, timeout_seconds: float) -> Mapping[str, Any]:
    request = Request(url, headers={"Accept": "application/json"})
    opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
    with opener.open(request, timeout=timeout_seconds) as response:
        payload = response.read(MAX_INSPECTOR_RESPONSE_BYTES + 1)
    if len(payload) > MAX_INSPECTOR_RESPONSE_BYTES:
        raise ValueError("ngrok inspector response exceeded the size limit")
    result = json.loads(payload.decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("ngrok inspector response must be an object")
    return result


def _tunnels(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = payload.get("tunnels") if isinstance(payload, Mapping) else None
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _matches_runtime_tunnel(tunnel: Mapping[str, Any], runtime: Mapping[str, Any]) -> bool:
    if str(tunnel.get("name") or "") != "fab-managed":
        return False
    public_url = _https_origin(str(tunnel.get("public_url") or ""))
    if public_url != runtime["publicUrl"]:
        return False
    config = tunnel.get("config")
    if not isinstance(config, Mapping):
        return False
    return _loopback_target_port(str(config.get("addr") or "")) == runtime["localApiPort"]


def _matches_dashboard_tunnel(tunnel: Mapping[str, Any], runtime: Mapping[str, Any]) -> bool:
    if str(tunnel.get("name") or "") != "fab-managed-dashboard":
        return False
    if _https_origin(str(tunnel.get("public_url") or "")) != runtime["dashboardUrl"]:
        return False
    config = tunnel.get("config")
    if not isinstance(config, Mapping):
        return False
    return _loopback_target_port(str(config.get("addr") or "")) == runtime["dashboardPort"]


def _https_origin(value: str) -> Optional[str]:
    try:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            return None
        return value.strip().rstrip("/")
    except ValueError:
        return None


def _loopback_http_origin(value: str) -> Optional[str]:
    try:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme.lower() != "http"
            or (parsed.hostname or "").lower() not in LOOPBACK_HOSTS
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.port is None
        ):
            return None
        return value.strip().rstrip("/")
    except ValueError:
        return None


def _loopback_target_port(value: str) -> Optional[int]:
    candidate = value.strip()
    if candidate.isdigit():
        port = int(candidate)
        return port if 1 <= port <= 65535 else None
    if "://" not in candidate:
        candidate = f"http://{candidate}"
    origin = _loopback_http_origin(candidate)
    return urlsplit(origin).port if origin else None


def _loopback_inspector_url(value: str) -> Optional[str]:
    try:
        parsed = urlsplit(str(value or "").strip())
        if (
            parsed.scheme.lower() != "http"
            or (parsed.hostname or "").lower() not in LOOPBACK_HOSTS
            or parsed.username
            or parsed.password
            or parsed.path.rstrip("/") != "/api/tunnels"
            or parsed.query
            or parsed.fragment
            or parsed.port is None
        ):
            return None
        return parsed.geturl()
    except ValueError:
        return None


def _same_path(left: Path, right: Path) -> bool:
    return str(left).replace("\\", "/").rstrip("/").casefold() == str(right).replace(
        "\\", "/"
    ).rstrip("/").casefold()


def _safe_timestamp(value: Any) -> Optional[str]:
    candidate = str(value or "").strip()
    return candidate[:80] if candidate else None
