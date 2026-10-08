import configparser
import json
import os
from typing import Any, Dict
from src.security.deployment_secrets import SECRET_ENV_NAMES, read_secret

from src.security.local_secret_store import (
    apply_local_wave_settings,
    configure_local_secret_paths,
)

class ConfigLoader:
    """Load sectioned configuration and collision-safe flat runtime aliases."""

    def __init__(self, config_file: str = "config/config.ini"):
        self.config_file = config_file
        self.config = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        config_data: Dict[str, Any] = {}
        parser = configparser.ConfigParser()
        
        if os.path.exists(self.config_file):
            parser.read(self.config_file)
            for section in parser.sections():
                config_data[section] = {}
                for key, value in parser.items(section):
                    parsed_value = self._parse_value(value)
                    config_data[section][key] = parsed_value
                    # Preserve the first legacy unqualified alias. Later
                    # sections still receive collision-safe section aliases
                    # such as waveapps_api_url and operations_api_url.
                    config_data.setdefault(key, parsed_value)

        configure_local_secret_paths(config_data, self.config_file)
        apply_local_wave_settings(config_data, mutate=True)
        
        # Override with environment variables (e.g., for sensitive data)
        for key, value in os.environ.items():
            parsed_value = self._parse_value(value)
            if key.startswith("APP_"):
                # Example: APP_GMAIL_CLIENT_ID -> gmail.client_id
                parts = key[len("APP_"):].lower().split("_", 1)
                if len(parts) == 2:
                    section, option = parts
                    if section not in config_data:
                        config_data[section] = {}
                    config_data[section][option] = parsed_value
                    config_data[option] = parsed_value
                else:
                    config_data[key.lower()] = parsed_value # For single-level env vars
            elif key.startswith("FAB_"):
                if key.startswith("FAB_WAVEAPPS_BUSINESS_"):
                    self._set_section_env_alias(
                        config_data,
                        "waveapps_business",
                        key[len("FAB_WAVEAPPS_BUSINESS_"):].lower(),
                        parsed_value,
                    )
                elif key.startswith("FAB_WAVEAPPS_PERSONAL_"):
                    self._set_section_env_alias(
                        config_data,
                        "waveapps_personal",
                        key[len("FAB_WAVEAPPS_PERSONAL_"):].lower(),
                        parsed_value,
                    )
                elif key == "FAB_WAVEAPPS_DEFAULT_TARGET":
                    config_data["waveapps_default_target"] = parsed_value
                else:
                    # Example: FAB_LOCAL_LEDGER_PATH -> fab_local_ledger_path
                    config_data[key.lower()] = parsed_value

        for name in SECRET_ENV_NAMES:
            if not os.environ.get(name) and not os.environ.get(name + "_FILE"):
                continue
            secret = read_secret(name)
            if name.startswith("FAB_WAVEAPPS_"):
                section = "waveapps_business" if "_BUSINESS_" in name else "waveapps_personal"
                self._set_section_env_alias(config_data, section, "access_token", secret)
            else:
                config_data[name.lower()] = secret
        self._add_flat_aliases(config_data)
        return config_data

    @staticmethod
    def _parse_value(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        lowered = stripped.lower()
        if lowered in {"true", "yes", "on"}:
            return True
        if lowered in {"false", "no", "off"}:
            return False
        if lowered in {"none", "null"}:
            return None
        if stripped.startswith(("{", "[")):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                pass
        return stripped

    def _add_flat_aliases(self, config_data: Dict[str, Any]) -> None:
        """Expose section-qualified flat aliases for runtime service consumers."""
        for section, values in list(config_data.items()):
            if not isinstance(values, dict):
                continue
            for key, value in values.items():
                config_data.setdefault(f"{section}_{key}", value)
                if key.startswith(f"{section}_"):
                    config_data.setdefault(key, value)

    @staticmethod
    def _set_section_env_alias(
        config_data: Dict[str, Any],
        section: str,
        option: str,
        value: Any,
    ) -> None:
        section_values = config_data.setdefault(section, {})
        if not isinstance(section_values, dict):
            section_values = {}
            config_data[section] = section_values
        section_values[option] = value
        config_data[f"{section}_{option}"] = value

    def get(self, section: str, key: str, default: Any = None) -> Any:
        """Retrieves a configuration value."""
        return self.config.get(section, {}).get(key, default)

    def get_section(self, section: str) -> Dict[str, Any]:
        """Retrieves an entire configuration section."""
        return self.config.get(section, {})

    def get_all_config(self) -> Dict[str, Any]:
        """Returns the entire loaded configuration."""
        return self.config


