import json

import pytest

import reviewctl.cli as cli


@pytest.mark.parametrize(
    ("raw", "violations"),
    [
        (b"[]", ("receipt-object",)),
        (b"{", ("json-receipt",)),
        (b"{}", ("receipt-digest",)),
        (b"\xff", ("json-receipt",)),
        (b'{"value":NaN}', ("json-receipt",)),
        (b'{"receiptSchemaVersion":true}', ("receipt-schema-version",)),
        (
            b'{"artifactKind":"project-review-checkpoint"}',
            ("project-checkpoint-not-review-receipt",),
        ),
    ],
)
def test_byte_verifier_preserves_global_receipt_rules(raw, violations) -> None:
    assert cli.receipt_bytes_violations(raw) == violations


def test_byte_verifier_accepts_unchanged_legacy_digest_receipt() -> None:
    receipt = {"reviewId": "fixture", "result": "unavailable"}
    receipt["sha256"] = cli.sha256_bytes(cli.canonical_json(receipt))
    assert cli.receipt_bytes_violations(json.dumps(receipt).encode()) == ()


def test_byte_verifier_rejects_legacy_kiro_claim() -> None:
    receipt = {"transport": "kiro"}
    receipt["sha256"] = cli.sha256_bytes(cli.canonical_json(receipt))
    assert cli.receipt_bytes_violations(json.dumps(receipt).encode()) == ("backend-qualification",)
