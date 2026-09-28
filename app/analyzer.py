"""Version compatibility analysis for registered message definitions.

The analyzer compares two registered versions of the same ``device_type``
without needing concrete payloads:

1. Every numeric/bool field that a condition references (a *control field*)
   is partitioned into a finite number of **equivalence classes** by the
   integer constants appearing in the conditions (predicates are piecewise
   constant step functions, so two values inside the same interval make
   every condition on that field take the same truth value).
2. The Cartesian product of the partitions is enumerated. For each reachable
   representative assignment the definition is *simulated*: conditional
   fields are kept or skipped, and every present field gets a bit offset and
   the layout a total length.
3. Each reachable old-version layout is matched against a reachable
   new-version layout under the same representative values. Field names,
   types, bit widths, offsets, total length and checksum algorithm must all
   agree. If any old layout has no matching new layout the change is
   ``breaking`` and per-branch differences are returned.

Combining more than :data:`MAX_COMBINATIONS` equivalence classes is rejected
with :class:`CompatibilityError` (mapped to HTTP 422).
"""

import itertools
from typing import Any, Dict, List, Optional, Tuple

MAX_COMBINATIONS = 256


class CompatibilityError(Exception):
    """Raised when compatibility analysis cannot be performed (e.g. too many
    equivalence-class combinations)."""


# ---------------------------------------------------------------------------
# Control fields / equivalence classes
# ---------------------------------------------------------------------------


def _numeric_domain(field: Dict[str, Any]) -> Tuple[int, int]:
    """Raw-value domain [lo, hi] of a numeric/bool field."""
    n = field["bit_length"]
    if field["type"] == "bool":
        return 0, 1
    if field["type"] == "int":
        return -(1 << (n - 1)), (1 << (n - 1)) - 1
    return 0, (1 << n) - 1


def _field_index(fields: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {f["name"]: f for f in fields}


def _control_fields(
    fields: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Fields (name -> def) referenced by at least one condition."""
    by_name = _field_index(fields)
    controls: Dict[str, Dict[str, Any]] = {}
    for f in fields:
        cond = f.get("condition")
        if cond and cond["field"] in by_name:
            controls[cond["field"]] = by_name[cond["field"]]
    return controls


def _cuts_for(op: str, value: Any) -> List[int]:
    """Domain cut points introduced by a single predicate.

    A predicate changes truth value at these integer boundaries; the space
    between two adjacent cuts is an equivalence class on which the predicate
    is constantly true or constantly false.
    """
    if op == "eq":
        return [value, value + 1]
    if op == "ne":
        return [value, value + 1]
    if op == "gt":  # true strictly above value
        return [value + 1]
    if op == "ge":  # true from value upward
        return [value]
    if op == "lt":  # true strictly below value
        return [value]
    if op == "le":  # true up to value inclusive
        return [value + 1]
    if op == "in":
        # Membership of a set of points; each point is its own class.
        cuts: List[int] = []
        for v in sorted(value):
            cuts.extend([v, v + 1])
        return cuts
    raise CompatibilityError(f"unsupported condition operator: {op}")


def _partition(lo: int, hi: int, cuts: List[int]) -> List[Tuple[int, int]]:
    """Split the integer domain [lo, hi] into adjacent equivalence classes
    [start, end] at the (clipped, deduplicated, sorted) cut points."""
    boundaries = sorted({c for c in cuts if lo < c <= hi})
    segments: List[Tuple[int, int]] = []
    start = lo
    for boundary in boundaries:
        segments.append((start, boundary - 1))
        start = boundary
    segments.append((start, hi))
    return segments


# ---------------------------------------------------------------------------
# Layout simulation for one concrete representative assignment
# ---------------------------------------------------------------------------


def _eval_condition(condition: Dict[str, Any], values: Dict[str, int]) -> bool:
    actual = values[condition["field"]]
    op = condition["op"]
    expected = condition["value"]
    if op == "eq":
        return actual == expected
    if op == "ne":
        return actual != expected
    if op == "gt":
        return actual > expected
    if op == "ge":
        return actual >= expected
    if op == "lt":
        return actual < expected
    if op == "le":
        return actual <= expected
    if op == "in":
        return actual in expected
    raise CompatibilityError(f"unsupported condition operator: {op}")


def simulate_layout(
    fields: List[Dict[str, Any]],
    checksum_cfg: Optional[Dict[str, Any]],
    assignment: Dict[str, int],
) -> Dict[str, Any]:
    """Simulate a definition under concrete raw values for the control fields.

    Returns a layout descriptor (present fields with bit offsets plus total
    length and checksum algorithm), or ``{"reachable": False, "reason": ...}``
    when a condition cannot be evaluated because its control field was
    itself skipped by an earlier branch.
    """
    values: Dict[str, int] = {}  # raw values of present control fields
    pos = 0
    layout_fields: List[Dict[str, Any]] = []

    for field in fields:
        name = field["name"]
        cond = field.get("condition")
        if cond is not None:
            if cond["field"] not in values:
                return {
                    "reachable": False,
                    "reason": (
                        f"condition of '{name}' references '{cond['field']}', "
                        "which is absent on this branch"
                    ),
                }
            if not _eval_condition(cond, values):
                continue

        bit_offset = pos
        width = field["bit_length"]
        layout_fields.append(
            {
                "name": name,
                "type": field["type"],
                "bit_length": width,
                "bit_offset": bit_offset,
            }
        )
        # Only numeric/bool fields can be referenced by later conditions;
        # all control fields are numeric/bool by registration validation.
        if field["type"] in ("uint", "int", "bool"):
            if name in assignment:
                values[name] = assignment[name]
        pos += width

    return {
        "reachable": True,
        "fields": layout_fields,
        "total_bits": pos,
        "checksum_algorithm": checksum_cfg["algorithm"] if checksum_cfg else None,
    }


# ---------------------------------------------------------------------------
# Layout matching
# ---------------------------------------------------------------------------


def _field_signature(entry: Dict[str, Any]) -> Tuple[str, str, int, int]:
    return (entry["name"], entry["type"], entry["bit_length"], entry["bit_offset"])


def _layout_signature(layout: Dict[str, Any]) -> Tuple[Any, ...]:
    """Everything that must agree for two layouts to be wire-compatible."""
    return (
        tuple(_field_signature(f) for f in layout["fields"]),
        layout["total_bits"],
        layout["checksum_algorithm"],
    )


def _field_differences(
    old: Dict[str, Any], new: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Per-field diffs between two reachable layouts (names, types, bit widths
    and offsets)."""
    diffs: List[Dict[str, Any]] = []
    old_by_name = {f["name"]: f for f in old["fields"]}
    new_by_name = {f["name"]: f for f in new["fields"]}

    for name in sorted(set(old_by_name) | set(new_by_name)):
        of, nf = old_by_name.get(name), new_by_name.get(name)
        if of is None:
            diffs.append(
                {
                    "kind": "field_added",
                    "field": name,
                    "new": {
                        "type": nf["type"],
                        "bit_length": nf["bit_length"],
                        "bit_offset": nf["bit_offset"],
                    },
                }
            )
            continue
        if nf is None:
            diffs.append(
                {
                    "kind": "field_removed",
                    "field": name,
                    "old": {
                        "type": of["type"],
                        "bit_length": of["bit_length"],
                        "bit_offset": of["bit_offset"],
                    },
                }
            )
            continue
        props = ("type", "bit_length", "bit_offset")
        changed = {p: {"old": of[p], "new": nf[p]} for p in props if of[p] != nf[p]}
        if changed:
            diffs.append({"kind": "field_changed", "field": name, **changed})
    return diffs


# ---------------------------------------------------------------------------
# Top-level comparison
# ---------------------------------------------------------------------------


def analyze_compatibility(old_def: Dict[str, Any], new_def: Dict[str, Any]) -> Dict[str, Any]:
    """Compare two definitions of the same device type.

    Returns a report with ``compatible`` (bool), equivalence-class metadata,
    and per-branch layouts/differences. Raises :class:`CompatibilityError`
    when the joint partition produces more than :data:`MAX_COMBINATIONS`
    combinations, or when a control field's domains do not overlap.
    """
    old_fields, new_fields = old_def["fields"], new_def["fields"]
    old_checksum, new_checksum = old_def.get("checksum"), new_def.get("checksum")

    # Repeat-driven layouts depend on runtime counter values, so no finite set
    # of static layouts can be concluded. Refuse instead of giving a possibly
    # wrong compatibility verdict.
    for label, fields in (("old", old_fields), ("new", new_fields)):
        repeated = [f["name"] for f in fields if f.get("repeat")]
        if repeated:
            raise CompatibilityError(
                f"compatibility analysis does not support repeated fields; the {label} "
                f"definition contains repeat declaration(s) on: {', '.join(repeated)}"
            )

    old_by_name, new_by_name = _field_index(old_fields), _field_index(new_fields)
    old_controls, new_controls = _control_fields(old_fields), _control_fields(new_fields)

    # Union of control fields, ordered by first reference in either version
    # so the report is stable across runs.
    control_order: List[str] = []
    for fields in (old_fields, new_fields):
        for f in fields:
            cond = f.get("condition")
            if cond and cond["field"] not in control_order:
                control_order.append(cond["field"])

    partitions: Dict[str, List[Tuple[int, int]]] = {}
    partition_infos: List[Dict[str, Any]] = []

    for name in control_order:
        old_f, new_f = old_by_name.get(name), new_by_name.get(name)
        # Cuts are collected from the conditions of the version that actually
        # treats this field as a control.
        cuts: List[int] = []
        for fields, controls in (
            (old_fields, old_controls),
            (new_fields, new_controls),
        ):
            if name not in controls:
                continue
            for f in fields:
                cond = f.get("condition")
                if cond and cond["field"] == name:
                    cuts.extend(_cuts_for(cond["op"], cond["value"]))

        if old_f is not None and new_f is not None:
            old_lo, old_hi = _numeric_domain(old_f)
            new_lo, new_hi = _numeric_domain(new_f)
            lo, hi = max(old_lo, new_lo), min(old_hi, new_hi)
            if lo > hi:
                raise CompatibilityError(
                    f"control field '{name}' has no overlapping raw-value domain "
                    f"between versions ({old_f['type']}:{old_f['bit_length']} vs "
                    f"{new_f['type']}:{new_f['bit_length']})"
                )
            domain_note = None
            if (old_lo, old_hi) != (new_lo, new_hi):
                domain_note = "intersection of both versions' raw-value domains"
        elif old_f is not None:
            lo, hi = _numeric_domain(old_f)
            domain_note = "control field removed in the new version"
        else:
            lo, hi = _numeric_domain(new_f)
            domain_note = "control field introduced by the new version"

        segments = _partition(lo, hi, cuts)
        partitions[name] = segments
        info: Dict[str, Any] = {
            "field": name,
            "classes": len(segments),
            "representatives": [start for start, _ in segments],
        }
        if domain_note:
            info["domain"] = domain_note
        partition_infos.append(info)

    # Cap the Cartesian product before enumerating it.
    combinations = 1
    for segments in partitions.values():
        combinations *= len(segments)
    if combinations > MAX_COMBINATIONS:
        raise CompatibilityError(
            f"{combinations} equivalence-class combinations exceed the limit of "
            f"{MAX_COMBINATIONS}; simplify the conditional structure of the definitions"
        )

    names = list(partitions)
    branches: List[Dict[str, Any]] = []
    reachable_old = 0
    unreachable_old = 0
    breaking_count = 0

    for product in itertools.product(*partitions.values()):
        assignment = {name: segments[0] for name, segments in zip(names, product)}
        representative = {
            name: {"op": "==", "value": value} for name, value in assignment.items()
        }

        old_layout = simulate_layout(old_fields, old_checksum, assignment)
        new_layout = simulate_layout(new_fields, new_checksum, assignment)

        if not old_layout["reachable"]:
            unreachable_old += 1
            continue
        reachable_old += 1

        if not new_layout["reachable"]:
            breaking_count += 1
            branches.append(
                {
                    "representative": representative,
                    "compatible": False,
                    "differences": [
                        {
                            "kind": "branch_unreachable",
                            "message": new_layout["reason"],
                        }
                    ],
                    "old_layout": _layout_view(old_layout),
                }
            )
            continue

        differences: List[Dict[str, Any]] = []
        if _layout_signature(old_layout) != _layout_signature(new_layout):
            differences = _field_differences(old_layout, new_layout)
            if old_layout["total_bits"] != new_layout["total_bits"]:
                differences.append(
                    {
                        "kind": "total_length_changed",
                        "old": old_layout["total_bits"],
                        "new": new_layout["total_bits"],
                    }
                )
            if old_layout["checksum_algorithm"] != new_layout["checksum_algorithm"]:
                differences.append(
                    {
                        "kind": "checksum_algorithm_changed",
                        "old": old_layout["checksum_algorithm"],
                        "new": new_layout["checksum_algorithm"],
                    }
                )

        compatible = not differences
        if not compatible:
            breaking_count += 1
        branches.append(
            {
                "representative": representative,
                "compatible": compatible,
                **(
                    {"differences": differences}
                    if differences
                    else {}
                ),
                "old_layout": _layout_view(old_layout),
                "new_layout": _layout_view(new_layout),
            }
        )

    return {
        "device_type": old_def["device_type"],
        "old_version": old_def["version"],
        "new_version": new_def["version"],
        "compatible": breaking_count == 0,
        "status": "compatible" if breaking_count == 0 else "breaking",
        "equivalence_classes": partition_infos,
        "combination_count": combinations,
        "reachable_branches": reachable_old,
        "unreachable_combinations": unreachable_old,
        "breaking_branches": breaking_count,
        "branches": branches,
    }


def _layout_view(layout: Dict[str, Any]) -> Dict[str, Any]:
    """Serializable summary of a simulated layout."""
    return {
        "fields": [
            {
                "name": f["name"],
                "type": f["type"],
                "bit_length": f["bit_length"],
                "bit_offset": f["bit_offset"],
            }
            for f in layout["fields"]
        ],
        "total_bits": layout["total_bits"],
        "checksum_algorithm": layout["checksum_algorithm"],
    }
