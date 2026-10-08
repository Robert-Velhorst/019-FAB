import json
import math

import pytest

from src.operations.drive_wave_delivery import _delivery_line_items, _line_item_reasons


@pytest.mark.parametrize("field", ["quantity", "unit_price", "amount", "tax_amount", "tax_rate", "confidence_score"])
@pytest.mark.parametrize("value", [True, False, "NaN", "Infinity", "bad", float("nan"), float("inf"), {}])
def test_invalid_numeric_line_fields_have_explicit_safe_projection(field, value):
    record = {"line_items": [{field: value}]}
    lines, invalid = _delivery_line_items(record)
    path = f"lineItems[0].{field}"
    assert lines[0][field] is None
    assert invalid == [path]
    assert _line_item_reasons(record) == [f"bookkeeping_line_item_invalid:{path}"]
    json.dumps(lines, allow_nan=False)
    assert record["line_items"][0][field] is value


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nested_metadata_nonfinite_numbers_are_reported_and_retained(value):
    record = {"line_items": [{"metadata": {"results": [{"confidence": value}]}}]}
    lines, invalid = _delivery_line_items(record)
    assert invalid == ["lineItems[0].metadata.results[0].confidence"]
    assert lines[0]["metadata"]["results"][0]["confidence"] is None
    assert not math.isfinite(record["line_items"][0]["metadata"]["results"][0]["confidence"])
    json.dumps(lines, allow_nan=False)


def test_valid_projection_preserves_precision_optional_values_and_text():
    record = {"line_items": [{"quantity": 0.123456789, "unit_price": "123.456789", "tax_amount": 0,
        "tax_rate": None, "metadata": {"notes": ["NaN", {"confidence": 0.999999}]}}]}
    lines, invalid = _delivery_line_items(record)
    assert invalid == []
    assert lines == record["line_items"]
    lines[0]["metadata"]["notes"][1]["confidence"] = 0
    assert record["line_items"][0]["metadata"]["notes"][1]["confidence"] == 0.999999


def test_missing_record_has_no_invented_line_items():
    assert _delivery_line_items(None) == ([], [])
