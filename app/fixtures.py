"""Regression-fixture snapshot capture and field-by-field comparison.

A snapshot is the parser output reduced to the four regression dimensions:

* ``fields[].raw_value``  -- numeric fields carry their raw integer (a list for
  repeated fields); bool is normalized to 0/1; hex/ascii have no raw integer
  and therefore compare by field name only (``raw_value`` is ``null``);
* ``branch_path[]``       -- ``matched``/``action`` per conditional field;
* ``checksum_valid``      -- the checksum verification result (``null`` when the
  target definition has no checksum configured).
"""

from typing import Any, Dict, List, Optional


def field_raw_value(entry: Dict[str, Any]) -> Any:
    """Raw comparison value for one parsed field entry."""
    if "raw_value" in entry:
        return entry["raw_value"]  # uint/int, list for repeated fields
    value = entry.get("value")
    if isinstance(value, bool):  # parser omits raw_value for bool
        return int(value)
    return None  # hex/ascii: presence is compared by name only


def capture_snapshot(parse_result: Dict[str, Any]) -> Dict[str, Any]:
    """Build the persisted baseline snapshot from a /parse-style result."""
    return {
        "fields": [
            {"name": f["name"], "raw_value": field_raw_value(f)}
            for f in parse_result["fields"]
        ],
        "branch_path": [
            {
                "field": b["field"],
                "matched": b["matched"],
                "action": b["action"],
            }
            for b in parse_result["branch_path"]
        ],
        "checksum_valid": (
            parse_result["checksum"]["valid"]
            if parse_result.get("checksum") is not None
            else None
        ),
    }


def verify_expectation(expect: Dict[str, Any], snapshot: Dict[str, Any]) -> List[str]:
    """Check a client-supplied expectation against a freshly captured snapshot.

    Every part of the expectation is optional and asserts a subset of the
    captured baseline. Returns a list of human-readable problems (empty == ok).
    """
    problems: List[str] = []

    if expect.get("fields") is not None:
        actual_fields = {f["name"]: f["raw_value"] for f in snapshot["fields"]}
        for item in expect["fields"]:
            name = item["name"]
            if name not in actual_fields:
                problems.append(
                    f"expect.fields references '{name}', which is not parsed by the "
                    f"baseline definition"
                )
            elif "raw_value" in item and item["raw_value"] != actual_fields[name]:
                problems.append(
                    f"expect.fields '{name}' raw_value {item['raw_value']!r} "
                    f"!= parsed baseline {actual_fields[name]!r}"
                )

    if expect.get("branch_path") is not None:
        actual_branches = {b["field"]: b for b in snapshot["branch_path"]}
        for item in expect["branch_path"]:
            field = item["field"]
            if field not in actual_branches:
                problems.append(
                    f"expect.branch_path references '{field}', which has no condition "
                    f"in the baseline definition"
                )
                continue
            if "matched" in item and item["matched"] != actual_branches[field]["matched"]:
                problems.append(
                    f"expect.branch_path '{field}' matched {item['matched']!r} "
                    f"!= parsed baseline {actual_branches[field]['matched']!r}"
                )
            if "action" in item and item["action"] != actual_branches[field]["action"]:
                problems.append(
                    f"expect.branch_path '{field}' action {item['action']!r} "
                    f"!= parsed baseline {actual_branches[field]['action']!r}"
                )

    if "checksum_valid" in expect and expect["checksum_valid"] != snapshot["checksum_valid"]:
        problems.append(
            f"expect.checksum_valid {expect['checksum_valid']!r} != parsed baseline "
            f"{snapshot['checksum_valid']!r}"
        )

    return problems


def _diff_field_lists(
    baseline: List[Dict[str, Any]],
    actual: List[Dict[str, Any]],
) -> Dict[str, Any]:
    base = {f["name"]: f["raw_value"] for f in baseline}
    current = {f["name"]: f["raw_value"] for f in actual}

    added = [{"name": n, "raw_value": current[n]} for n in current if n not in base]
    removed = [{"name": n, "raw_value": base[n]} for n in base if n not in current]
    changed = [
        {
            "name": n,
            "baseline_raw_value": base[n],
            "actual_raw_value": current[n],
        }
        for n in current
        if n in base and current[n] != base[n]
    ]
    unchanged = [
        {"name": n, "raw_value": current[n]}
        for n in current
        if n in base and current[n] == base[n]
    ]
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged": unchanged,
        "passed": not (added or removed or changed),
    }


def _diff_branch_lists(
    baseline: List[Dict[str, Any]],
    actual: List[Dict[str, Any]],
) -> Dict[str, Any]:
    base = {b["field"]: b for b in baseline}
    current = {b["field"]: b for b in actual}

    def brief(b: Dict[str, Any]) -> Dict[str, Any]:
        return {"field": b["field"], "matched": b["matched"], "action": b["action"]}

    added = [brief(current[n]) for n in current if n not in base]
    removed = [brief(base[n]) for n in base if n not in current]
    changed = []
    unchanged = []
    for n in current:
        if n not in base:
            continue
        if current[n]["matched"] != base[n]["matched"] or current[n]["action"] != base[n]["action"]:
            changed.append(
                {"field": n, "baseline": brief(base[n]), "actual": brief(current[n])}
            )
        else:
            unchanged.append(brief(current[n]))
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged": unchanged,
        "passed": not (added or removed or changed),
    }


def compare_snapshots(
    baseline: Dict[str, Any],
    actual: Dict[str, Any],
) -> Dict[str, Any]:
    """Compare an actual parse snapshot against the stored baseline.

    Never raises on differences: every failing item is reported individually so
    a single bad field/branch/checksum does not hide the remaining results.
    """
    fields = _diff_field_lists(baseline["fields"], actual["fields"])
    branches = _diff_branch_lists(baseline["branch_path"], actual["branch_path"])

    baseline_checksum = baseline.get("checksum_valid")
    actual_checksum = actual.get("checksum_valid")
    checksum = {
        "baseline_valid": baseline_checksum,
        "actual_valid": actual_checksum,
        "passed": baseline_checksum == actual_checksum,
    }

    errors: List[str] = []
    for item in fields["removed"]:
        errors.append(
            f"field '{item['name']}' missing in target parse "
            f"(baseline raw_value {item['raw_value']!r})"
        )
    for item in fields["added"]:
        errors.append(
            f"field '{item['name']}' newly appears in target parse "
            f"(raw_value {item['raw_value']!r})"
        )
    for item in fields["changed"]:
        errors.append(
            f"field '{item['name']}' raw_value changed: baseline "
            f"{item['baseline_raw_value']!r} -> actual {item['actual_raw_value']!r}"
        )
    for item in branches["removed"]:
        errors.append(f"branch condition on '{item['field']}' missing in target parse")
    for item in branches["added"]:
        errors.append(
            f"branch condition on '{item['field']}' newly appears in target parse"
        )
    for item in branches["changed"]:
        errors.append(
            f"branch on '{item['field']}' changed: baseline matched "
            f"{item['baseline']['matched']} ({item['baseline']['action']}) -> actual "
            f"matched {item['actual']['matched']} ({item['actual']['action']})"
        )
    if not checksum["passed"]:
        errors.append(
            f"checksum validation changed: baseline valid {baseline_checksum!r} "
            f"-> actual valid {actual_checksum!r}"
        )

    total_checks = (
        len(fields["added"]) + len(fields["removed"])
        + len(fields["changed"]) + len(fields["unchanged"])
        + len(branches["added"]) + len(branches["removed"])
        + len(branches["changed"]) + len(branches["unchanged"])
        + 1
    )
    passed_checks = len(fields["unchanged"]) + len(branches["unchanged"]) + int(checksum["passed"])

    return {
        "fields": fields,
        "branch_path": branches,
        "checksum": checksum,
        "total_checks": total_checks,
        "passed_checks": passed_checks,
        "pass_rate": round(passed_checks / total_checks, 4) if total_checks else 0.0,
        "passed": fields["passed"] and branches["passed"] and checksum["passed"],
        "errors": errors,
    }
