"""Pure construction and replay of signed raw-capture provenance.

This module only transforms and validates caller-supplied JSON values.  It does
no I/O, reads no configuration, and grants no persistence or runtime authority.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import hmac
import re
from typing import Annotated, Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from src.services.evidence_vault_canonical_core import canonical_json
from src.services.evidence_vault_raw_provenance import (
    PRE_RECEIPT_SNAPSHOT_VERSION,
    RAW_PROVENANCE_CAPTURE_KEY,
    EvidenceVaultRawProvenanceError,
    PreReceiptSnapshot,
    PublicKeyRegistry,
    RawAcquisitionReceipt,
    pre_receipt_snapshot_sha256,
    public_key_registry_fingerprint,
    receipt_set_fingerprint,
    verify_raw_acquisition_receipt,
)


RAW_PROVENANCE_ENVELOPE_VERSION = "evidence-vault-raw-provenance-envelope-v1"
DETERMINISTIC_EXTRACTOR_VERSION = "evidence-vault-deterministic-extractor-v1"
RAW_EVIDENCE_BINDING_VERSION = "evidence-vault-raw-evidence-binding-v1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_JSON_POINTER_TOKEN_RE = re.compile(r"(?:[^~]|~[01])*")
_OWNED_DOCUMENT_FIELDS = (
    "text",
    "content",
    "markdown",
    "markdown_content",
    "summary",
    "title",
)
_GROUP_IDENTITY_FIELDS = (
    "workspace_slug",
    "source_scan_id",
    "acquisition_session_id",
    "canonical_brand_domain",
    "pre_receipt_snapshot_sha256",
)

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class EvidenceVaultRawCaptureError(EvidenceVaultRawProvenanceError):
    """A signed capture, extractor result, or locator is not reproducible."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class RawProvenanceEnvelope(_StrictModel):
    """The exact durable envelope embedded under the reserved capture key."""

    schema_version: Literal[RAW_PROVENANCE_ENVELOPE_VERSION]
    workspace_slug: str
    source_scan_id: str
    acquisition_session_id: str
    canonical_brand_domain: str
    canonical_brand_url: str
    pre_receipt_snapshot_sha256: Sha256
    signed_receipts: list[RawAcquisitionReceipt] = Field(min_length=1, max_length=2)
    receipt_set_fingerprint: Sha256

    @model_validator(mode="after")
    def _receipt_group_is_canonical(self) -> "RawProvenanceEnvelope":
        fingerprints = [receipt.receipt_fingerprint for receipt in self.signed_receipts]
        if fingerprints != sorted(fingerprints):
            raise ValueError("signed_receipts must be sorted by receipt_fingerprint")
        roles = [receipt.claims.channel_role for receipt in self.signed_receipts]
        if len(roles) != len(set(roles)):
            raise ValueError("signed_receipts must have unique channel roles")
        expected_set = receipt_set_fingerprint(self.signed_receipts)
        if not hmac.compare_digest(self.receipt_set_fingerprint, expected_set):
            raise ValueError("receipt_set_fingerprint does not match signed_receipts")
        return self


@dataclass(frozen=True)
class SignedRawCapture:
    """Detached durable payload plus the hash a capture row must persist."""

    durable_raw_capture_payload: dict[str, JsonValue]
    capture_content_hash: str

    @property
    def raw_payload(self) -> dict[str, JsonValue]:
        return self.durable_raw_capture_payload


@dataclass(frozen=True)
class ValidatedRawCapture:
    """Structurally replayed capture; this type grants no signature authority."""

    pre_receipt_snapshot: PreReceiptSnapshot
    envelope: RawProvenanceEnvelope
    capture_content_hash: str

    @property
    def receipts(self) -> tuple[RawAcquisitionReceipt, ...]:
        return tuple(self.envelope.signed_receipts)

    @property
    def raw_payload(self) -> dict[str, JsonValue]:
        return deepcopy(self.pre_receipt_snapshot.raw_payload)


@dataclass(frozen=True, init=False)
class VerifiedRawCapture:
    """Signature-verified capture bound to one exact public-key policy."""

    pre_receipt_snapshot: PreReceiptSnapshot
    envelope: RawProvenanceEnvelope
    capture_content_hash: str
    public_key_registry_fingerprint: str

    def __init__(self) -> None:
        raise TypeError("VerifiedRawCapture is created only by signature verification")

    @classmethod
    def _from_verified(
        cls,
        *,
        validated: ValidatedRawCapture,
        registry_fingerprint: str,
    ) -> "VerifiedRawCapture":
        instance = object.__new__(cls)
        object.__setattr__(instance, "pre_receipt_snapshot", validated.pre_receipt_snapshot)
        object.__setattr__(instance, "envelope", validated.envelope)
        object.__setattr__(instance, "capture_content_hash", validated.capture_content_hash)
        object.__setattr__(
            instance,
            "public_key_registry_fingerprint",
            registry_fingerprint,
        )
        return instance

    @property
    def receipts(self) -> tuple[RawAcquisitionReceipt, ...]:
        return tuple(self.envelope.signed_receipts)

    @property
    def raw_payload(self) -> dict[str, JsonValue]:
        return deepcopy(self.pre_receipt_snapshot.raw_payload)


@dataclass(frozen=True)
class DeterministicExtraction:
    document: str
    sha256: str

    @property
    def extracted_document(self) -> str:
        return self.document

    @property
    def extracted_document_sha256(self) -> str:
        return self.sha256


@dataclass(frozen=True)
class ReproducedPassage:
    passage_text: str
    passage_sha256: str


def raw_capture_content_hash(raw_payload: Mapping[str, Any]) -> str:
    """Hash the complete durable JSON capture, including its envelope."""

    if not isinstance(raw_payload, Mapping):
        raise EvidenceVaultRawCaptureError("raw_payload must be an object")
    return hashlib.sha256(canonical_json(dict(raw_payload)).encode("utf-8")).hexdigest()


def build_signed_raw_capture(
    snapshot: PreReceiptSnapshot,
    receipts: Sequence[RawAcquisitionReceipt | Mapping[str, Any]],
    *,
    public_key_registry: PublicKeyRegistry | Mapping[str, Any],
) -> SignedRawCapture:
    """Embed a canonical signed envelope without mutating any caller value."""

    if public_key_registry is None:
        raise EvidenceVaultRawCaptureError("public_key_registry is required")
    if not isinstance(snapshot, PreReceiptSnapshot):
        raise EvidenceVaultRawCaptureError("snapshot must be an exact PreReceiptSnapshot")
    snapshot = PreReceiptSnapshot.model_validate(deepcopy(snapshot.model_dump(mode="json")))
    if RAW_PROVENANCE_CAPTURE_KEY in snapshot.raw_payload:
        raise EvidenceVaultRawCaptureError(f"raw_payload must not contain reserved key {RAW_PROVENANCE_CAPTURE_KEY!r}")
    parsed_receipts = _validated_receipt_group(
        receipts,
        snapshot=snapshot,
        public_key_registry=public_key_registry,
    )
    _validate_receipt_fragments(snapshot.raw_payload, parsed_receipts)
    snapshot_hash = pre_receipt_snapshot_sha256(snapshot)
    envelope = RawProvenanceEnvelope(
        schema_version=RAW_PROVENANCE_ENVELOPE_VERSION,
        workspace_slug=snapshot.workspace_slug,
        source_scan_id=snapshot.source_scan_id,
        acquisition_session_id=snapshot.acquisition_session_id,
        canonical_brand_domain=snapshot.canonical_brand_domain,
        canonical_brand_url=snapshot.canonical_brand_url,
        pre_receipt_snapshot_sha256=snapshot_hash,
        signed_receipts=sorted(parsed_receipts, key=lambda receipt: receipt.receipt_fingerprint),
        receipt_set_fingerprint=receipt_set_fingerprint(parsed_receipts),
    )
    durable_payload = deepcopy(snapshot.raw_payload)
    durable_payload[RAW_PROVENANCE_CAPTURE_KEY] = envelope.model_dump(mode="json")
    return SignedRawCapture(
        durable_raw_capture_payload=durable_payload,
        capture_content_hash=raw_capture_content_hash(durable_payload),
    )


def build_signed_raw_capture_payload(
    snapshot: PreReceiptSnapshot,
    receipts: Sequence[RawAcquisitionReceipt | Mapping[str, Any]],
    *,
    public_key_registry: PublicKeyRegistry | Mapping[str, Any],
) -> dict[str, JsonValue]:
    """Convenience form returning only the detached durable payload."""

    return build_signed_raw_capture(
        snapshot,
        receipts,
        public_key_registry=public_key_registry,
    ).durable_raw_capture_payload


def parse_signed_raw_capture(
    durable_raw_capture_payload: Mapping[str, Any],
    *,
    capture_content_hash: str,
) -> ValidatedRawCapture:
    """Structurally replay a capture without granting signature authority."""

    if not isinstance(durable_raw_capture_payload, Mapping):
        raise EvidenceVaultRawCaptureError("durable_raw_capture_payload must be an object")
    if not isinstance(capture_content_hash, str) or not _SHA256_RE.fullmatch(capture_content_hash):
        raise EvidenceVaultRawCaptureError("capture_content_hash must be a lowercase SHA-256")
    durable = deepcopy(dict(durable_raw_capture_payload))
    expected_capture_hash = raw_capture_content_hash(durable)
    if not hmac.compare_digest(capture_content_hash, expected_capture_hash):
        raise EvidenceVaultRawCaptureError("capture_content_hash does not match durable raw payload")
    envelope_value = durable.pop(RAW_PROVENANCE_CAPTURE_KEY, None)
    if not isinstance(envelope_value, Mapping):
        raise EvidenceVaultRawCaptureError("durable raw payload lacks its provenance envelope")
    envelope = RawProvenanceEnvelope.model_validate(dict(envelope_value))
    snapshot = PreReceiptSnapshot(
        schema_version=PRE_RECEIPT_SNAPSHOT_VERSION,
        workspace_slug=envelope.workspace_slug,
        source_scan_id=envelope.source_scan_id,
        acquisition_session_id=envelope.acquisition_session_id,
        canonical_brand_domain=envelope.canonical_brand_domain,
        canonical_brand_url=envelope.canonical_brand_url,
        raw_payload=durable,
    )
    expected_snapshot_hash = pre_receipt_snapshot_sha256(snapshot)
    if not hmac.compare_digest(envelope.pre_receipt_snapshot_sha256, expected_snapshot_hash):
        raise EvidenceVaultRawCaptureError(
            "pre_receipt_snapshot_sha256 does not match the envelope-free durable payload"
        )
    receipts = _validated_receipt_group(
        envelope.signed_receipts,
        snapshot=snapshot,
        public_key_registry=None,
    )
    expected_dumps = [
        receipt.model_dump(mode="json") for receipt in sorted(receipts, key=lambda receipt: receipt.receipt_fingerprint)
    ]
    if envelope.model_dump(mode="json")["signed_receipts"] != expected_dumps:
        raise EvidenceVaultRawCaptureError("signed_receipts are not canonical structural dumps")
    expected_set = receipt_set_fingerprint(receipts)
    if not hmac.compare_digest(envelope.receipt_set_fingerprint, expected_set):
        raise EvidenceVaultRawCaptureError("receipt_set_fingerprint is invalid")
    _validate_receipt_fragments(durable, receipts)
    return ValidatedRawCapture(snapshot, envelope, expected_capture_hash)


def parse_and_validate_signed_raw_capture(
    durable_raw_capture_payload: Mapping[str, Any],
    *,
    capture_content_hash: str,
    public_key_registry: PublicKeyRegistry | Mapping[str, Any],
) -> VerifiedRawCapture:
    """Replay a capture and verify every signature against one exact registry."""

    if public_key_registry is None:
        raise EvidenceVaultRawCaptureError("public_key_registry is required")
    validated = parse_signed_raw_capture(
        durable_raw_capture_payload,
        capture_content_hash=capture_content_hash,
    )
    _validated_receipt_group(
        validated.envelope.signed_receipts,
        snapshot=validated.pre_receipt_snapshot,
        public_key_registry=public_key_registry,
    )
    return VerifiedRawCapture._from_verified(
        validated=validated,
        registry_fingerprint=public_key_registry_fingerprint(public_key_registry),
    )


def validate_signed_raw_capture(
    durable_raw_capture_payload: Mapping[str, Any],
    *,
    capture_content_hash: str,
    public_key_registry: PublicKeyRegistry | Mapping[str, Any],
) -> VerifiedRawCapture:
    """Verify a signed capture for authority-boundary callers."""

    return parse_and_validate_signed_raw_capture(
        durable_raw_capture_payload,
        capture_content_hash=capture_content_hash,
        public_key_registry=public_key_registry,
    )


def extract_deterministic_document(
    verified_raw_capture: VerifiedRawCapture,
    *,
    receipt_fingerprint: str,
) -> DeterministicExtraction:
    """Reproduce one signed document selected from a verified capture."""

    if type(verified_raw_capture) is not VerifiedRawCapture:
        raise EvidenceVaultRawCaptureError("deterministic extraction requires an exact VerifiedRawCapture")
    if not isinstance(receipt_fingerprint, str) or not _SHA256_RE.fullmatch(receipt_fingerprint):
        raise EvidenceVaultRawCaptureError("receipt_fingerprint must be a lowercase SHA-256")
    if not _SHA256_RE.fullmatch(verified_raw_capture.public_key_registry_fingerprint):
        raise EvidenceVaultRawCaptureError("verified capture lacks a canonical public key registry fingerprint")
    durable_payload = verified_raw_capture.raw_payload
    durable_payload[RAW_PROVENANCE_CAPTURE_KEY] = verified_raw_capture.envelope.model_dump(mode="json")
    replayed = parse_signed_raw_capture(
        durable_payload,
        capture_content_hash=verified_raw_capture.capture_content_hash,
    )
    selected = [
        receipt
        for receipt in replayed.receipts
        if hmac.compare_digest(receipt.receipt_fingerprint, receipt_fingerprint)
    ]
    if len(selected) != 1:
        raise EvidenceVaultRawCaptureError("receipt_fingerprint does not select exactly one verified receipt")
    model = selected[0]
    if model.claims.extractor_version != DETERMINISTIC_EXTRACTOR_VERSION:
        raise EvidenceVaultRawCaptureError("receipt uses an unsupported extractor_version")
    fragment = _resolve_json_pointer(
        replayed.raw_payload,
        model.claims.raw_fragment_json_pointer,
    )
    fragment_sha = _json_fragment_sha256(fragment)
    if not hmac.compare_digest(model.claims.raw_fragment_sha256, fragment_sha):
        raise EvidenceVaultRawCaptureError("raw_fragment_sha256 does not match resolved fragment")
    if not isinstance(fragment, Mapping):
        raise EvidenceVaultRawCaptureError("deterministic extractor raw fragment must be an object")
    if model.claims.channel_role == "owned_web":
        document = _owned_document(fragment)
    elif model.claims.channel_role == "external_social_profile":
        document = _external_document(fragment)
    else:  # The receipt model is closed, but keep the extractor independently fail-closed.
        raise EvidenceVaultRawCaptureError("receipt channel_role is not extractable")
    document_sha = hashlib.sha256(document.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(model.claims.extracted_document_sha256, document_sha):
        raise EvidenceVaultRawCaptureError("deterministic document differs from signed extracted_document_sha256")
    return DeterministicExtraction(document=document, sha256=document_sha)


def reproduce_passage_locator(
    *,
    extracted_document: str,
    passage_locator: Mapping[str, Any],
    durable_evidence_record_content: str,
) -> ReproducedPassage:
    """Bind one meaningful passage to exact UTF-8 spans in both documents."""

    if not isinstance(extracted_document, str) or not extracted_document:
        raise EvidenceVaultRawCaptureError("extracted_document must be non-empty text")
    if not isinstance(durable_evidence_record_content, str):
        raise EvidenceVaultRawCaptureError("durable_evidence_record_content must be text")
    if not isinstance(passage_locator, Mapping):
        raise EvidenceVaultRawCaptureError("passage_locator must be an object")
    locator = dict(passage_locator)
    kind = locator.get("kind")
    common_fields = {
        "kind",
        "extracted_start",
        "extracted_end",
        "evidence_start",
        "evidence_end",
    }
    expected_fields = common_fields if kind == "utf8_byte_range" else None
    if expected_fields is None:
        raise EvidenceVaultRawCaptureError("passage_locator kind is unsupported")
    if set(locator) != expected_fields:
        raise EvidenceVaultRawCaptureError(f"{kind} locator has non-exact fields")

    offsets = (
        locator["extracted_start"],
        locator["extracted_end"],
        locator["evidence_start"],
        locator["evidence_end"],
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in offsets):
        raise EvidenceVaultRawCaptureError("passage locator offsets must be integers")
    extracted_start, extracted_end, evidence_start, evidence_end = offsets
    extracted_bytes = extracted_document.encode("utf-8")
    evidence_bytes = durable_evidence_record_content.encode("utf-8")
    if (
        extracted_start < 0
        or extracted_end <= extracted_start
        or extracted_end > len(extracted_bytes)
        or evidence_start < 0
        or evidence_end <= evidence_start
        or evidence_end > len(evidence_bytes)
    ):
        raise EvidenceVaultRawCaptureError("passage locator offsets are out of bounds or empty")
    selected_extracted_bytes = extracted_bytes[extracted_start:extracted_end]
    selected_evidence_bytes = evidence_bytes[evidence_start:evidence_end]
    try:
        passage = selected_extracted_bytes.decode("utf-8")
        selected_evidence_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceVaultRawCaptureError(
            "passage locator offsets do not fall on UTF-8 code-point boundaries"
        ) from exc

    if selected_extracted_bytes != selected_evidence_bytes:
        raise EvidenceVaultRawCaptureError("extracted and durable evidence UTF-8 spans do not match exactly")
    if len(selected_extracted_bytes) < 8:
        raise EvidenceVaultRawCaptureError("reproduced passage is shorter than 8 UTF-8 bytes")
    if len(passage) < 4:
        raise EvidenceVaultRawCaptureError("reproduced passage must contain at least four Unicode characters")
    return ReproducedPassage(
        passage_text=passage,
        passage_sha256=hashlib.sha256(selected_extracted_bytes).hexdigest(),
    )


def reproduce_passage(
    *,
    extracted_document: str,
    passage_locator: Mapping[str, Any],
    durable_evidence_record_content: str,
) -> ReproducedPassage:
    return reproduce_passage_locator(
        extracted_document=extracted_document,
        passage_locator=passage_locator,
        durable_evidence_record_content=durable_evidence_record_content,
    )


def _validated_receipt_group(
    receipts: Sequence[RawAcquisitionReceipt | Mapping[str, Any]],
    *,
    snapshot: PreReceiptSnapshot,
    public_key_registry: PublicKeyRegistry | Mapping[str, Any] | None,
) -> list[RawAcquisitionReceipt]:
    if isinstance(receipts, (str, bytes)) or not isinstance(receipts, Sequence):
        raise EvidenceVaultRawCaptureError("receipts must be a sequence")
    if not 1 <= len(receipts) <= 2:
        raise EvidenceVaultRawCaptureError("signed capture requires one or two receipts")
    parsed = [_receipt_from(receipt, public_key_registry=public_key_registry) for receipt in receipts]
    roles = [receipt.claims.channel_role for receipt in parsed]
    if len(roles) != len(set(roles)):
        raise EvidenceVaultRawCaptureError("signed capture receipts must have unique channel roles")
    expected_identity = {
        "workspace_slug": snapshot.workspace_slug,
        "source_scan_id": snapshot.source_scan_id,
        "acquisition_session_id": snapshot.acquisition_session_id,
        "canonical_brand_domain": snapshot.canonical_brand_domain,
        "pre_receipt_snapshot_sha256": pre_receipt_snapshot_sha256(snapshot),
    }
    for receipt in parsed:
        for field in _GROUP_IDENTITY_FIELDS:
            if getattr(receipt.claims, field) != expected_identity[field]:
                raise EvidenceVaultRawCaptureError(f"receipt {field} does not match the exact pre-receipt snapshot")
    return parsed


def _receipt_from(
    receipt: RawAcquisitionReceipt | Mapping[str, Any],
    *,
    public_key_registry: PublicKeyRegistry | Mapping[str, Any] | None,
) -> RawAcquisitionReceipt:
    if public_key_registry is not None:
        try:
            return verify_raw_acquisition_receipt(
                receipt,
                public_key_registry=public_key_registry,
            )
        except EvidenceVaultRawCaptureError:
            raise
        except EvidenceVaultRawProvenanceError as exc:
            raise EvidenceVaultRawCaptureError(str(exc)) from exc
    if isinstance(receipt, RawAcquisitionReceipt):
        # Reparse a detached dump so caller mutation cannot bypass structural checks.
        return RawAcquisitionReceipt.model_validate(receipt.model_dump(mode="json"))
    if not isinstance(receipt, Mapping):
        raise EvidenceVaultRawCaptureError("receipt must be an object")
    return RawAcquisitionReceipt.model_validate(deepcopy(dict(receipt)))


def _validate_receipt_fragments(
    raw_payload: Mapping[str, Any],
    receipts: Sequence[RawAcquisitionReceipt],
) -> None:
    for receipt in receipts:
        fragment = _resolve_json_pointer(
            raw_payload,
            receipt.claims.raw_fragment_json_pointer,
        )
        expected = _json_fragment_sha256(fragment)
        if not hmac.compare_digest(receipt.claims.raw_fragment_sha256, expected):
            raise EvidenceVaultRawCaptureError("raw_fragment_sha256 does not match the pre-receipt snapshot")


def _payload_without_envelope(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EvidenceVaultRawCaptureError("durable_raw_capture_payload must be an object")
    payload = deepcopy(dict(value))
    payload.pop(RAW_PROVENANCE_CAPTURE_KEY, None)
    return payload


def _owned_document(fragment: Mapping[str, Any]) -> str:
    for field in _OWNED_DOCUMENT_FIELDS:
        if field in fragment and not isinstance(fragment[field], str):
            raise EvidenceVaultRawCaptureError(f"owned_web allowlisted field {field!r} must be a string when present")
    for field in _OWNED_DOCUMENT_FIELDS:
        value = fragment.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise EvidenceVaultRawCaptureError("owned_web raw fragment lacks an allowlisted non-empty document field")


def _external_document(fragment: Mapping[str, Any]) -> str:
    required = {"title", "summary", "text", "highlights"}
    missing = required - set(fragment)
    if missing:
        raise EvidenceVaultRawCaptureError("external_social_profile raw fragment lacks required extractor fields")
    title = fragment["title"]
    summary = fragment["summary"]
    text = fragment["text"]
    highlights_value = fragment["highlights"]
    if not all(isinstance(value, str) for value in (title, summary, text)):
        raise EvidenceVaultRawCaptureError("external title, summary, and text must be strings")
    if not isinstance(highlights_value, list) or not all(isinstance(item, str) for item in highlights_value):
        raise EvidenceVaultRawCaptureError("external highlights must be a list of strings")
    highlights = " ".join(item for item in highlights_value if item.strip())
    parts = [part.strip() for part in (title, summary, highlights, text) if part.strip()]
    document = " ".join(" ".join(part.split()) for part in parts).strip()
    if not document:
        raise EvidenceVaultRawCaptureError("external_social_profile raw fragment has no reproducible content")
    return document


def _json_fragment_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _strict_json_pointer_tokens(pointer: Any, *, allow_root: bool) -> tuple[str, ...]:
    if not isinstance(pointer, str) or "\x00" in pointer or len(pointer) > 2048:
        raise EvidenceVaultRawCaptureError("JSON pointer is invalid")
    if pointer == "":
        if allow_root:
            return ()
        raise EvidenceVaultRawCaptureError("JSON pointer must select a non-root value")
    if not pointer.startswith("/") or (not allow_root and pointer.endswith("/")):
        raise EvidenceVaultRawCaptureError("JSON pointer must select a non-root value")
    encoded_tokens = pointer[1:].split("/")
    if any((not allow_root and token == "") or not _JSON_POINTER_TOKEN_RE.fullmatch(token) for token in encoded_tokens):
        raise EvidenceVaultRawCaptureError("JSON pointer is not strict RFC 6901")
    return tuple(token.replace("~1", "/").replace("~0", "~") for token in encoded_tokens)


def _resolve_json_pointer(value: Any, pointer: Any, *, allow_root: bool = False) -> Any:
    current = value
    for token in _strict_json_pointer_tokens(pointer, allow_root=allow_root):
        if isinstance(current, Mapping):
            if token not in current:
                raise EvidenceVaultRawCaptureError(f"JSON pointer does not resolve at object member {token!r}")
            current = current[token]
        elif isinstance(current, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", token):
                raise EvidenceVaultRawCaptureError("JSON pointer array token is not a canonical index")
            index = int(token)
            if index >= len(current):
                raise EvidenceVaultRawCaptureError("JSON pointer array index is out of bounds")
            current = current[index]
        else:
            raise EvidenceVaultRawCaptureError("JSON pointer traverses through a scalar")
    return current


__all__ = [
    "DETERMINISTIC_EXTRACTOR_VERSION",
    "RAW_EVIDENCE_BINDING_VERSION",
    "RAW_PROVENANCE_ENVELOPE_VERSION",
    "DeterministicExtraction",
    "EvidenceVaultRawCaptureError",
    "RawProvenanceEnvelope",
    "ReproducedPassage",
    "SignedRawCapture",
    "ValidatedRawCapture",
    "VerifiedRawCapture",
    "build_signed_raw_capture",
    "build_signed_raw_capture_payload",
    "extract_deterministic_document",
    "parse_and_validate_signed_raw_capture",
    "parse_signed_raw_capture",
    "raw_capture_content_hash",
    "reproduce_passage",
    "reproduce_passage_locator",
    "validate_signed_raw_capture",
]
