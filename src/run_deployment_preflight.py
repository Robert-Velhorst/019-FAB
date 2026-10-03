"""Validate deployment configuration without creating or opening a ledger."""

import json
from src.config_loader import ConfigLoader
from src.operations.deployment_preflight import deployment_preflight


def main():
    try:
        result = deployment_preflight(ConfigLoader().get_all_config())
    except ValueError as error:
        result = {"status": "blocked", "issues": [{"message": str(error)}]}
    print(json.dumps(result, indent=2))
    return 1 if result["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
