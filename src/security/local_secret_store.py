from __future__ import annotations

import base64
import ctypes
import json
import os
import secrets
import tempfile
from copy import deepcopy
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet, InvalidToken


STORE_VERSION = 1
MAX_STORE_BYTES = 1024 * 1024
SUPPORTED_WAVE_TARGETS = {"waveapps_business", "waveapps_personal"}
WAVE_SETTING_FIELDS = {
    "access_token",
    "business_id",
    "anchor_account_id",
    "default_category_account_id",
    "category_account_ids",
}
LOCAL_WAVE_MANAGED_FIELDS_KEY = "_fab_local_wave_secret_fields"
SUPPORTED_RUNTIME_SECRETS = {
    "web_jwt_secret",
    "operator_api_token",
    "hai_api_token",
}


class LocalSecretStoreError(RuntimeError):
    pass


def protect_with_current_user(content: bytes) -> bytes:
    return _windows_crypt(content, protect=True)


def unprotect_with_current_user(content: bytes) -> bytes:
    return _windows_crypt(content, protect=False)


class LocalSecretStore:
    """Encrypted local settings with a Windows-user-bound key where available."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.store_path = _configured_path(self.config, "fab_local_secret_store_path")
        self.key_path = _configured_path(self.config, "fab_local_secret_key_path")
        if not self.store_path or not self.key_path:
            raise LocalSecretStoreError("Local secret-store paths are not configured.")

    def load(self) -> Dict[str, Any]:
        if not os.path.isfile(self.store_path):
            return _empty_store()
        try:
            if os.path.getsize(self.store_path) > MAX_STORE_BYTES:
                raise LocalSecretStoreError("Local secret store exceeds the size limit.")
            with open(self.store_path, "rb") as handle:
                ciphertext = handle.read()
            plaintext = Fernet(self._load_key(create=False)).decrypt(ciphertext)
            payload = json.loads(plaintext.decode("utf-8"))
        except LocalSecretStoreError:
            raise
        except (InvalidToken, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LocalSecretStoreError("Local secret store could not be decrypted.") from exc
        if not isinstance(payload, dict) or payload.get("version") != STORE_VERSION:
            raise LocalSecretStoreError("Local secret store has an unsupported format.")
        wave = payload.get("wave")
        if not isinstance(wave, dict):
            raise LocalSecretStoreError("Local secret store is missing its Wave settings section.")
        return payload

    def update_wave_target(
        self,
        target_system: str,
        updates: Dict[str, Any],
        *,
        clear_access_token: bool = False,
    ) -> Dict[str, Any]:
        target = _wave_target(target_system)
        normalized = _normalize_wave_updates(updates)
        payload = self.load()
        wave = payload.setdefault("wave", {})
        current = dict(wave.get(target) or {})
        current.update(normalized)
        if clear_access_token:
            # Persist a tombstone so other long-running FAB processes clear any
            # previously loaded token on their next settings refresh.
            current["access_token"] = None
        wave[target] = current
        self._save(payload)
        return self.public_wave_status(target)

    def public_wave_status(self, target_system: str = "waveapps_business") -> Dict[str, Any]:
        target = _wave_target(target_system)
        payload = self.load()
        settings = (payload.get("wave") or {}).get(target) or {}
        return {
            "targetSystem": target,
            "storePresent": os.path.isfile(self.store_path),
            "keyPresent": os.path.isfile(self.key_path),
            "encryptedAtRest": True,
            "keyProtector": self.key_protector,
            "accessTokenStored": bool(settings.get("access_token")),
            "storedFields": sorted(key for key in settings if key != "access_token"),
        }

    def get_or_create_runtime_secret(self, name: str) -> str:
        secret_name = str(name or "").strip()
        if secret_name not in SUPPORTED_RUNTIME_SECRETS:
            raise ValueError("Unsupported FAB runtime secret.")
        payload = self.load()
        runtime = payload.setdefault("runtime", {})
        if not isinstance(runtime, dict):
            raise LocalSecretStoreError("Local secret store has an invalid runtime section.")
        current = runtime.get(secret_name)
        if current is not None:
            value = _bounded_text(current, secret_name, 512)
            if len(value) < 32:
                raise LocalSecretStoreError("Stored FAB runtime secret is too short.")
            return value
        value = secrets.token_urlsafe(48)
        runtime[secret_name] = value
        self._save(payload)
        return value

    @property
    def key_protector(self) -> str:
        if not os.path.isfile(self.key_path):
            return "windows_dpapi_current_user" if os.name == "nt" else "file_permissions"
        try:
            envelope = _read_json_file(self.key_path, MAX_STORE_BYTES)
        except (OSError, ValueError, json.JSONDecodeError):
            return "invalid"
        return str(envelope.get("protector") or "invalid")

    def _save(self, payload: Dict[str, Any]) -> None:
        serialized = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        if len(serialized) > MAX_STORE_BYTES:
            raise LocalSecretStoreError("Local secret store exceeds the size limit.")
        ciphertext = Fernet(self._load_key(create=True)).encrypt(serialized)
        _atomic_private_write(self.store_path, ciphertext)

    def _load_key(self, *, create: bool) -> bytes:
        if not os.path.isfile(self.key_path):
            if not create:
                raise LocalSecretStoreError("Local secret-store key is missing.")
            key = Fernet.generate_key()
            self._write_key(key)
            return key
        try:
            envelope = _read_json_file(self.key_path, 64 * 1024)
            if envelope.get("version") != STORE_VERSION:
                raise LocalSecretStoreError("Local secret-store key has an unsupported format.")
            protected_key = base64.b64decode(str(envelope.get("protectedKey") or ""), validate=True)
            protector = str(envelope.get("protector") or "")
            if protector == "windows_dpapi_current_user":
                key = _windows_unprotect(protected_key)
            elif protector == "file_permissions":
                key = protected_key
            else:
                raise LocalSecretStoreError("Local secret-store key protector is unsupported.")
            Fernet(key)
            return key
        except LocalSecretStoreError:
            raise
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise LocalSecretStoreError("Local secret-store key could not be loaded.") from exc

    def _write_key(self, key: bytes) -> None:
        protector = "windows_dpapi_current_user" if os.name == "nt" else "file_permissions"
        protected_key = _windows_protect(key) if os.name == "nt" else key
        envelope = {
            "version": STORE_VERSION,
            "protector": protector,
            "protectedKey": base64.b64encode(protected_key).decode("ascii"),
        }
        _atomic_private_write(
            self.key_path,
            json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("ascii"),
        )


def configure_local_secret_paths(config: Dict[str, Any], config_file: str) -> None:
    config_path = os.path.abspath(os.path.expanduser(str(config_file)))
    config_dir = os.path.dirname(config_path)
    root = os.path.dirname(config_dir) if os.path.basename(config_dir).lower() == "config" else config_dir
    credentials_dir = os.path.join(root, "credentials")
    config.setdefault(
        "fab_local_secret_store_path",
        os.path.join(credentials_dir, "fab-local-secrets.enc"),
    )
    config.setdefault(
        "fab_local_secret_key_path",
        os.path.join(credentials_dir, "fab-local-secrets.key"),
    )


def apply_local_wave_settings(
    config: Optional[Dict[str, Any]],
    *,
    mutate: bool = False,
) -> Dict[str, Any]:
    source = config or {}
    result = source if mutate else deepcopy(source)
    if not _configured_path(result, "fab_local_secret_store_path"):
        return result
    previously_managed = _managed_wave_fields(result)
    try:
        payload = LocalSecretStore(result).load()
    except LocalSecretStoreError as exc:
        _clear_managed_wave_settings(result, previously_managed)
        result["fab_local_secret_store_error"] = str(exc)
        return result
    result.pop("fab_local_secret_store_error", None)
    currently_managed: Dict[str, set[str]] = {}
    for target, settings in (payload.get("wave") or {}).items():
        if target not in SUPPORTED_WAVE_TARGETS or not isinstance(settings, dict):
            continue
        currently_managed[target] = {
            field
            for field in settings
            if field in WAVE_SETTING_FIELDS and not _environment_overrides(target, field)
        }
    for target in SUPPORTED_WAVE_TARGETS:
        removed_fields = previously_managed.get(target, set()) - currently_managed.get(target, set())
        _clear_target_settings(result, target, removed_fields)
    for target, settings in (payload.get("wave") or {}).items():
        if target not in SUPPORTED_WAVE_TARGETS or not isinstance(settings, dict):
            continue
        _apply_target_settings(result, target, settings)
    result[LOCAL_WAVE_MANAGED_FIELDS_KEY] = {
        target: sorted(fields)
        for target, fields in currently_managed.items()
        if fields
    }
    return result


def _apply_target_settings(config: Dict[str, Any], target: str, settings: Dict[str, Any]) -> None:
    nested = config.get(target)
    if not isinstance(nested, dict):
        nested = {}
        config[target] = nested
    id_option = "business_id" if target == "waveapps_business" else "personal_id"
    flat_fields = {
        "access_token": f"{target}_access_token",
        "business_id": "waveapps_business_id" if target == "waveapps_business" else "waveapps_personal_id",
        "anchor_account_id": f"{target}_anchor_account_id",
        "default_category_account_id": f"{target}_default_category_account_id",
        "category_account_ids": f"{target}_category_account_ids",
    }
    for field, flat_key in flat_fields.items():
        if field not in settings or _environment_overrides(target, field):
            continue
        value = deepcopy(settings[field])
        config[flat_key] = value
        nested[field if field != "business_id" else id_option] = value
        if field == "business_id":
            nested["id"] = value


def _managed_wave_fields(config: Dict[str, Any]) -> Dict[str, set[str]]:
    raw = config.get(LOCAL_WAVE_MANAGED_FIELDS_KEY)
    if not isinstance(raw, dict):
        return {}
    return {
        target: {
            field
            for field in fields
            if field in WAVE_SETTING_FIELDS
        }
        for target, fields in raw.items()
        if target in SUPPORTED_WAVE_TARGETS and isinstance(fields, (list, tuple, set))
    }


def _clear_managed_wave_settings(
    config: Dict[str, Any],
    managed_fields: Dict[str, set[str]],
) -> None:
    for target, fields in managed_fields.items():
        _clear_target_settings(config, target, fields)


def _clear_target_settings(config: Dict[str, Any], target: str, fields: set[str]) -> None:
    if not fields:
        return
    nested = config.get(target)
    id_option = "business_id" if target == "waveapps_business" else "personal_id"
    flat_fields = {
        "access_token": f"{target}_access_token",
        "business_id": "waveapps_business_id" if target == "waveapps_business" else "waveapps_personal_id",
        "anchor_account_id": f"{target}_anchor_account_id",
        "default_category_account_id": f"{target}_default_category_account_id",
        "category_account_ids": f"{target}_category_account_ids",
    }
    for field in fields:
        if field not in flat_fields or _environment_overrides(target, field):
            continue
        config[flat_fields[field]] = None
        if isinstance(nested, dict):
            nested[field if field != "business_id" else id_option] = None
            if field == "business_id":
                nested["id"] = None


def _environment_overrides(target: str, field: str) -> bool:
    suffix = {
        "access_token": "ACCESS_TOKEN",
        "business_id": "ID",
        "anchor_account_id": "ANCHOR_ACCOUNT_ID",
        "default_category_account_id": "DEFAULT_CATEGORY_ACCOUNT_ID",
        "category_account_ids": "CATEGORY_ACCOUNT_IDS",
    }[field]
    prefix = "FAB_WAVEAPPS_BUSINESS_" if target == "waveapps_business" else "FAB_WAVEAPPS_PERSONAL_"
    return os.environ.get(f"{prefix}{suffix}") not in (None, "")


def _normalize_wave_updates(updates: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(updates, dict):
        raise ValueError("Wave settings must be an object.")
    unexpected = sorted(set(updates) - WAVE_SETTING_FIELDS)
    if unexpected:
        raise ValueError(f"Unsupported Wave setting(s): {', '.join(unexpected)}")
    normalized: Dict[str, Any] = {}
    for key, value in updates.items():
        if key == "category_account_ids":
            if not isinstance(value, dict) or len(value) > 250:
                raise ValueError("categoryAccountIds must be an object with at most 250 entries.")
            mapping = {}
            for category, account_id in value.items():
                category_text = _bounded_text(category, "category", 255)
                account_text = _bounded_text(account_id, "account ID", 255)
                mapping[category_text] = account_text
            normalized[key] = mapping
            continue
        if value in (None, ""):
            normalized[key] = None
            continue
        limit = 16_384 if key == "access_token" else 255
        normalized[key] = _bounded_text(value, key, limit)
    return normalized


def _bounded_text(value: Any, label: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} must not be empty.")
    if len(text) > limit:
        raise ValueError(f"{label} must be at most {limit} characters.")
    return text


def _empty_store() -> Dict[str, Any]:
    return {"version": STORE_VERSION, "wave": {}}


def _wave_target(value: str) -> str:
    target = str(value or "").strip()
    if target not in SUPPORTED_WAVE_TARGETS:
        raise ValueError("targetSystem must be waveapps_business or waveapps_personal.")
    return target


def _configured_path(config: Dict[str, Any], key: str) -> str:
    value = config.get(key)
    return os.path.abspath(os.path.expanduser(str(value))) if value not in (None, "") else ""


def _read_json_file(path: str, maximum_bytes: int) -> Dict[str, Any]:
    if os.path.getsize(path) > maximum_bytes:
        raise ValueError("File exceeds size limit.")
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("JSON root must be an object.")
    return payload


def _atomic_private_write(path: str, content: bytes) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=".fab-secret-",
        suffix=".tmp",
        dir=directory,
        delete=False,
    )
    temporary_path = handle.name
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary_path, 0o600)
        except OSError:
            pass
        os.replace(temporary_path, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _windows_protect(content: bytes) -> bytes:
    return protect_with_current_user(content)


def _windows_unprotect(content: bytes) -> bytes:
    return unprotect_with_current_user(content)


def _windows_crypt(content: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        raise LocalSecretStoreError("Windows DPAPI is unavailable on this platform.")
    input_buffer = ctypes.create_string_buffer(content)
    input_blob = _DataBlob(
        len(content),
        ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    output_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if protect:
        succeeded = function(
            ctypes.byref(input_blob),
            "FAB local settings",
            None,
            None,
            None,
            0x1,
            ctypes.byref(output_blob),
        )
    else:
        succeeded = function(
            ctypes.byref(input_blob),
            None,
            None,
            None,
            None,
            0x1,
            ctypes.byref(output_blob),
        )
    if not succeeded:
        raise LocalSecretStoreError("Windows could not protect the local secret-store key.")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)
