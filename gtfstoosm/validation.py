"""Use the report of the MobilityData GTFS validator to decide whether to convert.

The validator (https://github.com/MobilityData/gtfs-validator) is run separately,
in its own container, and writes a report.json. This module only reads that report.
"""

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Notice:
    """One kind of finding of the validator, with how many times it occurs."""

    code: str
    severity: str
    count: int


def read_report(path: str) -> list[Notice]:
    """Read the notices of a validator report.json."""
    with open(path, encoding="utf-8") as f:
        report = json.load(f)
    try:
        return [
            Notice(
                code=notice["code"],
                severity=notice["severity"],
                count=int(notice.get("totalNotices", 0)),
            )
            for notice in report["notices"]
        ]
    except (KeyError, TypeError) as err:
        raise ValueError(f"{path} is not a GTFS validator report: {err}") from err


def blocking_errors(
    notices: list[Notice], ignored: set[str] | None = None
) -> list[Notice]:
    """The errors that must stop the conversion: all ERROR notices not ignored."""
    ignored = ignored or set()
    return [n for n in notices if n.severity == "ERROR" and n.code not in ignored]


def log_report(notices: list[Notice], ignored: set[str] | None = None) -> None:
    """Write the findings of the report to the log."""
    ignored = ignored or set()
    for notice in sorted(notices, key=lambda n: (n.severity, n.code)):
        if notice.severity == "ERROR" and notice.code not in ignored:
            logger.error(f"Validation error {notice.code} ({notice.count})")
        elif notice.severity in ("ERROR", "WARNING"):
            logger.warning(
                f"Validation {notice.severity.lower()} {notice.code} ({notice.count})"
            )
