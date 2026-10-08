import pytest

from src.operations.drive_wave_delivery import DriveWaveDeliveryService, _optional_int, _source_size


BAD_SIZES = [True, False, 17.9, 17.0, float("inf"), float("nan"), -1, 0,
             "1_7", "+17", "-17", "17.0", "NaN", "Infinity", "\u0661\u0667", 2**63]
BAD_IDS = ["true", "false", "fraction", "whole-float", "infinity", "nan", "negative", "zero",
           "underscore", "plus", "negative-string", "decimal-string", "nan-string", "infinity-string",
           "unicode-digits", "oversize"]


def document(size=17, local_size=17):
    return {"source_document_id": "synthetic-file", "original_filename": "invoice.pdf",
            "mime_type": "application/pdf", "metadata": {"sizeBytes": local_size,
                "providerMetadata": {"size": size}}}


@pytest.mark.parametrize("bad", BAD_SIZES, ids=BAD_IDS)
def test_bad_declared_source_size_cannot_fall_back_to_valid_local_size(bad):
    assert _source_size(document(bad)) is None


@pytest.mark.parametrize("bad", [value for value in BAD_SIZES if not isinstance(value, int) or isinstance(value, bool) or value != 0],
                         ids=[name for value, name in zip(BAD_SIZES, BAD_IDS) if not isinstance(value, int) or isinstance(value, bool) or value != 0])
def test_evidence_integer_does_not_coerce_malformed_values(bad):
    assert _optional_int(bad) is None


@pytest.mark.parametrize("provider,local,expected", [
    (17, 17, 17), ("17", 17, 17), (" 17 ", 17, 17), (17, "17", 17),
    (None, 17, 17), ("", 17, 17), (17, None, 17), (None, None, None),
    (17, 18, None), (17, True, None), (17, 17.5, None),
])
def test_source_size_requires_consistent_declared_values(provider, local, expected):
    assert _source_size(document(provider, local)) == expected


@pytest.mark.parametrize("bad", [None, "", True, 17.9, 17.0, float("inf"), "1_7", 18],
                         ids=["missing", "empty", "bool", "fraction", "whole-float", "infinity", "underscore", "changed"])
def test_current_provider_size_must_be_present_valid_and_exact(bad):
    service = DriveWaveDeliveryService(None)
    current = {"id": "synthetic-file", "name": "invoice.pdf", "mimeType": "application/pdf", "size": bad}
    with pytest.raises(RuntimeError):
        service._assert_provider_identity(document(), current)


@pytest.mark.parametrize("size", [17, "17", " 17 "])
def test_valid_current_provider_sizes_remain_supported(size):
    current = {"id": "synthetic-file", "name": "invoice.pdf", "mimeType": "application/pdf", "size": size}
    DriveWaveDeliveryService(None)._assert_provider_identity(document(), current)
