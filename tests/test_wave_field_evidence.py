from decimal import Decimal

import pytest

from src.operations.drive_wave_delivery import _wave_field_matches


@pytest.mark.parametrize("field,expected,observed", [
    ("date", "not-a-date", "also-invalid"),
    ("date", "2026-02-30", "2026-02-30"),
    ("date", True, True),
    ("amount", "bad-amount", "another-bad-amount"),
    ("amount", "NaN", "NaN"),
    ("amount", "Infinity", "Infinity"),
    ("amount", float("inf"), float("inf")),
    ("taxAmount", Decimal("NaN"), Decimal("NaN")),
    ("taxAmount", "sNaN", "sNaN"),
    ("taxAmount", Decimal("sNaN"), Decimal("sNaN")),
    ("amount", True, False),
    ("amount", {}, []),
    ("vendor", " ", "\t"),
    ("currency", " ", "\t"),
    ("category", False, False),
    ("description", [], []),
    ("invoiceNumber", {"value": "INV-1"}, {"value": "INV-1"}),
], ids=["invalid-date", "impossible-date", "bool-date", "invalid-amount", "nan", "infinity",
        "float-infinity", "decimal-nan", "signaling-nan", "decimal-signaling-nan", "bool-amount", "container-amount",
        "blank-vendor", "blank-currency", "bool-category", "container-description", "container-invoice"])
def test_invalid_expected_and_observed_fields_never_match(field, expected, observed):
    assert _wave_field_matches(field, expected, observed) is False


@pytest.mark.parametrize("field,expected,observed", [
    ("amount", 121, "121.00"),
    ("amount", Decimal("-4.28"), "-4.280"),
    ("taxAmount", 0, "0.00"),
    ("date", "2026-07-22", "22/07/2026"),
    ("vendor", "Example Vendor", " example  vendor "),
    ("currency", "EUR", "eur"),
    ("invoiceNumber", "INV-1", "inv-1"),
])
def test_existing_valid_field_normalization_remains_supported(field, expected, observed):
    assert _wave_field_matches(field, expected, observed) is True
