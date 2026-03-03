from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import requests
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit("Missing dependency: requests. Install it with: pip install requests") from exc


FILENAME_KEY_CANDIDATES: Tuple[str, ...] = ("file_name", "filename", "name", "source_file")
MISSING_TEXT = "<missing>"


@dataclass
class FailedField:
    path: str
    failure_type: str
    expected_value: Any
    actual_value: Any


@dataclass
class FileRunResult:
    payload_file: str
    stem: str
    status: str
    response_present: bool
    reference_present: bool
    reference_json_valid: bool
    match: bool
    reference_path: str
    response_hash: Optional[str] = None
    failed_fields: List[FailedField] = field(default_factory=list)
    note: str = ""
    reference_leaf_count: int = 0
    response_leaf_count: int = 0
    matched_leaf_count: int = 0
    mismatched_leaf_count: int = 0
    missing_leaf_count: int = 0
    unexpected_leaf_count: int = 0


@dataclass
class RunResult:
    run_index: int
    run_dir: Path
    response_json_path: Path
    run_meta_path: Path
    http_status: Optional[int]
    request_ok: bool
    run_error: Optional[str]
    item_source: str
    parsed_item_count: int
    unexpected_response_items: List[Dict[str, Any]]
    file_results: List[FileRunResult]
    payload_total: int
    responses_received: int
    matches: int
    missing_response_count: int
    missing_reference_count: int
    invalid_reference_json_count: int
    failed_field_count: int
    total_reference_leaf_count: int
    total_response_leaf_count: int
    total_matched_leaf_count: int
    total_mismatched_leaf_count: int
    total_missing_leaf_count: int
    total_unexpected_leaf_count: int
    overall_reference_match_percentage: float


@dataclass
class ConsistencyResult:
    consistent_files: int
    files_with_all_runs_response: int
    per_file: Dict[str, Dict[str, Any]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run PDF extraction API regression checks with optional field-level reporting."
    )
    parser.add_argument(
        "--endpoint",
        default="https://tools.sdplus.io/extract-claude",
        help="POST endpoint for file extraction API.",
    )
    parser.add_argument("--payload-dir", default="test payload", help="Directory containing payload PDFs.")
    parser.add_argument(
        "--reference-dir",
        default="test reference",
        help="Directory containing reference JSON files mapped by PDF stem.",
    )
    parser.add_argument(
        "--response-archive-dir",
        default="response archive",
        help="Base directory for archiving API responses.",
    )
    parser.add_argument(
        "--reports-dir",
        default="Archived Test reports",
        help="Base directory for archived text reports.",
    )
    parser.add_argument("--runs", type=int, choices=(1, 3), default=1, help="Number of test runs.")
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=180,
        help="HTTP timeout in seconds for each API request.",
    )
    parser.add_argument(
        "--pause-between-runs-seconds",
        type=float,
        default=0.0,
        help="Pause between runs (used when --runs 3).",
    )
    parser.add_argument(
        "--filename-key",
        default="auto",
        help="Response item key carrying filename. Use 'auto' for built-in candidates.",
    )
    parser.add_argument(
        "--report-failed-fields",
        action="store_true",
        help="Report all failed field paths (wrong value or missing in response) in console and report file.",
    )
    return parser.parse_args()


def pct(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "0.00%"
    return f"{(numerator / denominator) * 100:.2f}%"


def safe_json_string(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return repr(value)


def canonical_json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_text(value).encode("utf-8")).hexdigest()


def to_casefold_stem(filename: str) -> str:
    name = Path(filename).name.strip()
    stem = Path(name).stem.strip()
    return stem.casefold()


def collect_payload_pdfs(payload_dir: Path) -> List[Path]:
    if not payload_dir.exists() or not payload_dir.is_dir():
        raise SystemExit(f"Payload directory does not exist: {payload_dir}")
    payloads = sorted(
        [path for path in payload_dir.iterdir() if path.is_file() and path.suffix.casefold() == ".pdf"],
        key=lambda p: p.name.casefold(),
    )
    if not payloads:
        raise SystemExit(f"No PDF files found in payload directory: {payload_dir}")

    seen: Dict[str, str] = {}
    duplicates: List[str] = []
    for pdf in payloads:
        stem_key = pdf.stem.casefold()
        if stem_key in seen:
            duplicates.append(f"{seen[stem_key]} and {pdf.name}")
        else:
            seen[stem_key] = pdf.name
    if duplicates:
        dup_list = "; ".join(duplicates)
        raise SystemExit(f"Duplicate payload stems detected: {dup_list}")

    return payloads


def choose_filename_key(item: Dict[str, Any], filename_key: str) -> Optional[str]:
    if filename_key != "auto":
        value = item.get(filename_key)
        return value.strip() if isinstance(value, str) and value.strip() else None

    for key in FILENAME_KEY_CANDIDATES:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def extract_response_items(response_obj: Any) -> Tuple[List[Any], str, Optional[Dict[str, Any]]]:
    if isinstance(response_obj, list):
        return response_obj, "root_list", None
    if isinstance(response_obj, dict):
        for key in ("results", "data", "items"):
            value = response_obj.get(key)
            if isinstance(value, list):
                return value, key, None
        return [], "single_object", response_obj
    return [], "none", None


def send_request(
    endpoint: str,
    payload_files: Sequence[Path],
    timeout_seconds: int,
) -> Dict[str, Any]:
    open_handles = []
    files: List[Tuple[str, Tuple[str, Any, str]]] = []
    try:
        for path in payload_files:
            handle = path.open("rb")
            open_handles.append(handle)
            files.append(("files", (path.name, handle, "application/pdf")))

        response = requests.post(endpoint, files=files, timeout=timeout_seconds)
        parsed_json = None
        json_error = None
        try:
            parsed_json = response.json()
        except ValueError as exc:
            json_error = str(exc)

        return {
            "http_status": response.status_code,
            "request_ok": response.ok,
            "response_text": response.text,
            "parsed_json": parsed_json,
            "json_error": json_error,
            "request_error": None,
        }
    except requests.RequestException as exc:
        return {
            "http_status": None,
            "request_ok": False,
            "response_text": "",
            "parsed_json": None,
            "json_error": None,
            "request_error": str(exc),
        }
    finally:
        for handle in open_handles:
            handle.close()


def build_response_map(
    items: Sequence[Any],
    payload_stems: Dict[str, str],
    filename_key: str,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    mapped: Dict[str, Any] = {}
    unexpected: List[Dict[str, Any]] = []

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            unexpected.append(
                {
                    "index": index,
                    "reason": "ITEM_NOT_OBJECT",
                    "details": f"Item type: {type(item).__name__}",
                }
            )
            continue

        filename_value = choose_filename_key(item, filename_key)
        if not filename_value:
            unexpected.append(
                {
                    "index": index,
                    "reason": "FILENAME_MISSING",
                    "details": f"Checked key mode: {filename_key}",
                }
            )
            continue

        stem_key = to_casefold_stem(filename_value)
        if not stem_key:
            unexpected.append(
                {
                    "index": index,
                    "reason": "FILENAME_EMPTY_STEM",
                    "details": filename_value,
                }
            )
            continue

        if stem_key not in payload_stems:
            unexpected.append(
                {
                    "index": index,
                    "reason": "FILENAME_NOT_IN_PAYLOAD",
                    "details": filename_value,
                }
            )
            continue

        if stem_key in mapped:
            unexpected.append(
                {
                    "index": index,
                    "reason": "DUPLICATE_RESPONSE_FOR_FILE",
                    "details": filename_value,
                }
            )
            continue

        mapped[stem_key] = item

    return mapped, unexpected


def load_reference(reference_path: Path) -> Tuple[Optional[Any], bool, bool, str]:
    if not reference_path.exists():
        return None, False, False, "Reference file not found."
    try:
        content = json.loads(reference_path.read_text(encoding="utf-8"))
        return content, True, True, ""
    except json.JSONDecodeError as exc:
        return None, True, False, f"Invalid JSON in reference file: {exc}"


def dot_path(parent: str, key: str) -> str:
    return key if parent == "" else f"{parent}.{key}"


def index_path(parent: str, idx: int) -> str:
    return f"[{idx}]" if parent == "" else f"{parent}[{idx}]"


def list_any_path(parent: str) -> str:
    return "[*]" if parent == "" else f"{parent}[*]"


def leaf_value_count(value: Any) -> int:
    if isinstance(value, dict):
        return sum(leaf_value_count(child) for child in value.values())
    if isinstance(value, list):
        return sum(leaf_value_count(child) for child in value)
    return 1


@dataclass
class FieldComparison:
    matched_leaf_count: int = 0
    mismatched_leaf_count: int = 0
    missing_leaf_count: int = 0
    unexpected_leaf_count: int = 0
    failed_fields: List[FailedField] = field(default_factory=list)


def merge_field_comparison(target: FieldComparison, child: FieldComparison) -> None:
    target.matched_leaf_count += child.matched_leaf_count
    target.mismatched_leaf_count += child.mismatched_leaf_count
    target.missing_leaf_count += child.missing_leaf_count
    target.unexpected_leaf_count += child.unexpected_leaf_count
    target.failed_fields.extend(child.failed_fields)


def compare_reference_fields(
    expected: Any,
    actual: Any,
    path: str = "",
    collect_failures: bool = False,
) -> FieldComparison:
    comparison = FieldComparison()

    if isinstance(expected, dict) and isinstance(actual, dict):
        for key, expected_value in expected.items():
            child_path = dot_path(path, key)
            if key not in actual:
                comparison.missing_leaf_count += leaf_value_count(expected_value)
                if collect_failures:
                    comparison.failed_fields.append(
                        FailedField(
                            path=child_path,
                            failure_type="MISSING_IN_RESPONSE",
                            expected_value=expected_value,
                            actual_value=MISSING_TEXT,
                        )
                    )
                continue
            child = compare_reference_fields(
                expected_value,
                actual[key],
                path=child_path,
                collect_failures=collect_failures,
            )
            merge_field_comparison(comparison, child)

        for key, actual_value in actual.items():
            if key in expected:
                continue
            comparison.unexpected_leaf_count += leaf_value_count(actual_value)
            if collect_failures:
                comparison.failed_fields.append(
                    FailedField(
                        path=dot_path(path, key),
                        failure_type="UNEXPECTED_IN_RESPONSE",
                        expected_value=MISSING_TEXT,
                        actual_value=actual_value,
                    )
                )
        return comparison

    if isinstance(expected, list) and isinstance(actual, list):
        expected_buckets: Dict[str, List[Any]] = {}
        actual_buckets: Dict[str, List[Any]] = {}

        for value in expected:
            token = canonical_json_text(value)
            expected_buckets.setdefault(token, []).append(value)
        for value in actual:
            token = canonical_json_text(value)
            actual_buckets.setdefault(token, []).append(value)

        for token in set(expected_buckets.keys()) | set(actual_buckets.keys()):
            expected_values = expected_buckets.get(token, [])
            actual_values = actual_buckets.get(token, [])
            shared = min(len(expected_values), len(actual_values))

            for idx in range(shared):
                comparison.matched_leaf_count += leaf_value_count(expected_values[idx])

            if len(expected_values) > shared:
                for value in expected_values[shared:]:
                    comparison.missing_leaf_count += leaf_value_count(value)
                    if collect_failures:
                        comparison.failed_fields.append(
                            FailedField(
                                path=list_any_path(path),
                                failure_type="MISSING_IN_RESPONSE",
                                expected_value=value,
                                actual_value=MISSING_TEXT,
                            )
                        )
            if len(actual_values) > shared:
                for value in actual_values[shared:]:
                    comparison.unexpected_leaf_count += leaf_value_count(value)
                    if collect_failures:
                        comparison.failed_fields.append(
                            FailedField(
                                path=list_any_path(path),
                                failure_type="UNEXPECTED_IN_RESPONSE",
                                expected_value=MISSING_TEXT,
                                actual_value=value,
                            )
                        )
        return comparison

    if not isinstance(expected, (dict, list)) and not isinstance(actual, (dict, list)):
        if type(expected) is type(actual) and expected == actual:
            comparison.matched_leaf_count += 1
        else:
            comparison.mismatched_leaf_count += 1
            if collect_failures:
                comparison.failed_fields.append(
                    FailedField(
                        path=path or "<root>",
                        failure_type="VALUE_MISMATCH",
                        expected_value=expected,
                        actual_value=actual,
                    )
                )
        return comparison

    comparison.missing_leaf_count += leaf_value_count(expected)
    comparison.unexpected_leaf_count += leaf_value_count(actual)
    if collect_failures:
        comparison.failed_fields.append(
            FailedField(
                path=path or "<root>",
                failure_type="VALUE_MISMATCH",
                expected_value=expected,
                actual_value=actual,
            )
        )
    return comparison


def diff_reference_fields(expected: Any, actual: Any, path: str = "") -> List[FailedField]:
    return compare_reference_fields(expected, actual, path=path, collect_failures=True).failed_fields


def status_counts(file_results: Sequence[FileRunResult]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for result in file_results:
        counts[result.status] = counts.get(result.status, 0) + 1
    return counts


def execute_run(
    run_index: int,
    args: argparse.Namespace,
    payload_files: Sequence[Path],
    payload_stems: Dict[str, str],
    reference_dir: Path,
    campaign_response_dir: Path,
) -> RunResult:
    run_dir = campaign_response_dir / f"run_{run_index:02d}"
    run_dir.mkdir(parents=True, exist_ok=True)
    response_json_path = run_dir / "response.json"
    run_meta_path = run_dir / "run_meta.json"

    request_result = send_request(args.endpoint, payload_files, args.timeout_seconds)
    run_error = request_result["request_error"]

    response_obj_for_archive: Any
    if request_result["parsed_json"] is not None:
        response_obj_for_archive = request_result["parsed_json"]
    else:
        response_obj_for_archive = {
            "error": "NON_JSON_RESPONSE" if run_error is None else "REQUEST_FAILED",
            "http_status": request_result["http_status"],
            "request_error": run_error,
            "json_error": request_result["json_error"],
            "raw_text": request_result["response_text"],
        }

    response_json_path.write_text(
        json.dumps(response_obj_for_archive, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    response_items: List[Any] = []
    item_source = "none"
    single_response_obj: Optional[Dict[str, Any]] = None
    if request_result["parsed_json"] is not None:
        response_items, item_source, single_response_obj = extract_response_items(request_result["parsed_json"])

    if single_response_obj is not None:
        if len(payload_files) == 1:
            only_payload = payload_files[0]
            response_map = {only_payload.stem.casefold(): single_response_obj}
            unexpected_items: List[Dict[str, Any]] = []
        else:
            response_map = {}
            unexpected_items = [
                {
                    "index": None,
                    "reason": "SINGLE_OBJECT_AMBIGUOUS_MULTI_PAYLOAD",
                    "details": "Received a single JSON object for multiple payload files.",
                }
            ]
    else:
        response_map, unexpected_items = build_response_map(response_items, payload_stems, args.filename_key)

    file_results: List[FileRunResult] = []
    responses_received = 0
    matches = 0
    missing_reference_count = 0
    invalid_reference_json_count = 0
    failed_field_count = 0

    for payload in payload_files:
        stem_key = payload.stem.casefold()
        response_item = response_map.get(stem_key)
        reference_path = reference_dir / f"{payload.stem}.json"

        if response_item is None:
            reference_obj, reference_present, reference_json_valid, reference_note = load_reference(reference_path)
            reference_leaf_count = (
                leaf_value_count(reference_obj) if reference_json_valid and reference_obj is not None else 0
            )
            note = "No response item matched this payload file."
            if reference_note:
                note = f"{note} {reference_note}"
            file_results.append(
                FileRunResult(
                    payload_file=payload.name,
                    stem=payload.stem,
                    status="MISSING_RESPONSE",
                    response_present=False,
                    reference_present=reference_present,
                    reference_json_valid=reference_json_valid,
                    match=False,
                    reference_path=str(reference_path),
                    note=note,
                    reference_leaf_count=reference_leaf_count,
                    response_leaf_count=0,
                    matched_leaf_count=0,
                    mismatched_leaf_count=0,
                    missing_leaf_count=reference_leaf_count,
                    unexpected_leaf_count=0,
                )
            )
            continue

        responses_received += 1
        reference_obj, reference_present, reference_json_valid, reference_note = load_reference(reference_path)
        if not reference_present:
            missing_reference_count += 1
            file_results.append(
                FileRunResult(
                    payload_file=payload.name,
                    stem=payload.stem,
                    status="MISSING_REFERENCE",
                    response_present=True,
                    reference_present=False,
                    reference_json_valid=False,
                    match=False,
                    reference_path=str(reference_path),
                    response_hash=canonical_hash(response_item),
                    note=reference_note,
                    reference_leaf_count=0,
                    response_leaf_count=0,
                    matched_leaf_count=0,
                    mismatched_leaf_count=0,
                    missing_leaf_count=0,
                    unexpected_leaf_count=0,
                )
            )
            continue

        if not reference_json_valid:
            invalid_reference_json_count += 1
            file_results.append(
                FileRunResult(
                    payload_file=payload.name,
                    stem=payload.stem,
                    status="INVALID_REFERENCE_JSON",
                    response_present=True,
                    reference_present=True,
                    reference_json_valid=False,
                    match=False,
                    reference_path=str(reference_path),
                    response_hash=canonical_hash(response_item),
                    note=reference_note,
                    reference_leaf_count=0,
                    response_leaf_count=0,
                    matched_leaf_count=0,
                    mismatched_leaf_count=0,
                    missing_leaf_count=0,
                    unexpected_leaf_count=0,
                )
            )
            continue

        reference_leaf_count = leaf_value_count(reference_obj)
        response_leaf_count = leaf_value_count(response_item)
        comparison = compare_reference_fields(
            reference_obj,
            response_item,
            collect_failures=args.report_failed_fields,
        )
        is_match = (
            comparison.mismatched_leaf_count == 0
            and comparison.missing_leaf_count == 0
            and comparison.unexpected_leaf_count == 0
            and comparison.matched_leaf_count == reference_leaf_count
        )
        failed_fields: List[FailedField] = comparison.failed_fields if args.report_failed_fields and not is_match else []
        if args.report_failed_fields:
            failed_field_count += len(failed_fields)

        if is_match:
            matches += 1
            status = "PASS"
            note = "Field-level exact parity passed (100% reference match, 0 unexpected fields)."
        else:
            status = "FAIL_MISMATCH"
            note = "Field-level exact parity failed."

        file_results.append(
            FileRunResult(
                payload_file=payload.name,
                stem=payload.stem,
                status=status,
                response_present=True,
                reference_present=True,
                reference_json_valid=True,
                match=is_match,
                reference_path=str(reference_path),
                response_hash=canonical_hash(response_item),
                failed_fields=failed_fields,
                note=note,
                reference_leaf_count=reference_leaf_count,
                response_leaf_count=response_leaf_count,
                matched_leaf_count=comparison.matched_leaf_count,
                mismatched_leaf_count=comparison.mismatched_leaf_count,
                missing_leaf_count=comparison.missing_leaf_count,
                unexpected_leaf_count=comparison.unexpected_leaf_count,
            )
        )

    payload_total = len(payload_files)
    missing_response_count = payload_total - responses_received
    total_reference_leaf_count = sum(item.reference_leaf_count for item in file_results)
    total_response_leaf_count = sum(item.response_leaf_count for item in file_results)
    total_matched_leaf_count = sum(item.matched_leaf_count for item in file_results)
    total_mismatched_leaf_count = sum(item.mismatched_leaf_count for item in file_results)
    total_missing_leaf_count = sum(item.missing_leaf_count for item in file_results)
    total_unexpected_leaf_count = sum(item.unexpected_leaf_count for item in file_results)
    overall_reference_match_percentage = (
        round((total_matched_leaf_count / total_reference_leaf_count) * 100, 2)
        if total_reference_leaf_count > 0
        else 0.0
    )

    run_meta = {
        "run_index": run_index,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "endpoint": args.endpoint,
        "http_status": request_result["http_status"],
        "request_ok": request_result["request_ok"],
        "request_error": request_result["request_error"],
        "json_error": request_result["json_error"],
        "item_source": item_source,
        "parsed_item_count": 1 if single_response_obj is not None else len(response_items),
        "payload_total": payload_total,
        "responses_received": responses_received,
        "matches": matches,
        "missing_response_count": missing_response_count,
        "missing_reference_count": missing_reference_count,
        "invalid_reference_json_count": invalid_reference_json_count,
        "unexpected_response_item_count": len(unexpected_items),
        "failed_field_count": failed_field_count,
        "total_reference_leaf_count": total_reference_leaf_count,
        "total_response_leaf_count": total_response_leaf_count,
        "total_matched_leaf_count": total_matched_leaf_count,
        "total_mismatched_leaf_count": total_mismatched_leaf_count,
        "total_missing_leaf_count": total_missing_leaf_count,
        "total_unexpected_leaf_count": total_unexpected_leaf_count,
        "overall_reference_match_percentage": overall_reference_match_percentage,
    }
    run_meta_path.write_text(json.dumps(run_meta, ensure_ascii=False, indent=2), encoding="utf-8")

    return RunResult(
        run_index=run_index,
        run_dir=run_dir,
        response_json_path=response_json_path,
        run_meta_path=run_meta_path,
        http_status=request_result["http_status"],
        request_ok=request_result["request_ok"],
        run_error=run_error,
        item_source=item_source,
        parsed_item_count=1 if single_response_obj is not None else len(response_items),
        unexpected_response_items=unexpected_items,
        file_results=file_results,
        payload_total=payload_total,
        responses_received=responses_received,
        matches=matches,
        missing_response_count=missing_response_count,
        missing_reference_count=missing_reference_count,
        invalid_reference_json_count=invalid_reference_json_count,
        failed_field_count=failed_field_count,
        total_reference_leaf_count=total_reference_leaf_count,
        total_response_leaf_count=total_response_leaf_count,
        total_matched_leaf_count=total_matched_leaf_count,
        total_mismatched_leaf_count=total_mismatched_leaf_count,
        total_missing_leaf_count=total_missing_leaf_count,
        total_unexpected_leaf_count=total_unexpected_leaf_count,
        overall_reference_match_percentage=overall_reference_match_percentage,
    )


def compute_consistency(run_results: Sequence[RunResult], payload_files: Sequence[Path]) -> ConsistencyResult:
    per_file: Dict[str, Dict[str, Any]] = {}
    consistent_files = 0
    files_with_all_runs_response = 0

    for payload in payload_files:
        stem = payload.stem.casefold()
        hashes: List[str] = []
        statuses: List[str] = []

        for run in run_results:
            matched = next((item for item in run.file_results if item.stem.casefold() == stem), None)
            if matched is None:
                statuses.append("NOT_FOUND")
                continue
            statuses.append(matched.status)
            if matched.response_hash:
                hashes.append(matched.response_hash)

        has_all = len(hashes) == len(run_results)
        is_consistent = has_all and len(set(hashes)) == 1
        if has_all:
            files_with_all_runs_response += 1
        if is_consistent:
            consistent_files += 1

        per_file[payload.name] = {
            "statuses": statuses,
            "hashes": hashes,
            "has_all_runs_response": has_all,
            "is_consistent": is_consistent,
        }

    return ConsistencyResult(
        consistent_files=consistent_files,
        files_with_all_runs_response=files_with_all_runs_response,
        per_file=per_file,
    )


def build_report_lines(
    args: argparse.Namespace,
    campaign_ts: str,
    payload_files: Sequence[Path],
    run_results: Sequence[RunResult],
    consistency: Optional[ConsistencyResult],
) -> List[str]:
    lines: List[str] = []
    payload_total = len(payload_files)
    total_failed_fields = sum(run.failed_field_count for run in run_results)

    lines.append("PDF API Regression Report")
    lines.append("=" * 80)
    lines.append(f"Campaign timestamp: {campaign_ts}")
    lines.append(f"Endpoint: {args.endpoint}")
    lines.append(f"Runs requested: {args.runs}")
    lines.append(f"Payload directory: {args.payload_dir}")
    lines.append(f"Reference directory: {args.reference_dir}")
    lines.append(f"Response archive directory: {args.response_archive_dir}")
    lines.append(f"Reports directory: {args.reports_dir}")
    lines.append(f"Filename key mode: {args.filename_key}")
    lines.append(f"Field-level failure reporting: {'ON' if args.report_failed_fields else 'OFF'}")
    lines.append("")
    lines.append(f"Payload files ({payload_total}):")
    for payload in payload_files:
        lines.append(f"- {payload.name}")
    lines.append("")

    for run in run_results:
        lines.append("-" * 80)
        lines.append(f"Run {run.run_index:02d}")
        lines.append("-" * 80)
        lines.append(f"Run directory: {run.run_dir}")
        lines.append(f"Response JSON: {run.response_json_path}")
        lines.append(f"Run metadata JSON: {run.run_meta_path}")
        lines.append(f"HTTP status: {run.http_status if run.http_status is not None else 'N/A'}")
        lines.append(f"Request successful: {run.request_ok}")
        if run.run_error:
            lines.append(f"Run error: {run.run_error}")
        lines.append(f"Response item source: {run.item_source}")
        lines.append(f"Parsed response item count: {run.parsed_item_count}")
        lines.append(f"Reference leaf fields: {run.total_reference_leaf_count}")
        lines.append(f"Response leaf fields: {run.total_response_leaf_count}")
        lines.append(f"Matched leaf fields: {run.total_matched_leaf_count}")
        lines.append(f"Mismatched leaf fields: {run.total_mismatched_leaf_count}")
        lines.append(f"Missing leaf fields: {run.total_missing_leaf_count}")
        lines.append(f"Unexpected leaf fields: {run.total_unexpected_leaf_count}")
        lines.append(f"Reference match %: {run.overall_reference_match_percentage:.2f}%")
        lines.append(f"Missing response count: {run.missing_response_count}")
        lines.append(f"Missing reference count: {run.missing_reference_count}")
        lines.append(f"Invalid reference JSON count: {run.invalid_reference_json_count}")
        lines.append(f"Unexpected response-item count: {len(run.unexpected_response_items)}")
        if args.report_failed_fields:
            lines.append(f"Total failed fields in run: {run.failed_field_count}")

        counts = status_counts(run.file_results)
        if counts:
            lines.append("Status breakdown:")
            for status in sorted(counts.keys()):
                lines.append(f"- {status}: {counts[status]}")

        lines.append("Per-file results:")
        for result in run.file_results:
            lines.append(f"- {result.payload_file}: {result.status}")
            lines.append(f"  reference: {result.reference_path}")
            lines.append(f"  note: {result.note}")
            lines.append(
                "  field_metrics: "
                f"reference={result.reference_leaf_count}; "
                f"response={result.response_leaf_count}; "
                f"matched={result.matched_leaf_count}; "
                f"mismatched={result.mismatched_leaf_count}; "
                f"missing={result.missing_leaf_count}; "
                f"unexpected={result.unexpected_leaf_count}"
            )
            if args.report_failed_fields:
                lines.append(f"  failed_field_count: {len(result.failed_fields)}")
                for failed in result.failed_fields:
                    lines.append(
                        "  field_failure: "
                        f"path={failed.path}; type={failed.failure_type}; "
                        f"expected={safe_json_string(failed.expected_value)}; "
                        f"actual={safe_json_string(failed.actual_value)}"
                    )

        if run.unexpected_response_items:
            lines.append("Unexpected response items:")
            for item in run.unexpected_response_items:
                lines.append(
                    "- "
                    f"index={item.get('index')}; reason={item.get('reason')}; details={item.get('details')}"
                )

        lines.append("")

    if consistency is not None:
        lines.append("=" * 80)
        lines.append("3-Run Consistency Summary")
        lines.append("=" * 80)
        lines.append(
            "Consistency (output hash stability): "
            f"{consistency.consistent_files} out of {payload_total} "
            f"({pct(consistency.consistent_files, payload_total)})"
        )
        lines.append(
            "All-runs coverage: "
            f"{consistency.files_with_all_runs_response} out of {payload_total} "
            f"({pct(consistency.files_with_all_runs_response, payload_total)})"
        )
        lines.append("Per-file consistency:")
        for filename in sorted(consistency.per_file.keys(), key=str.casefold):
            info = consistency.per_file[filename]
            lines.append(
                "- "
                f"{filename}: consistent={info['is_consistent']}; "
                f"has_all_runs_response={info['has_all_runs_response']}; "
                f"statuses={info['statuses']}"
            )
        lines.append("")

    if args.report_failed_fields:
        lines.append("=" * 80)
        lines.append("Global Failed-Field Totals")
        lines.append("=" * 80)
        lines.append(f"Total failed fields across all runs: {total_failed_fields}")
        lines.append("")

    return lines


def main() -> int:
    args = parse_args()

    payload_dir = Path(args.payload_dir)
    reference_dir = Path(args.reference_dir)
    response_archive_dir = Path(args.response_archive_dir)
    reports_dir = Path(args.reports_dir)

    payload_files = collect_payload_pdfs(payload_dir)
    payload_stems = {path.stem.casefold(): path.name for path in payload_files}

    campaign_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    campaign_response_dir = response_archive_dir / campaign_ts
    campaign_report_dir = reports_dir / campaign_ts
    campaign_response_dir.mkdir(parents=True, exist_ok=True)
    campaign_report_dir.mkdir(parents=True, exist_ok=True)

    run_results: List[RunResult] = []
    for run_index in range(1, args.runs + 1):
        run_result = execute_run(
            run_index=run_index,
            args=args,
            payload_files=payload_files,
            payload_stems=payload_stems,
            reference_dir=reference_dir,
            campaign_response_dir=campaign_response_dir,
        )
        run_results.append(run_result)
        if run_index < args.runs and args.pause_between_runs_seconds > 0:
            time.sleep(args.pause_between_runs_seconds)

    consistency = compute_consistency(run_results, payload_files) if args.runs == 3 else None
    report_lines = build_report_lines(args, campaign_ts, payload_files, run_results, consistency)
    report_text = "\n".join(report_lines).rstrip() + "\n"

    print(report_text)

    report_path = campaign_report_dir / "test_report.txt"
    report_path.write_text(report_text, encoding="utf-8")
    print(f"Report saved to: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
