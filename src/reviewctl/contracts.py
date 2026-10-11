"""Native typed review contracts independent from transport orchestration."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, NoReturn, Protocol

FINDING_FIELDS = {"severity", "path", "line", "title", "evidence", "reproduction"}
FINDING_SEVERITIES = {"critical", "high", "medium", "low", "info"}
REVIEW_VERDICTS = {"approved", "changes-requested"}
FINDINGS_REQUIRED_FIELDS = ("verdict", "findings")
OPTIONAL_REVIEWED_FILES_DIALECT = "optional-reviewed-files-v1"
REVIEW_DECLARATION_CONTRACTS = frozenset({"findings-json"})
FINDINGS_CONTRACT_VIOLATION_CODES = frozenset(
    {
        "finding-fields",
        "finding-path",
        "finding-value",
        "findings-shape",
        "invalid-json",
        "prepared-contract",
        "response-fields",
        "review-declaration",
        "top-level-not-object",
        "verdict",
        "verdict-invariant",
    }
)


FINDINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdict", "findings"],
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": sorted(REVIEW_VERDICTS)},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": sorted(FINDING_FIELDS),
                "additionalProperties": False,
                "properties": {
                    "severity": {"type": "string", "enum": sorted(FINDING_SEVERITIES)},
                    "path": {"type": "string"},
                    "line": {"type": "integer", "minimum": 1},
                    "title": {"type": "string"},
                    "evidence": {"type": "string"},
                    "reproduction": {"type": "string"},
                },
            },
        },
    },
}

REVIEWED_FILES_SCHEMA: dict[str, Any] = {
    "type": "array",
    "minItems": 1,
    "items": {"type": "string", "minLength": 1},
}


def require_string_json_object_keys(value: object) -> None:
    """Reject Python mappings that JSON would silently coerce into another identity."""
    pending = [(value, False)]
    active_containers: set[int] = set()
    while pending:
        current, exiting = pending.pop()
        if not isinstance(current, dict | list | tuple):
            continue
        if isinstance(current, dict) and type(current) not in {dict, FrozenDict}:
            raise ValueError("unsupported JSON object type")
        if isinstance(current, list) and type(current) is not list:
            raise ValueError("unsupported JSON array type")
        if isinstance(current, tuple) and type(current) is not tuple:
            raise ValueError("unsupported JSON sequence type")
        identity = id(current)
        if exiting:
            active_containers.remove(identity)
            continue
        if identity in active_containers:
            raise ValueError("circular JSON value")
        active_containers.add(identity)
        pending.append((current, True))
        if isinstance(current, dict):
            if any(type(key) is not str for key in current):
                raise ValueError("JSON object keys must be strings")
            pending.extend((item, False) for item in current.values())
        else:
            pending.extend((item, False) for item in current)


def canonical_json(value: object) -> bytes:
    """Serialize contract identity and normalized values deterministically."""
    require_string_json_object_keys(value)
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def has_exact_json_scalar_types(value: object) -> bool:
    """Reject host-language scalar subclasses before contract comparisons."""
    pending = [value]
    seen_containers: set[int] = set()
    while pending:
        current = pending.pop()
        if isinstance(current, str) and type(current) is not str:
            return False
        if isinstance(current, int) and type(current) not in {bool, int}:
            return False
        if isinstance(current, float) and type(current) is not float:
            return False
        if not isinstance(current, dict | list | tuple):
            continue
        if isinstance(current, dict) and type(current) not in {dict, FrozenDict}:
            return False
        if isinstance(current, list) and type(current) is not list:
            return False
        if isinstance(current, tuple) and type(current) is not tuple:
            return False
        identity = id(current)
        if identity in seen_containers:
            continue
        seen_containers.add(identity)
        if isinstance(current, dict):
            if any(type(key) is not str for key in current):
                return False
            pending.extend(current.values())
        else:
            pending.extend(current)
    return True


@dataclass(frozen=True)
class ContractContext:
    """Bounded facts that affect preparation and semantic validation."""

    file_names: tuple[str, ...] = ()
    review_declaration_required: bool = False


def valid_review_basename(value: object) -> bool:
    """Return whether a value is a safe printable basename for review identity."""
    return (
        type(value) is str
        and bool(value)
        and value == value.strip()
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and value.isprintable()
    )


def valid_review_path(value: object) -> bool:
    """Accept a safe basename or canonical repository-relative file path."""
    return type(value) is str and all(valid_review_basename(part) for part in value.split("/"))


def valid_contract_context(value: object, *, require_file_names: bool = False) -> bool:
    """Validate canonical contract facts before they affect identity or semantics."""
    if type(value) is not ContractContext:
        return False
    file_names = value.file_names
    return (
        type(require_file_names) is bool
        and type(value.review_declaration_required) is bool
        and type(file_names) is tuple
        and (not require_file_names or bool(file_names))
        and all(valid_review_path(file_name) for file_name in file_names)
        and list(file_names) == sorted(set(file_names))
    )


@dataclass(frozen=True)
class PreparedContract:
    """Provider-neutral contract compiled for one review context."""

    name: str
    version: str
    file_names: tuple[str, ...]
    review_declaration_required: bool
    schema: dict[str, Any]
    output_instructions: str
    digest: str
    dialect: str | None = None

    @property
    def identity_material(self) -> dict[str, object]:
        identity = {
            "name": self.name,
            "version": self.version,
            "context": {
                "fileNames": list(self.file_names),
                "reviewDeclarationRequired": self.review_declaration_required,
            },
            "schema": self.schema,
            "outputInstructions": self.output_instructions,
        }
        if self.dialect is not None:
            identity["dialect"] = self.dialect
        return identity


class EvaluationStatus(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    INVALID = "invalid"


class FragmentKind(StrEnum):
    FINDING = "finding"


@dataclass(frozen=True)
class EvaluationContext:
    packet_digest: str | None = None


@dataclass(frozen=True)
class ContractFragment:
    fragment_id: str
    fingerprint: str
    kind: FragmentKind
    value: dict[str, Any]
    payload_digest: str
    scope: tuple[str, ...]


@dataclass(frozen=True)
class ContractCoverage:
    required_fields: tuple[str, ...]
    covered_fields: tuple[str, ...]
    missing_fields: tuple[str, ...]


def findings_required_fields(review_declaration_required: bool) -> tuple[str, ...]:
    """Return canonical ordered fields for one findings review context."""
    if type(review_declaration_required) is not bool:
        raise ValueError("review declaration requirement must be boolean")
    if review_declaration_required:
        return (*FINDINGS_REQUIRED_FIELDS, "reviewedFiles")
    return FINDINGS_REQUIRED_FIELDS


def findings_coverage(
    *,
    review_declaration_required: bool,
    verdict_covered: bool,
    findings_covered: bool,
    review_declaration_covered: bool,
) -> ContractCoverage:
    """Build producer-authoritative coverage in required-field order."""
    required_fields = findings_required_fields(review_declaration_required)
    coverage = {
        "verdict": verdict_covered,
        "findings": findings_covered,
        "reviewedFiles": review_declaration_covered,
    }
    return ContractCoverage(
        required_fields=required_fields,
        covered_fields=tuple(field for field in required_fields if coverage[field]),
        missing_fields=tuple(field for field in required_fields if not coverage[field]),
    )


@dataclass(frozen=True)
class ContractCompletionRequest:
    prepared_digest: str
    packet_digest: str | None
    missing_fields: tuple[str, ...]
    invalid_fragment_indexes: tuple[int, ...]
    violations: tuple[str, ...]


@dataclass(frozen=True)
class ContractEvaluation:
    """Exact decode and semantic-validation result for one raw payload."""

    name: str
    version: str
    prepared_digest: str
    payload_digest: str
    normalized_digest: str | None
    value: dict[str, Any] | None
    violations: tuple[str, ...]
    status: EvaluationStatus = EvaluationStatus.INVALID
    valid_fragments: tuple[ContractFragment, ...] = ()
    coverage: ContractCoverage | None = None
    completion_request: ContractCompletionRequest | None = None


class ReviewContract(Protocol):
    """Deep contract boundary consumed by transports and orchestration."""

    name: str
    version: str

    def prepare(self, context: ContractContext) -> PreparedContract: ...

    def evaluate(
        self,
        payload: str,
        prepared: PreparedContract,
        context: ContractContext,
        *,
        evidence: EvaluationContext | None = None,
    ) -> ContractEvaluation: ...


class DuplicateJsonField(ValueError):
    """Raised when exact JSON decoding encounters the same object key twice."""


class _InvalidJsonConstant(ValueError):
    """Raised when JSON decoding encounters a non-standard numeric constant."""


def _reject_json_constant(value: str) -> None:
    raise _InvalidJsonConstant(value)


def exact_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise DuplicateJsonField(key)
        value[key] = item
    return value


class FrozenDict(dict[str, Any]):
    """JSON-compatible dictionary with immutable v1 finding values."""

    def _reject_mutation(self, *args: object, **kwargs: object) -> NoReturn:
        raise TypeError("contract finding values are immutable")

    __setitem__ = _reject_mutation
    __delitem__ = _reject_mutation
    clear = _reject_mutation
    pop = _reject_mutation
    popitem = _reject_mutation
    setdefault = _reject_mutation
    update = _reject_mutation
    __ior__ = _reject_mutation

    def __copy__(self) -> FrozenDict:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> FrozenDict:
        memo[id(self)] = self
        return self


def _contains_surrogate(value: object) -> bool:
    pending = [value]
    while pending:
        current = pending.pop()
        if type(current) is str:
            if any(0xD800 <= ord(character) <= 0xDFFF for character in current):
                return True
        elif isinstance(current, list):
            pending.extend(current)
        elif isinstance(current, dict):
            pending.extend(current.keys())
            pending.extend(current.values())
    return False


def valid_finding(value: object) -> bool:
    """Validate one finding before any canonical identity operation."""
    if type(value) not in {dict, FrozenDict} or set(value) != FINDING_FIELDS:
        return False
    severity = value["severity"]
    if type(severity) is not str or severity not in FINDING_SEVERITIES:
        return False
    if type(value["line"]) is not int or value["line"] < 1:
        return False
    if not valid_review_path(value["path"]):
        return False
    if not all(
        type(value[field]) is str and bool(value[field].strip())
        for field in ("title", "evidence", "reproduction")
    ):
        return False
    try:
        canonical_json(value)
    except TypeError, ValueError, UnicodeError, OverflowError:
        return False
    return True


def _validate_finding(
    finding: object, context: ContractContext
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(finding, dict) or set(finding) != FINDING_FIELDS:
        return None, "finding-fields"
    if not valid_finding(finding):
        return None, "finding-value"
    if context.file_names and finding["path"] not in context.file_names:
        return None, "finding-path"
    return FrozenDict(finding), None


class FindingsJsonContract:
    name = "findings-json"
    version = "1"

    def __init__(self, *, dialect: str | None = OPTIONAL_REVIEWED_FILES_DIALECT) -> None:
        if dialect not in (None, OPTIONAL_REVIEWED_FILES_DIALECT):
            raise ValueError("unsupported findings dialect")
        self.dialect = dialect

    def prepare(self, context: ContractContext) -> PreparedContract:
        if not valid_contract_context(context):
            raise ValueError("invalid contract context")
        schema = deepcopy(FINDINGS_SCHEMA)
        if context.review_declaration_required:
            schema["required"].append("reviewedFiles")
        if context.review_declaration_required or (self.dialect and context.file_names):
            schema["properties"]["reviewedFiles"] = deepcopy(REVIEWED_FILES_SCHEMA)
        top_level_fields = (
            "`verdict`, `findings`, and `reviewedFiles`"
            if context.review_declaration_required
            else "`verdict` and `findings`"
        )
        if self.dialect and context.file_names and not context.review_declaration_required:
            top_level_fields += ", plus optional `reviewedFiles`"
        instructions = (
            "Return only JSON matching the supplied schema. The top-level object has exactly "
            f"{top_level_fields}. Each finding has exactly six fields: `severity`, `path`, "
            "`line`, `title`, `evidence`, and `reproduction`. Use `changes-requested` if and only "
            "if `findings` is non-empty; use `approved` if and only if `findings` is empty."
        )
        if context.review_declaration_required:
            instructions += (
                " Also list every frozen snapshot actually reviewed in `reviewedFiles`; do not "
                "emit a verdict if a supplied file could not be read. The runner records the "
                "authoritative source hashes."
            )
        identity = {
            "name": self.name,
            "version": self.version,
            "context": {
                "fileNames": list(context.file_names),
                "reviewDeclarationRequired": context.review_declaration_required,
            },
            "schema": schema,
            "outputInstructions": instructions,
        }
        if self.dialect is not None:
            identity["dialect"] = self.dialect
        return PreparedContract(
            name=self.name,
            version=self.version,
            file_names=context.file_names,
            review_declaration_required=context.review_declaration_required,
            schema=schema,
            output_instructions=instructions,
            digest=hashlib.sha256(canonical_json(identity)).hexdigest(),
            dialect=self.dialect,
        )

    def evaluate(
        self,
        payload: str,
        prepared: PreparedContract,
        context: ContractContext,
        *,
        evidence: EvaluationContext | None = None,
    ) -> ContractEvaluation:
        payload_is_text = type(payload) is str
        payload_bytes = payload.encode(errors="surrogatepass") if payload_is_text else b""
        payload_digest = hashlib.sha256(payload_bytes).hexdigest()
        prepared_digest = (
            prepared.digest
            if type(prepared) is PreparedContract and type(prepared.digest) is str
            else ""
        )

        def rejected(code: str) -> ContractEvaluation:
            return ContractEvaluation(
                name=self.name,
                version=self.version,
                prepared_digest=prepared_digest,
                payload_digest=payload_digest,
                normalized_digest=None,
                value=None,
                violations=(code,),
            )

        if not payload_is_text:
            return rejected("invalid-json")
        if (
            type(prepared) is not PreparedContract
            or not has_exact_json_scalar_types(prepared.identity_material)
            or type(prepared.digest) is not str
            or not valid_contract_context(context)
        ):
            return rejected("prepared-contract")
        try:
            prepared_identity = canonical_json(prepared.identity_material)
            expected_identity = canonical_json(self.prepare(context).identity_material)
        except TypeError, ValueError, UnicodeError, OverflowError, RecursionError:
            return rejected("prepared-contract")
        if (
            prepared_identity != expected_identity
            or prepared.digest != hashlib.sha256(prepared_identity).hexdigest()
        ):
            return rejected("prepared-contract")

        try:
            value = json.loads(
                payload,
                object_pairs_hook=exact_json_object,
                parse_constant=_reject_json_constant,
            )
        except ValueError, RecursionError:
            return rejected("invalid-json")
        try:
            canonical_json(value)
        except TypeError, ValueError, UnicodeError, OverflowError, RecursionError:
            return rejected("invalid-json")
        if _contains_surrogate(value):
            return rejected("invalid-json")
        if not isinstance(value, dict):
            return rejected("top-level-not-object")

        required_fields = findings_required_fields(context.review_declaration_required)
        allowed_fields = set(prepared.schema["properties"])
        violation = (
            None if set(required_fields) <= set(value) <= allowed_fields else "response-fields"
        )

        verdict = value.get("verdict")
        verdict_valid = type(verdict) is str and verdict in REVIEW_VERDICTS
        if violation is None and not verdict_valid:
            violation = "verdict"

        findings = value.get("findings")
        findings_are_list = isinstance(findings, list)
        if violation is None and not findings_are_list:
            violation = "findings-shape"

        normalized_findings: list[dict[str, Any]] = []
        normalized_finding_identities: set[bytes] = set()
        invalid_fragment_indexes: list[int] = []
        first_finding_violation: str | None = None
        if findings_are_list:
            for index, finding in enumerate(findings):
                normalized_finding, finding_violation = _validate_finding(finding, context)
                if finding_violation is not None:
                    invalid_fragment_indexes.append(index)
                    if first_finding_violation is None:
                        first_finding_violation = finding_violation
                    continue
                assert normalized_finding is not None
                finding_identity = canonical_json(normalized_finding)
                if finding_identity in normalized_finding_identities:
                    continue
                normalized_finding_identities.add(finding_identity)
                normalized_findings.append(normalized_finding)
        verdict_invariant = (
            verdict_valid and findings_are_list and (verdict == "approved") == (not findings)
        )
        if violation is None and not verdict_invariant:
            violation = "verdict-invariant"
        if violation is None and first_finding_violation is not None:
            violation = first_finding_violation

        normalized_files: list[str] | None = None
        if context.review_declaration_required or (self.dialect and "reviewedFiles" in value):
            reviewed_files = value.get("reviewedFiles")
            candidate_files: list[str] = []
            review_declaration_valid = isinstance(reviewed_files, list)
            if review_declaration_valid:
                for reviewed in reviewed_files:
                    if type(reviewed) is not str or not reviewed.strip():
                        review_declaration_valid = False
                        break
                    declared = reviewed.strip()
                    if declared in context.file_names:
                        candidate_files.append(declared)
                        continue
                    declared_path = Path(declared)
                    if (
                        not declared_path.is_absolute()
                        or not declared_path.parent.name.startswith("reviewctl-input-")
                        or declared_path.name not in context.file_names
                    ):
                        review_declaration_valid = False
                        break
                    candidate_files.append(declared_path.name)
            if review_declaration_valid and (
                len(candidate_files) != len(set(candidate_files))
                or set(candidate_files) != set(context.file_names)
            ):
                review_declaration_valid = False
            if review_declaration_valid:
                normalized_files = list(context.file_names)
            elif violation is None:
                violation = "review-declaration"

        findings_valid = findings_are_list and not invalid_fragment_indexes and violation is None
        coverage = findings_coverage(
            review_declaration_required=context.review_declaration_required,
            verdict_covered=verdict_invariant,
            findings_covered=findings_valid,
            review_declaration_covered=normalized_files is not None,
        )

        fragments: list[ContractFragment] = []
        for finding in normalized_findings:
            scope = (finding["path"],)
            fingerprint = hashlib.sha256(
                canonical_json(
                    {
                        "contract": self.name,
                        "version": self.version,
                        "kind": FragmentKind.FINDING.value,
                        "value": finding,
                        "scope": scope,
                    }
                )
            ).hexdigest()
            fragment_id = hashlib.sha256(
                canonical_json({"fingerprint": fingerprint, "payloadDigest": payload_digest})
            ).hexdigest()
            fragments.append(
                ContractFragment(
                    fragment_id=fragment_id,
                    fingerprint=fingerprint,
                    kind=FragmentKind.FINDING,
                    value=finding,
                    payload_digest=payload_digest,
                    scope=scope,
                )
            )
        fragments.sort(key=lambda fragment: fragment.fragment_id)

        if violation is not None:
            violations = (violation,)
            if not fragments:
                return ContractEvaluation(
                    name=self.name,
                    version=self.version,
                    prepared_digest=prepared.digest,
                    payload_digest=payload_digest,
                    normalized_digest=None,
                    value=None,
                    violations=violations,
                    coverage=coverage,
                )
            completion_request = ContractCompletionRequest(
                prepared_digest=prepared.digest,
                packet_digest=evidence.packet_digest if evidence is not None else None,
                missing_fields=coverage.missing_fields,
                invalid_fragment_indexes=tuple(invalid_fragment_indexes),
                violations=violations,
            )
            return ContractEvaluation(
                name=self.name,
                version=self.version,
                prepared_digest=prepared.digest,
                payload_digest=payload_digest,
                normalized_digest=None,
                value=None,
                violations=violations,
                status=EvaluationStatus.INCOMPLETE,
                valid_fragments=tuple(fragments),
                coverage=coverage,
                completion_request=completion_request,
            )

        assert verdict_valid
        normalized: dict[str, Any] = {"verdict": verdict, "findings": normalized_findings}
        if normalized_files is not None:
            normalized["reviewedFiles"] = normalized_files

        normalized_digest = hashlib.sha256(canonical_json(normalized)).hexdigest()
        return ContractEvaluation(
            name=self.name,
            version=self.version,
            prepared_digest=prepared.digest,
            payload_digest=payload_digest,
            normalized_digest=normalized_digest,
            value=normalized,
            violations=(),
            status=EvaluationStatus.COMPLETE,
            valid_fragments=tuple(fragments),
            coverage=coverage,
        )


_CONTRACTS: dict[str, ReviewContract] = {"findings-json": FindingsJsonContract()}
_LEGACY_FINDINGS_CONTRACT = FindingsJsonContract(dialect=None)


def findings_contract_for_dialect(dialect: object) -> ReviewContract:
    """Resolve only the frozen legacy envelope or the explicitly named current dialect."""
    if dialect is None:
        return _LEGACY_FINDINGS_CONTRACT
    if type(dialect) is str and dialect == OPTIONAL_REVIEWED_FILES_DIALECT:
        return get_contract("findings-json")
    raise ValueError("unsupported findings dialect")


def resolve_findings_prepared(context: ContractContext, digest: str) -> PreparedContract:
    """Reproduce an exact supported preparation for fragment/completion provenance."""
    for dialect in (OPTIONAL_REVIEWED_FILES_DIALECT, None):
        prepared = findings_contract_for_dialect(dialect).prepare(context)
        if prepared.digest == digest:
            return prepared
    raise ValueError("unsupported prepared findings identity")


def get_contract(name: str) -> ReviewContract:
    """Return a native contract by its stable name."""
    if type(name) is not str:
        raise KeyError(name)
    return _CONTRACTS[name]
