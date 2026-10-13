"""Bounded secret-file input shared by startup and container health probes."""

import os
import stat
from pathlib import Path
from typing import Mapping

SECRET_ENV_NAMES = (
    "FAB_LOCAL_API_TOKEN", "FAB_HAI_API_TOKEN",
    "FAB_WAVEAPPS_BUSINESS_ACCESS_TOKEN", "FAB_WAVEAPPS_PERSONAL_ACCESS_TOKEN",
)
MAX_API_TOKEN_BYTES = 8192


def valid_api_token(value: object) -> bool:
    return (isinstance(value, str) and 1 <= len(value) <= MAX_API_TOKEN_BYTES
            and all(33 <= ord(char) <= 126 for char in value))


def read_secret(name: str, environment: Mapping[str, str] = None) -> str:
    environment = os.environ if environment is None else environment
    value = environment.get(name, "")
    filename = environment.get(name + "_FILE", "")
    if not filename:
        return value
    if value:
        raise ValueError(f"Configure only {name} or {name}_FILE, not both.")
    try:
        path = Path(filename)
        if not path.is_absolute() or not stat.S_ISREG(path.stat().st_mode):
            raise ValueError()
        # Keep the early check for Windows special paths, then validate the
        # actual opened descriptor so replacement cannot bypass file checks.
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(path, flags), "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > MAX_API_TOKEN_BYTES:
                raise ValueError()
            raw = handle.read(MAX_API_TOKEN_BYTES + 1)
        value = raw.decode("utf-8")
        if value.endswith("\r\n"):
            value = value[:-2]
        elif value.endswith("\n"):
            value = value[:-1]
        if len(raw) > MAX_API_TOKEN_BYTES or not valid_api_token(value):
            raise ValueError()
    except (OSError, ValueError, UnicodeError):
        raise ValueError(f"{name}_FILE must reference a readable, bounded UTF-8 secret file.") from None
    return value


def strong_secret(value: object) -> bool:
    """Reject obvious placeholders; this is not an entropy measurement."""
    if not valid_api_token(value) or len(value) < 32:
        return False
    lowered = value.lower()
    return (
        len(set(value)) >= 12
        and not any(marker in lowered for marker in (
            "changeme", "change-me", "replace-me", "placeholder", "example", "your-secret", "<", ">",
        ))
    )
