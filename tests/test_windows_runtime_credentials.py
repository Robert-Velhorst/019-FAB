from unittest.mock import patch

import pytest

from src.security.windows_runtime_credentials import existing_api_credentials


API = "synthetic-operator-0123456789-ABCDEFGHIJK"
HAI = "synthetic-hai-9876543210-LMNOPQRSTUVWXYZ"


@pytest.mark.parametrize("alias", ["operations_api_token", "fab_operations_api_token", "fab_local_api_token"])
def test_configured_aliases_do_not_need_a_secret_store(alias):
    with patch("src.security.windows_runtime_credentials.LocalSecretStore") as store:
        result = existing_api_credentials({alias: API, "operations_hai_api_token": HAI})
    assert result == {"apiToken": API, "haiToken": HAI}
    store.assert_not_called()


def test_environment_aliases_take_precedence_over_ini_aliases():
    result = existing_api_credentials({
        "fab_local_api_token": API, "fab_operations_api_token": "old", "operations_api_token": "older",
        "fab_hai_api_token": HAI, "operations_hai_api_token": "old",
    })
    assert result == {"apiToken": API, "haiToken": HAI}


@pytest.mark.parametrize("profile", ["vm", "", None, [], {}])
def test_invalid_profiles_fail_cleanly(profile):
    with pytest.raises(ValueError, match="profile"):
        existing_api_credentials({"fab_deployment_profile": profile})


@pytest.mark.parametrize("value", [False, [API], {"secret": API}, "x" * 8193, API + "\n", API + "\u00e9"])
def test_invalid_explicit_tokens_are_rejected(value):
    with pytest.raises(ValueError):
        existing_api_credentials({"fab_local_api_token": value, "fab_hai_api_token": HAI})


@pytest.mark.parametrize("runtime", [None, [], {"operator_api_token": API}, {"operator_api_token": API, "hai_api_token": API}])
def test_incomplete_or_invalid_store_is_not_repaired(runtime):
    with patch("src.security.windows_runtime_credentials.LocalSecretStore") as store:
        store.return_value.load.return_value = {"runtime": runtime}
        with pytest.raises(ValueError):
            existing_api_credentials({})
    store.return_value.get_or_create_runtime_secret.assert_not_called()
    store.return_value.load.assert_called_once_with()


def test_two_missing_inputs_read_store_once():
    with patch("src.security.windows_runtime_credentials.LocalSecretStore") as store:
        store.return_value.load.return_value = {"runtime": {"operator_api_token": API, "hai_api_token": HAI}}
        assert existing_api_credentials({}) == {"apiToken": API, "haiToken": HAI}
    store.return_value.load.assert_called_once_with()
    store.return_value.get_or_create_runtime_secret.assert_not_called()


def test_windows_rejects_long_placeholder_without_store_fallback():
    with patch("src.security.windows_runtime_credentials.LocalSecretStore") as store:
        with pytest.raises(ValueError):
            existing_api_credentials({"fab_deployment_profile": "windows", "fab_local_api_token": "x" * 48, "fab_hai_api_token": HAI})
    store.assert_not_called()
