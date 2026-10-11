import hashlib
import json
from copy import copy, deepcopy
from dataclasses import replace

import pytest

import reviewctl.contracts as contracts_module
from reviewctl.contracts import (
    ContractContext,
    ContractEvaluation,
    EvaluationContext,
    EvaluationStatus,
    FragmentKind,
    PreparedContract,
    canonical_json,
    findings_required_fields,
    get_contract,
    has_exact_json_scalar_types,
    require_string_json_object_keys,
    valid_contract_context,
    valid_finding,
    valid_review_basename,
)


class TextSubclass(str):
    pass


class HostileText(str):
    def strip(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("hostile strip executed")

    def __eq__(self, other: object) -> bool:
        raise AssertionError("hostile equality executed")

    def __ne__(self, other: object) -> bool:
        raise AssertionError("hostile inequality executed")

    def __hash__(self) -> int:
        raise AssertionError("hostile hash executed")


class HostileDict(dict[str, object]):
    def __iter__(self):
        raise AssertionError("hostile dictionary iteration executed")


class HostileList(list[object]):
    pass


class HostileTuple(tuple[object, ...]):
    pass


class IntSubclass(int):
    pass


class FloatSubclass(float):
    pass


class HostileSchema(dict[str, object]):
    def values(self):
        raise AssertionError("hostile schema values executed")


class PreparedContractSubclass(PreparedContract):
    pass


class HostileContractContext(ContractContext):
    def __eq__(self, other: object) -> bool:
        raise AssertionError("hostile context equality executed")


@pytest.mark.parametrize(
    "name",
    [
        "source.py\0",
        "source.py\n",
        "source.py\t",
        "source\u202e.py",
        chr(0xD800),
    ],
)
def test_review_basename_rejects_non_printable_names(name: str) -> None:
    assert valid_review_basename(name) is False
    assert valid_contract_context(ContractContext(file_names=(name,))) is False


@pytest.mark.parametrize("name", ["café.py", "审计.py", "emoji-🧾.py"])
def test_review_basename_accepts_printable_unicode(name: str) -> None:
    assert name.isprintable()
    assert valid_review_basename(name) is True
    assert valid_contract_context(ContractContext(file_names=(name,))) is True


@pytest.mark.parametrize(
    "name",
    [None, b"source.py", TextSubclass("source.py"), "", "   ", ".", "..", "a/b", "a\\b"],
)
def test_review_basename_requires_an_exact_safe_basename(name: object) -> None:
    assert valid_review_basename(name) is False


@pytest.mark.parametrize("name", [" source.py", "source.py ", "\tsource.py"])
def test_review_basename_rejects_surrounding_whitespace(name: str) -> None:
    assert valid_review_basename(name) is False
    assert valid_contract_context(ContractContext(file_names=(name,))) is False


def test_contract_context_requires_exact_type_without_invoking_subclass_equality() -> None:
    context = HostileContractContext(file_names=("source.py",))

    assert valid_contract_context(context, require_file_names=True) is False


@pytest.mark.parametrize("name", ["source.py\0", "source.py\n", "source.py\t", "source\u202e.py"])
def test_findings_contract_rejects_unsafe_matching_scope_without_exception(name: str) -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=(name,))
    prepared = replace(
        contract.prepare(ContractContext(file_names=("source.py",))), file_names=(name,)
    )
    prepared = replace(
        prepared,
        digest=hashlib.sha256(canonical_json(prepared.identity_material)).hexdigest(),
    )

    evaluation = contract.evaluate(
        json.dumps(finding_payload(path=name)),
        prepared,
        context,
    )

    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.violations == ("prepared-contract",)
    assert evaluation.value is None


def test_findings_contract_accepts_printable_unicode_scope() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("revisión-🧾.py",))

    evaluation = contract.evaluate(
        json.dumps(finding_payload(path="revisión-🧾.py"), ensure_ascii=False),
        contract.prepare(context),
        context,
    )

    assert evaluation.status is EvaluationStatus.COMPLETE


def test_findings_dialect_changes_envelope_without_changing_finding_identity() -> None:
    from reviewctl.contracts import findings_contract_for_dialect

    current = get_contract("findings-json")
    legacy = findings_contract_for_dialect(None)
    context = ContractContext(file_names=("source.py",))
    payload = json.dumps(finding_payload())
    old = legacy.evaluate(payload, legacy.prepare(context), context)
    new = current.evaluate(payload, current.prepare(context), context)
    # Version 1 still identifies identical finding/fragment semantics. The explicit
    # dialect versions the response envelope and participates in prepared identity.
    assert old.version == new.version == "1"
    assert old.value == new.value
    assert old.valid_fragments == new.valid_fragments
    assert old.prepared_digest != new.prepared_digest
    assert legacy.prepare(context).dialect is None
    assert current.prepare(context).dialect == "optional-reviewed-files-v1"


def test_findings_dialect_resolution_rejects_unknown_identity() -> None:
    from reviewctl.contracts import (
        FindingsJsonContract,
        findings_contract_for_dialect,
        resolve_findings_prepared,
    )

    with pytest.raises(ValueError, match="unsupported findings dialect"):
        FindingsJsonContract(dialect="unknown")
    with pytest.raises(ValueError, match="unsupported findings dialect"):
        findings_contract_for_dialect("unknown")
    with pytest.raises(ValueError, match="unsupported prepared findings identity"):
        resolve_findings_prepared(ContractContext(file_names=("source.py",)), "a" * 64)


def test_findings_contract_prepares_a_stable_portable_contract() -> None:
    prepared = get_contract("findings-json").prepare(ContractContext())

    assert prepared.name == "findings-json"
    assert prepared.version == "1"
    assert prepared.schema["required"] == ["verdict", "findings"]
    assert "reviewedFiles" not in prepared.schema["properties"]
    assert prepared.digest == hashlib.sha256(canonical_json(prepared.identity_material)).hexdigest()
    assert "changes-requested" in prepared.output_instructions
    assert "approved" in prepared.output_instructions


def test_findings_contract_can_require_a_review_declaration_without_mutating_portable_schema() -> (
    None
):
    contract = get_contract("findings-json")

    declared = contract.prepare(
        ContractContext(file_names=("source.py",), review_declaration_required=True)
    )
    portable = contract.prepare(ContractContext(file_names=("source.py",)))

    assert declared.schema["required"] == ["verdict", "findings", "reviewedFiles"]
    assert declared.schema["properties"]["reviewedFiles"] == {
        "type": "array",
        "minItems": 1,
        "items": {"type": "string", "minLength": 1},
    }
    assert (
        portable.schema["properties"]["reviewedFiles"]
        == (declared.schema["properties"]["reviewedFiles"])
    )
    assert "reviewedFiles" not in portable.schema["required"]
    assert declared.digest != portable.digest


@pytest.mark.parametrize(
    "context",
    [
        ContractContext(file_names=("../source.py",)),
        ContractContext(file_names=("source.py",), review_declaration_required=1),
        ContractContext(file_names=(chr(0xD800),)),
    ],
)
def test_findings_contract_prepare_rejects_malformed_context(
    context: ContractContext,
) -> None:
    with pytest.raises(ValueError, match="invalid contract context"):
        get_contract("findings-json").prepare(context)


@pytest.mark.parametrize(
    "context",
    [
        ContractContext(file_names=("../source.py",)),
        ContractContext(file_names=("source.py",), review_declaration_required=1),
        ContractContext(file_names=(chr(0xD800),)),
    ],
)
def test_findings_contract_evaluate_rejects_malformed_context_without_exception(
    context: ContractContext,
) -> None:
    contract = get_contract("findings-json")
    base = contract.prepare(
        ContractContext(
            file_names=("source.py",),
            review_declaration_required=bool(context.review_declaration_required),
        )
    )
    if context.file_names == (chr(0xD800),):
        prepared = base
    else:
        prepared = replace(
            base,
            file_names=context.file_names,
            review_declaration_required=context.review_declaration_required,
        )
        prepared = replace(
            prepared,
            digest=hashlib.sha256(canonical_json(prepared.identity_material)).hexdigest(),
        )
    payload = {"verdict": "approved", "findings": []}
    if context.review_declaration_required:
        payload["reviewedFiles"] = ["source.py"]

    evaluation = contract.evaluate(json.dumps(payload), prepared, context)

    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.violations == ("prepared-contract",)
    assert evaluation.value is None


@pytest.mark.parametrize("prepared", [None, object()])
def test_findings_contract_evaluate_rejects_malformed_prepared_without_exception(
    prepared: object,
) -> None:
    evaluation = get_contract("findings-json").evaluate(
        json.dumps({"verdict": "approved", "findings": []}),
        prepared,  # type: ignore[arg-type]
        ContractContext(),
    )

    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.prepared_digest == ""
    assert evaluation.violations == ("prepared-contract",)


def test_findings_contract_evaluate_rejects_hostile_prepared_schema_without_exception() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = replace(contract.prepare(context), schema=HostileSchema())

    evaluation = contract.evaluate("{}", prepared, context)

    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.violations == ("prepared-contract",)


@pytest.mark.parametrize("constant", [float("nan"), float("inf"), float("-inf")])
def test_canonical_json_rejects_non_finite_numbers(constant: float) -> None:
    with pytest.raises(ValueError, match="JSON compliant"):
        canonical_json({"extension.example": constant})


def test_canonical_json_preserves_legitimate_json() -> None:
    assert canonical_json({"finite": 1.25, "items": [True, None]}) == (
        b'{"finite":1.25,"items":[true,null]}'
    )


def test_canonical_json_rejects_non_string_object_keys() -> None:
    with pytest.raises(ValueError, match="object keys must be strings"):
        canonical_json({"extension.example": {1: "hostile"}})


@pytest.mark.parametrize(
    "value",
    [HostileDict(), HostileList(), HostileTuple(("value",))],
)
def test_require_string_json_object_keys_rejects_container_subclasses(value: object) -> None:
    with pytest.raises(ValueError, match="unsupported JSON"):
        require_string_json_object_keys(value)


def test_require_string_json_object_keys_rejects_circular_values() -> None:
    value: list[object] = []
    value.append(value)

    with pytest.raises(ValueError, match="circular JSON value"):
        require_string_json_object_keys(value)


@pytest.mark.parametrize(
    "value",
    [
        TextSubclass("text"),
        IntSubclass(1),
        FloatSubclass(1.0),
        HostileDict(),
        HostileList(),
        HostileTuple(("value",)),
        {1: "non-string key"},
    ],
)
def test_has_exact_json_scalar_types_rejects_hostile_scalar_and_container_subclasses(
    value: object,
) -> None:
    assert has_exact_json_scalar_types(value) is False


def test_has_exact_json_scalar_types_ignores_repeated_containers() -> None:
    value: list[object] = []
    value.append(value)

    assert has_exact_json_scalar_types(value) is True


def test_findings_required_fields_requires_an_exact_boolean() -> None:
    with pytest.raises(ValueError, match="must be boolean"):
        findings_required_fields(1)  # type: ignore[arg-type]


def test_surrogate_dictionary_key_is_rejected() -> None:
    assert contracts_module._contains_surrogate({"\ud800": "value"}) is True


def test_findings_contract_stabilizes_prepared_serialization_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = get_contract("findings-json")
    context = ContractContext()
    prepared = contract.prepare(context)

    def fail_serialization(value: object) -> bytes:
        raise TypeError("serialization failed")

    monkeypatch.setattr(contracts_module, "canonical_json", fail_serialization)
    evaluation = contract.evaluate("{}", prepared, context)

    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.violations == ("prepared-contract",)


def test_findings_contract_stabilizes_payload_serialization_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = get_contract("findings-json")
    context = ContractContext()
    prepared = contract.prepare(context)
    original = contracts_module.canonical_json

    def fail_payload_serialization(value: object) -> bytes:
        if isinstance(value, dict) and value.get("verdict") == "approved":
            raise TypeError("serialization failed")
        return original(value)

    monkeypatch.setattr(contracts_module, "canonical_json", fail_payload_serialization)
    evaluation = contract.evaluate(
        json.dumps({"verdict": "approved", "findings": []}), prepared, context
    )

    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.violations == ("invalid-json",)


def test_findings_contract_rejects_surrogate_dictionary_key_after_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = get_contract("findings-json")
    context = ContractContext()
    prepared = contract.prepare(context)
    original = contracts_module.canonical_json

    def permit_surrogate_key(value: object) -> bytes:
        try:
            return original(value)
        except UnicodeError:
            return b"surrogate"

    monkeypatch.setattr(contracts_module, "canonical_json", permit_surrogate_key)
    evaluation = contract.evaluate(
        '{"verdict":"approved","findings":[],"\\ud800":"value"}',
        prepared,
        context,
    )

    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.violations == ("invalid-json",)


def test_findings_contract_normalizes_reviewed_files_to_authoritative_context_order() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("alpha.py", "beta.py"), review_declaration_required=True)
    prepared = contract.prepare(context)
    payload = json.dumps(
        {
            "verdict": "approved",
            "findings": [],
            "reviewedFiles": ["beta.py", "alpha.py"],
        }
    )

    evaluation = contract.evaluate(payload, prepared, context)

    assert evaluation.value == {
        "verdict": "approved",
        "findings": [],
        "reviewedFiles": ["alpha.py", "beta.py"],
    }


def test_contract_registry_rejects_unknown_contracts() -> None:
    try:
        get_contract("unknown")
    except KeyError as error:
        assert error.args == ("unknown",)
    else:
        raise AssertionError("unknown contract was accepted")


@pytest.mark.parametrize(
    "name",
    [
        None,
        b"findings-json",
        bytearray(b"findings-json"),
        [],
        {},
        1,
        True,
        object(),
        TextSubclass("findings-json"),
    ],
)
def test_contract_registry_rejects_non_text_names_deterministically(name: object) -> None:
    with pytest.raises(KeyError):
        get_contract(name)  # type: ignore[arg-type]


def finding_payload(**finding_overrides: object) -> dict[str, object]:
    finding = {
        "severity": "high",
        "path": "source.py",
        "line": 3,
        "title": "Duplicate effect",
        "evidence": "The same key reaches the write twice.",
        "reproduction": "Submit the same key twice.",
    }
    finding.update(finding_overrides)
    return {"verdict": "changes-requested", "findings": [finding]}


def test_valid_finding_requires_strict_canonical_serializability() -> None:
    legitimate = finding_payload()["findings"][0]

    assert valid_finding(legitimate) is True
    assert valid_finding({**legitimate, "line": 10**5000}) is False
    assert valid_finding({**legitimate, "title": chr(0xD800)}) is False


@pytest.mark.parametrize("field", ["severity", "path", "title", "evidence", "reproduction"])
def test_valid_finding_rejects_hostile_text_subclasses_without_invoking_them(
    field: str,
) -> None:
    value = finding_payload()["findings"][0]

    assert valid_finding({**value, field: HostileText(str(value[field]))}) is False


def test_valid_finding_rejects_dictionary_subclasses_before_iteration() -> None:
    value = HostileDict(finding_payload()["findings"][0])

    assert valid_finding(value) is False


def test_findings_contract_rejects_hostile_payload_subclass_without_invoking_it() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))

    evaluation = contract.evaluate(
        HostileText(json.dumps(finding_payload())),
        contract.prepare(context),
        context,
    )

    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.violations == ("invalid-json",)


@pytest.mark.parametrize("field", ["name", "version", "output_instructions", "digest"])
def test_findings_contract_rejects_hostile_prepared_strings_without_invoking_them(
    field: str,
) -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)
    hostile = replace(prepared, **{field: HostileText(str(getattr(prepared, field)))})

    evaluation = contract.evaluate("{}", hostile, context)

    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.violations == ("prepared-contract",)


def test_findings_contract_rejects_prepared_contract_subclasses() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)
    subclass = PreparedContractSubclass(**prepared.__dict__)

    evaluation = contract.evaluate("{}", subclass, context)

    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.violations == ("prepared-contract",)


@pytest.mark.parametrize("path", ["source.py\0", "source.py\n", "source.py\t", "source\u202e.py"])
def test_valid_finding_rejects_unsafe_paths(path: str) -> None:
    assert valid_finding(finding_payload(path=path)["findings"][0]) is False


def test_findings_contract_evaluates_and_hashes_a_normalized_value() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)
    payload = json.dumps(finding_payload(), indent=2)

    evaluation = contract.evaluate(payload, prepared, context)

    assert evaluation.value == finding_payload()
    assert evaluation.violations == ()
    assert evaluation.payload_digest == hashlib.sha256(payload.encode()).hexdigest()
    assert (
        evaluation.normalized_digest
        == hashlib.sha256(canonical_json(finding_payload())).hexdigest()
    )
    assert evaluation.status is EvaluationStatus.COMPLETE
    assert len(evaluation.valid_fragments) == 1
    assert evaluation.valid_fragments[0].kind is FragmentKind.FINDING
    assert evaluation.valid_fragments[0].value == finding_payload()["findings"][0]
    assert evaluation.valid_fragments[0].scope == ("source.py",)
    assert evaluation.coverage is not None
    assert evaluation.coverage.required_fields == ("verdict", "findings")
    assert evaluation.coverage.covered_fields == ("verdict", "findings")
    assert evaluation.coverage.missing_fields == ()
    assert evaluation.completion_request is None


@pytest.mark.parametrize(
    "payload",
    [
        None,
        b"{}",
        bytearray(b"{}"),
        [],
        {},
        1,
        True,
        object(),
        TextSubclass("{}"),
    ],
)
def test_findings_contract_rejects_non_text_payloads_deterministically(
    payload: object,
) -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)

    evaluation = contract.evaluate(payload, prepared, context)  # type: ignore[arg-type]

    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.violations == ("invalid-json",)
    assert evaluation.prepared_digest == prepared.digest
    assert evaluation.payload_digest == hashlib.sha256(b"").hexdigest()
    assert evaluation.normalized_digest is None
    assert evaluation.value is None
    assert evaluation.valid_fragments == ()
    assert evaluation.coverage is None
    assert evaluation.completion_request is None


def test_findings_contract_normalizes_a_required_review_declaration() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(
        file_names=("source.py", "test_source.py"), review_declaration_required=True
    )
    prepared = contract.prepare(context)
    value = finding_payload()
    value["reviewedFiles"] = [
        "/private/tmp/reviewctl-input-abc/source.py",
        "test_source.py",
    ]

    evaluation = contract.evaluate(json.dumps(value), prepared, context)

    assert evaluation.violations == ()
    assert evaluation.value == {
        **finding_payload(),
        "reviewedFiles": ["source.py", "test_source.py"],
    }
    assert evaluation.status is EvaluationStatus.COMPLETE
    assert evaluation.coverage is not None
    assert evaluation.coverage.required_fields == (
        "verdict",
        "findings",
        "reviewedFiles",
    )
    assert evaluation.coverage.covered_fields == (
        "verdict",
        "findings",
        "reviewedFiles",
    )


@pytest.mark.parametrize("reviewed_file", [1, " "])
def test_findings_contract_rejects_invalid_review_declaration_item(reviewed_file: object) -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",), review_declaration_required=True)
    payload = {"verdict": "approved", "findings": [], "reviewedFiles": [reviewed_file]}

    evaluation = contract.evaluate(json.dumps(payload), contract.prepare(context), context)

    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.violations == ("review-declaration",)


def test_findings_contract_rejects_duplicate_json_fields() -> None:
    contract = get_contract("findings-json")
    context = ContractContext()
    prepared = contract.prepare(context)

    evaluation = contract.evaluate(
        '{"verdict":"approved","verdict":"changes-requested","findings":[]}',
        prepared,
        context,
    )

    assert evaluation.value is None
    assert evaluation.normalized_digest is None
    assert evaluation.violations == ("invalid-json",)
    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.valid_fragments == ()
    assert evaluation.coverage is None
    assert evaluation.completion_request is None


def test_findings_contract_rejects_mutated_prepared_material() -> None:
    contract = get_contract("findings-json")
    context = ContractContext()
    prepared = contract.prepare(context)
    prepared.schema["additionalProperties"] = True

    evaluation = contract.evaluate(
        json.dumps({"verdict": "approved", "findings": []}), prepared, context
    )

    assert evaluation.value is None
    assert evaluation.violations == ("prepared-contract",)
    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.valid_fragments == ()


def test_findings_contract_rejects_a_prepared_contract_from_another_context() -> None:
    contract = get_contract("findings-json")
    prepared = contract.prepare(ContractContext())
    declaration_context = ContractContext(
        file_names=("source.py",), review_declaration_required=True
    )

    evaluation = contract.evaluate(
        json.dumps({"verdict": "approved", "findings": []}),
        prepared,
        declaration_context,
    )

    assert evaluation.value is None
    assert evaluation.violations == ("prepared-contract",)


def test_findings_contract_rejects_file_context_substitution() -> None:
    contract = get_contract("findings-json")
    prepared = contract.prepare(
        ContractContext(file_names=("source.py",), review_declaration_required=True)
    )
    substituted_context = ContractContext(
        file_names=("other.py",), review_declaration_required=True
    )

    evaluation = contract.evaluate(
        json.dumps({"verdict": "approved", "findings": [], "reviewedFiles": ["other.py"]}),
        prepared,
        substituted_context,
    )

    assert evaluation.value is None
    assert evaluation.violations == ("prepared-contract",)


def invalid_contract_cases() -> list[tuple[str, ContractContext, str]]:
    missing_field = finding_payload()
    del missing_field["findings"][0]["title"]  # type: ignore[index]
    extra_field = finding_payload(extra="not allowed")
    declaration = finding_payload()
    declaration["reviewedFiles"] = ["source.py", "source.py"]
    return [
        ("not json", ContractContext(), "invalid-json"),
        ("[]", ContractContext(), "top-level-not-object"),
        (
            json.dumps({"verdict": "approved", "findings": [], "extra": True}),
            ContractContext(),
            "response-fields",
        ),
        (
            json.dumps({"verdict": "unavailable", "findings": []}),
            ContractContext(),
            "verdict",
        ),
        (
            json.dumps({"verdict": "changes-requested", "findings": {}}),
            ContractContext(),
            "findings-shape",
        ),
        (json.dumps(missing_field), ContractContext(), "finding-fields"),
        (json.dumps(extra_field), ContractContext(), "finding-fields"),
        (json.dumps(finding_payload(title="  ")), ContractContext(), "finding-value"),
        (json.dumps(finding_payload(line=True)), ContractContext(), "finding-value"),
        (
            json.dumps(finding_payload(path="invented.py")),
            ContractContext(file_names=("source.py",)),
            "finding-path",
        ),
        (
            json.dumps({"verdict": "approved", "findings": finding_payload()["findings"]}),
            ContractContext(),
            "verdict-invariant",
        ),
        (
            json.dumps(declaration),
            ContractContext(file_names=("source.py",), review_declaration_required=True),
            "review-declaration",
        ),
        (
            json.dumps({"verdict": "approved", "findings": [], "reviewedFiles": ["../source.py"]}),
            ContractContext(file_names=("source.py",), review_declaration_required=True),
            "review-declaration",
        ),
        (
            json.dumps(
                {
                    "verdict": "approved",
                    "findings": [],
                    "reviewedFiles": ["unrelated/source.py"],
                }
            ),
            ContractContext(file_names=("source.py",), review_declaration_required=True),
            "review-declaration",
        ),
    ]


def test_findings_contract_reports_stable_semantic_violation_codes() -> None:
    contract = get_contract("findings-json")

    for payload, context, expected_code in invalid_contract_cases():
        prepared = contract.prepare(context)
        evaluation = contract.evaluate(payload, prepared, context)

        assert evaluation.value is None, expected_code
        assert evaluation.normalized_digest is None, expected_code
        assert evaluation.violations == (expected_code,)


@pytest.mark.parametrize(
    ("payload", "expected_violation"),
    [
        ('{"hostile":' + ("9" * 5000) + "}", "invalid-json"),
        (("[" * 2000) + "0" + ("]" * 2000), "top-level-not-object"),
    ],
)
def test_findings_contract_rejects_hostile_json_parser_limits(
    payload: str, expected_violation: str
) -> None:
    contract = get_contract("findings-json")
    context = ContractContext()

    evaluation = contract.evaluate(payload, contract.prepare(context), context)

    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.violations == (expected_violation,)


def test_contract_evaluation_additive_defaults_preserve_positional_compatibility() -> None:
    evaluation = ContractEvaluation(
        "findings-json",
        "1",
        "prepared",
        "payload",
        None,
        None,
        ("invalid-json",),
    )

    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.valid_fragments == ()
    assert evaluation.coverage is None
    assert evaluation.completion_request is None


def test_finding_fingerprint_ignores_payload_formatting_but_fragment_id_does_not() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)
    compact = json.dumps(finding_payload(), separators=(",", ":"))
    formatted = json.dumps(finding_payload(), indent=2)

    compact_fragment = contract.evaluate(compact, prepared, context).valid_fragments[0]
    formatted_fragment = contract.evaluate(formatted, prepared, context).valid_fragments[0]

    assert compact_fragment.fingerprint == formatted_fragment.fingerprint
    assert compact_fragment.fragment_id != formatted_fragment.fragment_id
    assert compact_fragment.payload_digest == hashlib.sha256(compact.encode()).hexdigest()
    expected_fingerprint = hashlib.sha256(
        canonical_json(
            {
                "contract": "findings-json",
                "version": "1",
                "kind": "finding",
                "value": finding_payload()["findings"][0],
                "scope": ["source.py"],
            }
        )
    ).hexdigest()
    assert compact_fragment.fingerprint == expected_fingerprint
    assert (
        compact_fragment.fragment_id
        == hashlib.sha256(
            canonical_json(
                {
                    "fingerprint": expected_fingerprint,
                    "payloadDigest": compact_fragment.payload_digest,
                }
            )
        ).hexdigest()
    )


def test_mixed_findings_extract_only_valid_siblings_and_request_completion() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)
    valid_finding = finding_payload()["findings"][0]
    invalid_finding = {**valid_finding, "severity": "urgent"}
    payload = json.dumps(
        {
            "verdict": "changes-requested",
            "findings": [invalid_finding, valid_finding, {**valid_finding, "path": "other.py"}],
        }
    )

    evaluation = contract.evaluate(
        payload,
        prepared,
        context,
        evidence=EvaluationContext(packet_digest="packet-sha256"),
    )

    assert evaluation.status is EvaluationStatus.INCOMPLETE
    assert evaluation.value is None
    assert evaluation.normalized_digest is None
    assert evaluation.violations == ("finding-value",)
    assert [fragment.value for fragment in evaluation.valid_fragments] == [valid_finding]
    assert evaluation.coverage is not None
    assert evaluation.coverage.required_fields == ("verdict", "findings")
    assert evaluation.coverage.covered_fields == ("verdict",)
    assert evaluation.coverage.missing_fields == ("findings",)
    assert not set(evaluation.coverage.covered_fields).intersection(
        evaluation.coverage.missing_fields
    )
    assert evaluation.completion_request is not None
    assert evaluation.completion_request.prepared_digest == prepared.digest
    assert evaluation.completion_request.packet_digest == "packet-sha256"
    assert evaluation.completion_request.missing_fields == ("findings",)
    assert evaluation.completion_request.invalid_fragment_indexes == (0, 2)
    assert evaluation.completion_request.violations == ("finding-value",)


def test_repeated_valid_findings_are_canonicalized_before_incomplete_evaluation() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    valid = finding_payload()["findings"][0]
    invalid = {**valid, "severity": "urgent"}

    evaluation = contract.evaluate(
        json.dumps({"verdict": "changes-requested", "findings": [valid, valid, invalid]}),
        contract.prepare(context),
        context,
        evidence=EvaluationContext(packet_digest="b" * 64),
    )

    assert evaluation.status is EvaluationStatus.INCOMPLETE
    assert evaluation.violations == ("finding-value",)
    assert len(evaluation.valid_fragments) == 1
    assert evaluation.valid_fragments[0].value == valid


def test_repeated_findings_are_canonicalized_in_complete_normalized_value() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    valid = finding_payload()["findings"][0]

    evaluation = contract.evaluate(
        json.dumps({"verdict": "changes-requested", "findings": [valid, valid]}),
        contract.prepare(context),
        context,
    )

    assert evaluation.status is EvaluationStatus.COMPLETE
    assert evaluation.value == {"verdict": "changes-requested", "findings": [valid]}
    assert len(evaluation.valid_fragments) == 1


@pytest.mark.parametrize("reverse", [False, True])
def test_distinct_fragments_are_emitted_in_canonical_id_order(reverse: bool) -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    findings = [
        finding_payload(line=1, title="Finding 0")["findings"][0],
        finding_payload(line=3, title="Finding 2")["findings"][0],
    ]
    if reverse:
        findings.reverse()

    evaluation = contract.evaluate(
        json.dumps({"verdict": "changes-requested", "findings": findings, "extra": True}),
        contract.prepare(context),
        context,
        evidence=EvaluationContext(packet_digest="b" * 64),
    )

    fragment_ids = [fragment.fragment_id for fragment in evaluation.valid_fragments]
    assert len(fragment_ids) == 2
    assert fragment_ids == sorted(fragment_ids)


@pytest.mark.parametrize(
    "invalid_finding",
    [
        {key: value for key, value in finding_payload()["findings"][0].items() if key != "title"},
        {**finding_payload()["findings"][0], "severity": "urgent"},
        {**finding_payload()["findings"][0], "path": "other.py"},
    ],
)
def test_finding_violations_require_a_covered_verdict(invalid_finding: dict[str, object]) -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    valid = finding_payload()["findings"][0]
    evaluation = contract.evaluate(
        json.dumps({"verdict": "approved", "findings": [invalid_finding, valid]}),
        contract.prepare(context),
        context,
    )

    assert evaluation.violations == ("verdict-invariant",)
    assert evaluation.coverage is not None
    assert evaluation.coverage.covered_fields == ()
    assert evaluation.coverage.missing_fields == ("verdict", "findings")


def test_valid_finding_can_survive_invalid_top_level_requirements() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",), review_declaration_required=True)
    prepared = contract.prepare(context)
    value = finding_payload()
    value["verdict"] = "unavailable"
    value["extra"] = True

    evaluation = contract.evaluate(json.dumps(value), prepared, context)

    assert evaluation.status is EvaluationStatus.INCOMPLETE
    assert len(evaluation.valid_fragments) == 1
    assert evaluation.violations == ("response-fields",)
    assert evaluation.coverage is not None
    assert evaluation.coverage.required_fields == (
        "verdict",
        "findings",
        "reviewedFiles",
    )
    assert evaluation.coverage.covered_fields == ()
    assert evaluation.coverage.missing_fields == (
        "verdict",
        "findings",
        "reviewedFiles",
    )
    assert evaluation.completion_request is not None
    assert evaluation.completion_request.invalid_fragment_indexes == ()


def test_verdict_invariant_with_a_valid_finding_is_incomplete_without_verdict_fragment() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)
    value = finding_payload()
    value["verdict"] = "approved"

    evaluation = contract.evaluate(json.dumps(value), prepared, context)

    assert evaluation.status is EvaluationStatus.INCOMPLETE
    assert evaluation.violations == ("verdict-invariant",)
    assert len(evaluation.valid_fragments) == 1
    assert all(fragment.kind is FragmentKind.FINDING for fragment in evaluation.valid_fragments)
    assert evaluation.coverage is not None
    assert evaluation.coverage.covered_fields == ()
    assert evaluation.coverage.missing_fields == ("verdict", "findings")


def test_invalid_findings_do_not_extract_or_make_response_incomplete() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)

    for overrides in (
        {"severity": "urgent"},
        {"path": "other.py"},
        {"title": ""},
        {"extra": "field"},
    ):
        evaluation = contract.evaluate(json.dumps(finding_payload(**overrides)), prepared, context)

        assert evaluation.status is EvaluationStatus.INVALID
        assert evaluation.valid_fragments == ()
        assert evaluation.completion_request is None


def test_missing_findings_and_approved_response_never_extract_fragments() -> None:
    contract = get_contract("findings-json")
    context = ContractContext()
    prepared = contract.prepare(context)

    for value in (
        {"verdict": "changes-requested"},
        {"verdict": "approved", "findings": []},
    ):
        evaluation = contract.evaluate(json.dumps(value), prepared, context)

        assert evaluation.valid_fragments == ()
        assert evaluation.completion_request is None
    assert (
        contract.evaluate(
            json.dumps({"verdict": "approved", "findings": []}), prepared, context
        ).status
        is EvaluationStatus.COMPLETE
    )


def test_exact_json_object_and_prepared_contract_are_required_before_extraction() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)
    finding = json.dumps(finding_payload()["findings"][0])
    payloads = (
        f'{{"verdict":"changes-requested","findings":[{finding}],"findings":[]}}',
        f"[{finding}]",
        f'{{"verdict":"changes-requested","findings":[{finding}]',
    )

    for payload in payloads:
        evaluation = contract.evaluate(payload, prepared, context)
        assert evaluation.status is EvaluationStatus.INVALID
        assert evaluation.valid_fragments == ()
        assert evaluation.coverage is None

    prepared.schema["additionalProperties"] = True
    evaluation = contract.evaluate(json.dumps(finding_payload()), prepared, context)
    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.valid_fragments == ()
    assert evaluation.violations == ("prepared-contract",)


def assert_non_finite_json_is_rejected(constant: str) -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)
    finding = json.dumps(finding_payload()["findings"][0])
    payload = f'{{"verdict":"changes-requested","findings":[{finding}],"nonFinite":{constant}}}'

    evaluation = contract.evaluate(payload, prepared, context)

    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.violations == ("invalid-json",)
    assert evaluation.valid_fragments == ()
    assert evaluation.coverage is None
    assert evaluation.completion_request is None


def test_findings_contract_rejects_nan_before_fragment_extraction() -> None:
    assert_non_finite_json_is_rejected("NaN")


def test_findings_contract_rejects_infinity_before_fragment_extraction() -> None:
    assert_non_finite_json_is_rejected("Infinity")


def test_findings_contract_rejects_negative_infinity_before_fragment_extraction() -> None:
    assert_non_finite_json_is_rejected("-Infinity")


def test_findings_contract_rejects_oversized_integer_without_exception() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)
    payload = json.dumps(finding_payload()).replace('"line": 3', f'"line": {"1" * 5001}')

    evaluation = contract.evaluate(payload, prepared, context)

    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.violations == ("invalid-json",)
    assert evaluation.valid_fragments == ()


@pytest.mark.parametrize(
    "payload_template",
    [
        '{"verdict":"changes-requested","findings":[$FINDING],"extra":1e400}',
        '{"verdict":"changes-requested","findings":[$FINDING],"extra":{"nested":-1e400}}',
        '{"verdict":"changes-requested","findings":[$FINDING],"extra":[0,{"nested":1e400}]}',
        '{"verdict":"changes-requested","findings":[$OVERFLOW,$FINDING]}',
        '[{"nested":-1e400}]',
    ],
)
def test_findings_contract_rejects_decoded_nonfinite_tree_before_extraction(
    payload_template: str,
) -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)
    finding = json.dumps(finding_payload()["findings"][0], separators=(",", ":"))
    overflowing_finding = finding.replace('"line":3', '"line":1e400')
    payload = payload_template.replace("$OVERFLOW", overflowing_finding).replace(
        "$FINDING", finding
    )

    evaluation = contract.evaluate(payload, prepared, context)

    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.violations == ("invalid-json",)
    assert evaluation.valid_fragments == ()
    assert evaluation.coverage is None
    assert evaluation.completion_request is None


@pytest.mark.parametrize(
    ("payload_template", "expected_violation"),
    [
        (
            '{"verdict":"changes-requested","findings":[$FINDING],"extra":{"nested":[1e308]}}',
            "response-fields",
        ),
        (
            '{"verdict":"changes-requested","findings":[$FINITE,$FINDING]}',
            "finding-value",
        ),
    ],
)
def test_large_finite_decoded_numbers_follow_normal_contract_semantics(
    payload_template: str,
    expected_violation: str,
) -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)
    finding = json.dumps(finding_payload()["findings"][0], separators=(",", ":"))
    finite_finding = finding.replace('"line":3', '"line":1e308')
    payload = payload_template.replace("$FINITE", finite_finding).replace("$FINDING", finding)

    evaluation = contract.evaluate(payload, prepared, context)

    assert evaluation.status is EvaluationStatus.INCOMPLETE
    assert evaluation.violations == (expected_violation,)
    assert len(evaluation.valid_fragments) == 1


def test_large_schema_valid_integer_remains_complete() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)
    payload = json.dumps(finding_payload(line=10**100))

    evaluation = contract.evaluate(payload, prepared, context)

    assert evaluation.status is EvaluationStatus.COMPLETE
    assert evaluation.value is not None
    assert evaluation.value["findings"][0]["line"] == 10**100


def assert_isolated_surrogate_is_rejected(surrogate: str, *, extra_field: bool) -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)
    value = finding_payload(title=surrogate)
    if extra_field:
        value["extra"] = True

    evaluation = contract.evaluate(json.dumps(value, ensure_ascii=True), prepared, context)

    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.violations == ("invalid-json",)
    assert evaluation.valid_fragments == ()
    assert evaluation.coverage is None
    assert evaluation.completion_request is None


def test_findings_contract_rejects_isolated_surrogates_before_fragment_extraction() -> None:
    assert_isolated_surrogate_is_rejected(chr(0xD800), extra_field=True)
    assert_isolated_surrogate_is_rejected(chr(0xDC00), extra_field=True)


def test_findings_contract_rejects_isolated_surrogate_on_complete_path() -> None:
    assert_isolated_surrogate_is_rejected(chr(0xD800), extra_field=False)


def test_findings_contract_rejects_escaped_surrogate_in_top_level_extra() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)
    value = {**finding_payload(), "extra": chr(0xD800)}

    evaluation = contract.evaluate(json.dumps(value, ensure_ascii=True), prepared, context)

    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.violations == ("invalid-json",)
    assert evaluation.valid_fragments == ()
    assert evaluation.coverage is None
    assert evaluation.completion_request is None


def test_findings_contract_rejects_literal_surrogate_with_stable_payload_digest() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)
    payload = json.dumps(finding_payload())[:-1] + f', "extra": "{chr(0xD800)}"}}'

    evaluation = contract.evaluate(payload, prepared, context)

    assert (
        evaluation.payload_digest
        == hashlib.sha256(payload.encode(errors="surrogatepass")).hexdigest()
    )
    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.violations == ("invalid-json",)
    assert evaluation.valid_fragments == ()
    assert evaluation.coverage is None
    assert evaluation.completion_request is None


def test_findings_contract_rejects_surrogate_in_reviewed_files() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",), review_declaration_required=True)
    prepared = contract.prepare(context)
    value = {**finding_payload(), "reviewedFiles": [chr(0xDC00)]}

    evaluation = contract.evaluate(json.dumps(value, ensure_ascii=True), prepared, context)

    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.violations == ("invalid-json",)
    assert evaluation.valid_fragments == ()
    assert evaluation.coverage is None
    assert evaluation.completion_request is None


def test_valid_unicode_payload_digest_is_unchanged() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)
    payload = json.dumps(finding_payload(title="Résumé"), ensure_ascii=False)

    evaluation = contract.evaluate(payload, prepared, context)

    assert evaluation.status is EvaluationStatus.COMPLETE
    assert evaluation.payload_digest == hashlib.sha256(payload.encode()).hexdigest()


def test_finding_values_are_immutable_without_losing_dict_compatibility() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)
    evaluation = contract.evaluate(json.dumps(finding_payload()), prepared, context)
    assert evaluation.value is not None
    fragment_value = evaluation.valid_fragments[0].value
    evaluation_finding = evaluation.value["findings"][0]
    original = finding_payload()["findings"][0]

    assert fragment_value is evaluation_finding
    assert fragment_value == original
    assert json.loads(json.dumps(fragment_value)) == original

    with pytest.raises(TypeError):
        fragment_value["title"] = "mutated"
    with pytest.raises(TypeError):
        del evaluation_finding["title"]
    with pytest.raises(TypeError):
        fragment_value.update({"title": "mutated"})

    assert fragment_value == original
    assert evaluation_finding == original

    assert copy(fragment_value) is fragment_value
    assert deepcopy(fragment_value) is fragment_value
    copied_evaluation = deepcopy(evaluation)
    assert copied_evaluation == evaluation
    assert copied_evaluation.valid_fragments[0].value is fragment_value
    assert copied_evaluation.value is not None
    assert copied_evaluation.value["findings"][0] is fragment_value
