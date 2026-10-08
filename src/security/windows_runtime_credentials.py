"""Read the Windows launcher's existing API credentials without provisioning."""

import configparser
import json
import sys
from typing import Any

from src.security.deployment_secrets import strong_secret
from src.security.local_secret_store import LocalSecretStore, LocalSecretStoreError


def existing_api_credentials(config: dict[str, Any]) -> dict[str, str]:
    profile = config.get("fab_deployment_profile", config.get("operations_deployment_profile", "local"))
    if profile not in ("local", "windows"):
        raise ValueError("Unsupported Windows deployment profile.")
    configured = {
        "apiToken": config.get("fab_local_api_token", config.get("fab_operations_api_token", config.get("operations_api_token", ""))),
        "haiToken": config.get("fab_hai_api_token", config.get("operations_hai_api_token", "")),
    }
    names = {"apiToken": "operator_api_token", "haiToken": "hai_api_token"}
    runtime = None
    result = {}
    for key, value in configured.items():
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise ValueError("Invalid configured API credential.")
        # Match Start-FAB's local compatibility fallback, but never create a secret.
        if not value or (profile == "local" and len(value) < 32):
            if runtime is None:
                runtime = LocalSecretStore(config).load().get("runtime", {})
                if not isinstance(runtime, dict):
                    raise ValueError("Invalid stored runtime credentials.")
            value = runtime.get(names[key])
        if (
            not isinstance(value, str)
            or not 32 <= len(value) <= 8192
            or any(ord(char) < 33 or ord(char) > 126 for char in value)
            or (profile == "windows" and not strong_secret(value))
        ):
            raise ValueError("Missing or invalid existing API credential.")
        result[key] = value
    if result["apiToken"] == result["haiToken"]:
        raise ValueError("Operator and HAI credentials must be different.")
    return result


def main() -> int:
    from src.config_loader import ConfigLoader

    try:
        credentials = existing_api_credentials(ConfigLoader("config/config.ini").get_all_config())
    except (OSError, ValueError, LocalSecretStoreError, configparser.Error):
        # The launcher captures stdout privately; failures must never echo inputs.
        print("FAB could not read existing API/HAI credentials. Check configured secrets and start FAB first.", file=sys.stderr)
        return 1
    print(json.dumps(credentials))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
