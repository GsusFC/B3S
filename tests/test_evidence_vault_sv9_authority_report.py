import ast
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

import pytest

from src.services import evidence_vault_sv9_authority_application as application
from src.services import evidence_vault_sv9_authority_report as publication
from src.sv9.aggregator import aggregate
from src.sv9.models import ComponentResult, STATUS_NOT_DETECTED
from tests.test_evidence_vault_sv9_authority_application import _ApplicationRepository, _Flow, _relation, _run
from tests.test_vault_sv9_parity import _components, _vision_sentinel_result
from web import scan_runner


# fmt: off
_row = lambda status, evaluation, candidate=None, delta=None, authority=None: (status, evaluation, candidate, delta, authority)
ESTABLISHED = _row("authority_established", "candidate_available", "C", None, "A")
ADVANCED = _row("authority_advanced", "candidate_available", "C", None, "A")
RETAINED = _row("authority_retained", "no_new_score", None, None, "A")
PENDING = _row("review_required", "review_required", None, "D", "A")
REVIEWED = _row("review_required", "candidate_available", "C", None, "A")
REVIEW_WITHOUT_AUTHORITY = _row("review_required", "candidate_available", "C")
FIRST_REVIEW = _row("first_run_unresolved", "review_required")
FIRST_NO_SCORE = _row("first_run_unresolved", "no_new_score")
CONFLICT_CANDIDATE = _row("authority_conflict", "candidate_available")
CONFLICT_REVIEW = _row("authority_conflict", "review_required")
CONFLICT_REVIEW_AUTHORITY = _row("authority_conflict", "review_required", None, None, "A")
CONFLICT_NO_SCORE = _row("authority_conflict", "no_new_score")
CONFLICT_NONE = _row("authority_conflict", None)
EXPECTED_ROWS = frozenset({ESTABLISHED, ADVANCED, RETAINED, PENDING, REVIEWED, REVIEW_WITHOUT_AUTHORITY, FIRST_REVIEW, FIRST_NO_SCORE, CONFLICT_CANDIDATE, CONFLICT_REVIEW, CONFLICT_REVIEW_AUTHORITY, CONFLICT_NO_SCORE, CONFLICT_NONE})


def _app(status, evaluation, candidate=None, signed_delta=None, authority=None): return {"status": status, "reason_codes": [], "evaluation_status": evaluation, "candidate": candidate, "signed_delta": signed_delta, "authority": authority}
def _shape(value): return _row(value["status"], value["evaluation_status"], "C" if value["candidate"] is not None else None, "D" if value["signed_delta"] is not None else None, "A" if value["authority"] is not None else None)
def _report(payload, scan="scan"): return scan_runner._compose_report(scan, "https://example.test", "Example", payload)
def _no(value, current="new", source=None):
    result = publication.project_vault_authority_publication(value, current, source); assert result["action"] == "record_no_score" and result["source_report_id"] is None and result["scanner_payload"]["sv9"]["brand3_score"] is None; return result


def _adopt():
    repo = _ApplicationRepository(records=(9,)); outcome = _run(repo, _Flow()); assert _shape(outcome) == ESTABLISHED; return repo, outcome

def _advanced():
    repo, _outcome = _adopt(); older = deepcopy(repo.authority["event"]); repo.records = (3, 9)
    outcome = _run(repo, _Flow(), current=(3, 9), relations=[_relation(repo, "M1", number=3), _relation(repo, "M1", number=9)], source="scan-2")
    outcome["authority"]["event"] = older; assert _shape(outcome) == ADVANCED; return outcome

def _retained():
    repo, established = _adopt(); source = _report(publication.project_vault_authority_publication(established, "scan")["scanner_payload"])
    outcome = _run(repo, _Flow(fail=1), relations=[_relation(repo, "M1", number=9)], trusted=()); assert _shape(outcome) == RETAINED; return repo, outcome, source

def _pending():
    repo, _outcome, source = _retained()
    outcome = _run(repo, _Flow(), current=(3,), relations=[_relation(repo, "M1", number=3)], source="scan-3")
    assert _shape(outcome) == PENDING; return outcome, source

def _candidate_review(with_authority):
    if with_authority:
        repo, _outcome, source = _retained(); repo.records, current = (3, 9), (3, 9)
    else: repo, source, current = _ApplicationRepository(records=(9,)), None, (9,)
    repo.adopt_error = application.EvidenceVaultSv9JudgmentCandidateLegacyAuthorityError("legacy")
    outcome = _run(repo, _Flow(), current=current, relations=[_relation(repo, "M1", number=value) for value in current], source="review")
    assert _shape(outcome) == (REVIEWED if with_authority else REVIEW_WITHOUT_AUTHORITY); return outcome, source

def _case(row):
    if row == ESTABLISHED: return _adopt()[1], "scan", None
    if row == ADVANCED: return _advanced(), "scan-2", None
    if row == RETAINED:
        _repo, outcome, source = _retained(); return outcome, "current", source
    if row == PENDING:
        outcome, source = _pending(); return outcome, "scan-3", source
    if row == REVIEWED:
        outcome, source = _candidate_review(True); return outcome, "review", source
    if row == REVIEW_WITHOUT_AUTHORITY: return _candidate_review(False)[0], "review", None
    if row == CONFLICT_REVIEW_AUTHORITY:
        _repo, outcome, _source = _retained(); return _app("authority_conflict", "review_required", authority=deepcopy(outcome["authority"])), "current", None
    return _app(row[0], row[1]), "current", None


@pytest.mark.parametrize("row", sorted(EXPECTED_ROWS, key=repr))
def test_every_producer_row_projects_through_the_public_contract(row):
    value, current, source = _case(row); result = publication.project_vault_authority_publication(value, current, source); assert _shape(value) == row and set(result) == {"action", "source_report_id", "scanner_payload"}
    expected = "publish_current" if row in {ESTABLISHED, ADVANCED} else "retain_source" if row in {RETAINED, PENDING, REVIEWED} else "record_no_score"
    assert result["action"] == expected
    if expected == "publish_current": assert result["source_report_id"] is None and result["scanner_payload"]["source_run_id"] == current and _report(result["scanner_payload"], current)["score"] is not None
    elif expected == "retain_source": assert result == {"action": "retain_source", "source_report_id": source["id"], "scanner_payload": None}
    else: assert result["source_report_id"] is None and _report(result["scanner_payload"], current)["score"] is None


def _expression(value): return ast.dump(ast.parse(value, mode="eval").body, annotate_fields=False)
_GENERIC = _expression("_result('authority_conflict', outcome)")
_CALLS = Counter({
    ("run_evidence_vault_sv9_authority_application", _expression("_result('authority_conflict', {'reason_codes': ['evaluation_failure']})")): 1,
    ("run_evidence_vault_sv9_authority_application", _GENERIC): 2, ("_apply_candidate", _GENERIC): 3, ("_candidate_readback", _GENERIC): 1, ("_apply_review", _GENERIC): 4,
    ("_candidate_review", _expression("_result('review_required', dict(outcome) | {'reason_codes': [reason]}, authority, candidate)")): 1,
    ("_apply_review", _expression("_result('authority_conflict', outcome, authority)")): 1, ("_apply_review", _expression("_result('review_required', outcome, authority, signed_delta=signed)")): 2,
    ("_apply_review", _expression("_result('first_run_unresolved', outcome)")): 1, ("_retain", _expression("_result('authority_retained', outcome, authority)")): 1,
    ("_retain", _expression("_result('first_run_unresolved' if state == 'absent' else 'authority_conflict', outcome)")): 1, ("_success", _expression("_result(status, outcome, authority, candidate)")): 1,
})
_CALL_ROWS = {
    ("run_evidence_vault_sv9_authority_application", _expression("_result('authority_conflict', {'reason_codes': ['evaluation_failure']})")): {CONFLICT_NONE},
    ("run_evidence_vault_sv9_authority_application", _GENERIC): {CONFLICT_NO_SCORE, CONFLICT_NONE}, ("_apply_candidate", _GENERIC): {CONFLICT_CANDIDATE}, ("_candidate_readback", _GENERIC): {CONFLICT_CANDIDATE}, ("_apply_review", _GENERIC): {CONFLICT_REVIEW},
    ("_candidate_review", _expression("_result('review_required', dict(outcome) | {'reason_codes': [reason]}, authority, candidate)")): {REVIEWED, REVIEW_WITHOUT_AUTHORITY},
    ("_apply_review", _expression("_result('authority_conflict', outcome, authority)")): {CONFLICT_REVIEW_AUTHORITY}, ("_apply_review", _expression("_result('review_required', outcome, authority, signed_delta=signed)")): {PENDING},
    ("_apply_review", _expression("_result('first_run_unresolved', outcome)")): {FIRST_REVIEW}, ("_retain", _expression("_result('authority_retained', outcome, authority)")): {RETAINED},
    ("_retain", _expression("_result('first_run_unresolved' if state == 'absent' else 'authority_conflict', outcome)")): {FIRST_NO_SCORE, CONFLICT_NO_SCORE}, ("_success", _expression("_result(status, outcome, authority, candidate)")): {ESTABLISHED, ADVANCED},
}


def _result_calls(tree):
    found = []
    class Visitor(ast.NodeVisitor):
        function = None
        def visit_FunctionDef(self, node):
            previous, self.function = self.function, node.name
            for child in node.body: self.visit(child)
            self.function = previous
        visit_AsyncFunctionDef = visit_FunctionDef
        def visit_Call(self, node):
            if self.function and isinstance(node.func, ast.Name) and node.func.id == "_result": found.append((self.function, ast.dump(node, annotate_fields=False)))
            self.generic_visit(node)
    Visitor().visit(tree); return Counter(found)

def test_authority_application_result_inventory_is_complete_and_bounded():
    observed = _result_calls(ast.parse(Path(application.__file__).read_text()))
    assert observed == _CALLS and sum(observed.values()) == 19 and set(observed) == set(_CALL_ROWS)
    assert all(rows <= EXPECTED_ROWS and rows for rows in _CALL_ROWS.values()) and set().union(*_CALL_ROWS.values()) == EXPECTED_ROWS
    assert publication.VALID_APPLICATION_ROWS == EXPECTED_ROWS and type(publication.VALID_APPLICATION_ROWS) is frozenset


@pytest.mark.parametrize("change", (
    lambda value: value.__setitem__("status", "unknown"), lambda value: value.__setitem__("evaluation_status", "unknown_dispatcher"),
    lambda value: value.__setitem__("candidate", None), lambda value: value.__setitem__("signed_delta", {}), lambda value: value.__setitem__("authority", None),
    lambda value: value["candidate"].__setitem__("id", "not-a-uuid"), lambda value: value["candidate"].__setitem__("source_scan_id", "other"),
    lambda value: value["authority"].__setitem__("score", True), lambda value: value["authority"]["active_authority_event"].__setitem__("event_fingerprint", "0" * 64), lambda value: value["authority"]["event"].pop("created_at"),
))
def test_status_slots_and_canonical_authority_tampering_fail_closed(change):
    _repo, outcome = _adopt(); broken = deepcopy(outcome); change(broken); _no(broken, "scan")


def test_pending_reopen_requires_exact_delta_current_source_and_never_publishes():
    pending, source = _pending(); assert publication.project_vault_authority_publication(pending, "scan-3", source)["action"] == "retain_source"
    for change, current in ((lambda value: value["signed_delta"].__setitem__("canonical_delta_fingerprint", "0" * 64), "scan-3"), (lambda value: None, "other"), (lambda value: value["authority"]["current_head"]["request"].__setitem__("source_scan_id", "other"), "scan-3"), (lambda value: value.__setitem__("status", "authority_retained"), "scan-3")):
        broken = deepcopy(pending); change(broken); _no(broken, current, source)
    stable = deepcopy(_adopt()[1]); stable["authority"] = deepcopy(pending["authority"]); _no(stable, "scan")


def test_retained_source_identity_envelope_score_fingerprints_and_overlay_are_strict():
    _repo, retained, source = _retained(); before = deepcopy(source); assert publication.project_vault_authority_publication(retained, "current", source) == {"action": "retain_source", "source_report_id": "scan", "scanner_payload": None} and source == before
    for change in (lambda value: value.__setitem__("id", "other"), lambda value: value["raw"].__setitem__("source_run_id", "other"), lambda value: value["raw"].__setitem__("schema_version", "foreign"), lambda value: value["sv9_assessment"].__setitem__("assessment_fingerprint", "0" * 64), lambda value: value["raw"]["sv9"].__setitem__("brand3_score", True), lambda value: value["raw"]["sv9"].__setitem__("score_fingerprint", "0" * 64)):
        broken = deepcopy(source); change(broken); _no(retained, "current", broken)
    reviewed, review_source = _candidate_review(True); assert publication.project_vault_authority_publication(reviewed, "review", review_source)["action"] == "retain_source"
    broken = deepcopy(reviewed); broken["candidate"]["source_scan_id"] = "other"; _no(broken, "review", review_source)
    pending, _pending_source = _pending(); broken = deepcopy(retained); broken["authority"] = pending["authority"]; _no(broken, "current", source)


def test_publish_is_input_preserving_scanner_compatible_and_has_no_runtime_provenance():
    _repo, established = _adopt(); before = deepcopy(established); published = publication.project_vault_authority_publication(established, "scan")
    assert established == before and published == json.loads(json.dumps(published)) and _report(published["scanner_payload"])["score"] == established["authority"]["score"] == published["scanner_payload"]["sv9"]["brand3_score"]
    assert all(token not in json.dumps(published) for token in ("provider", "capture", "authority_provenance")) and publication.project_vault_authority_publication(_advanced(), "scan-2")["action"] == "publish_current"
    imports = {alias.name for node in ast.walk(ast.parse(Path(publication.__file__).read_text())) if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names}
    assert not any(name.startswith(("web", "src.history", "src.storage")) or "repository" in name or "provider" in name for name in imports)


def test_v1_and_v2_logical_capacity_and_not_detected_coherencia_match_scanner():
    _repo, outcome = _adopt(); assert publication.project_vault_authority_publication(outcome, "scan")["scanner_payload"]["sv9"]["assessment"]["tile_count"] == 80
    scanner = _vision_sentinel_result()["assessment"]; kernel = {key: value for key, value in scanner.items() if key not in {"assessment_schema_version", "expected_tile_count", "availability", "reason_codes"}}; kernel["schema_version"] = scanner["assessment_schema_version"]
    assessment, projection = publication._scanner(kernel); assert assessment["tile_count"] + sum(row["scale"] for row in assessment["component_sentinels"]) == 80 and _report(publication._payload("sentinel", assessment, projection), "sentinel")["sv9_assessment"]["schema_version"].endswith("v2")
    components = _components(); components["coherencia"] = ComponentResult(component="coherencia", status=STATUS_NOT_DETECTED); scanner = aggregate(components, brand_name="Example", url="https://example.test").to_dict(); kernel = {key: value for key, value in scanner["assessment"].items() if key not in {"assessment_schema_version", "expected_tile_count", "availability", "reason_codes"}}; kernel["schema_version"] = scanner["assessment"]["assessment_schema_version"]
    assessment, projection = publication._scanner(kernel); assert publication._payload("sentinel", assessment, projection)["sv9"]["result"]["needs_review"] is scanner["needs_review"] is False


def test_malformed_current_ids_and_unknown_dispatcher_rows_are_safe_no_score():
    malformed = "\ud800"; value = _app("first_run_unresolved", "no_new_score"); result = _no(value, malformed); assert result["scanner_payload"]["source_run_id"] is None and malformed not in json.dumps(result, ensure_ascii=False, allow_nan=False) and json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
    _repo, outcome = _adopt()
    for current in ("", " scan ", None, True): _no(outcome, current)
    _no(_app("review_required", "candidate_available"), "scan"); _no(_app("authority_conflict", "unknown_dispatcher"), "current")
# fmt: on
