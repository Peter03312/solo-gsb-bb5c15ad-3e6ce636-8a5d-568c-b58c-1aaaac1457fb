"""Batch message profiling: field presence and numeric statistics over samples.

One :class:`VersionProfile` accumulates the statistics for a single registered
``(device_type, version)``; samples resolved to different versions never share
state, so identically named fields across versions stay isolated. Fields that a
conditional branch skips on a given message are absent from the parser result
and therefore contribute neither presence nor a numeric denominator.
"""

from typing import Any, Dict, List, Optional


class _NumericStats:
    """Running min/max/mean over numeric scalar values or repeat elements.

    ``minimum``/``maximum`` keep the original values (ints stay ints); only the
    running total uses a float, so the mean is always returned as a float.
    """

    def __init__(self):
        self.count = 0
        self.total = 0.0
        self.minimum: Optional[Any] = None
        self.maximum: Optional[Any] = None

    def add(self, value: Any) -> None:
        self.count += 1
        self.total += value
        if self.minimum is None or value < self.minimum:
            self.minimum = value
        if self.maximum is None or value > self.maximum:
            self.maximum = value

    def mean(self) -> float:
        return round(self.total / self.count, 6)


class VersionProfile:
    """Field statistics for one version, emitted in field-definition order."""

    def __init__(self, record: Dict[str, Any]):
        self.device_type = record["device_type"]
        self.version = record["version"]
        self.active = bool(record["active"])
        self.fields: List[Dict[str, Any]] = record["fields"]
        self.accepted_count = 0
        self.rejected: List[Dict[str, Any]] = []
        self._present: Dict[str, int] = {f["name"]: 0 for f in self.fields}
        self._scalars: Dict[str, _NumericStats] = {
            f["name"]: _NumericStats()
            for f in self.fields
            if f.get("repeat") is None and f["type"] in ("uint", "int")
        }
        self._elements: Dict[str, _NumericStats] = {
            f["name"]: _NumericStats()
            for f in self.fields
            if f.get("repeat") is not None and f["type"] in ("uint", "int")
        }

    def add_accepted(self, result: Dict[str, Any]) -> None:
        """Fold one fully validated parse result into the profile. Only fields
        actually present in the result count; condition-skipped fields do not."""
        self.accepted_count += 1
        for entry in result["fields"]:
            name = entry["name"]
            self._present[name] += 1
            if name in self._scalars:
                self._scalars[name].add(entry["value"])
            elif name in self._elements:
                # Each expanded element is one statistical observation.
                for value in entry["value"]:
                    self._elements[name].add(value)

    def build(self) -> Dict[str, Any]:
        field_stats: List[Dict[str, Any]] = []
        for field in self.fields:
            name = field["name"]
            stats: Dict[str, Any] = {
                "name": name,
                "type": field["type"],
                "bit_length": field["bit_length"],
                "present_count": self._present[name],
            }
            if field.get("repeat") is not None:
                numeric = self._elements.get(name)
            else:
                numeric = self._scalars.get(name)
            if numeric is not None:
                # repeat fields report the element denominator explicitly;
                # absent fields (or empty arrays) yield null aggregates.
                if field.get("repeat") is not None:
                    stats["element_count"] = numeric.count
                if numeric.count:
                    stats["min"] = numeric.minimum
                    stats["max"] = numeric.maximum
                    stats["mean"] = numeric.mean()
                else:
                    stats["min"] = None
                    stats["max"] = None
                    stats["mean"] = None
            field_stats.append(stats)
        return {
            "version": self.version,
            "definition_active": self.active,
            "accepted_count": self.accepted_count,
            "rejected": self.rejected,
            "fields": field_stats,
        }
