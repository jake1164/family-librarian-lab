"""Publication-status assertions: reusable, dated, and cautious about what a missing date means.

A fixture can assert, for one series member, where the work stood on a stated date:

``planned``    announced as coming, with no release date announced
``scheduled``  a future release date is announced
``published``  released on or before the evaluation date
``unknown``    the references cannot say (always reviewed by a person, never scored)

Two rules keep this from scoring a provider wrongly:

* A provider that returns no date has said nothing. A missing date is never read as "not yet
  published", so that fact is reported as omitted, not as agreement.
* A reference that is not ``high`` confidence cannot mark a provider wrong. A contradiction
  with a medium or low reference becomes ``needs_review``; only a ``high`` one is a difference.
  A ``low`` reference does not even confirm: agreement with it is reviewed too.

``scheduled`` and ``published`` assertions that carry the member's release date follow the
calendar: the effective status is recomputed from that date at each evaluation date, so a
scheduled title becomes published on its release day without editing the fixture.
"""

from __future__ import annotations

import calendar
import re
from datetime import date
from typing import Any, Mapping

PLANNED = "planned"
SCHEDULED = "scheduled"
PUBLISHED = "published"
UNKNOWN = "unknown"

PUBLICATION_STATUSES = frozenset({PLANNED, SCHEDULED, PUBLISHED, UNKNOWN})

#: Keys of the ``publication_status`` mapping on a series member expectation.
ASSERTION_KEYS = {"status", "as_of", "confidence", "evidence"}

_PARTIAL_DATE = re.compile(r"^\s*(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?\s*$")


def _period(value: object) -> tuple[date, date] | None:
    """The first and last day a (possibly partial) ISO date could mean."""

    match = _PARTIAL_DATE.match(str(value or ""))
    if not match:
        return None
    year = int(match.group(1))
    month = int(match.group(2)) if match.group(2) else None
    day = int(match.group(3)) if match.group(3) else None
    try:
        if month is None:
            return date(year, 1, 1), date(year, 12, 31)
        if day is None:
            return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
        return date(year, month, day), date(year, month, day)
    except ValueError:
        return None


def _day(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def classify_date(value: object, as_of: str | None) -> str:
    """``published``, ``scheduled`` or ``unknown`` for a release date seen on ``as_of``.

    A year or a month that still contains the evaluation date is ``unknown``: it is neither
    clearly in the past nor clearly in the future.
    """

    today = _day(as_of)
    period = _period(value)
    if today is None or period is None:
        return UNKNOWN
    start, end = period
    if end <= today:
        return PUBLISHED
    if start > today:
        return SCHEDULED
    return UNKNOWN


def effective_status(
    assertion: Mapping[str, Any], published: object, as_of: str | None
) -> tuple[str, str]:
    """The status the fixture implies on ``as_of`` and why."""

    status = str(assertion.get("status"))
    if status in (SCHEDULED, PUBLISHED) and published:
        derived = classify_date(published, as_of)
        return derived, f"release date {published} against evaluation date {as_of}: {derived}"
    if status == PUBLISHED:
        asserted = _day(assertion.get("as_of"))
        today = _day(as_of)
        if asserted is not None and today is not None and today < asserted:
            return UNKNOWN, f"published was asserted on {assertion.get('as_of')}, after the evaluation date {as_of}"
    return status, f"asserted {status} on {assertion.get('as_of')}"


__all__ = [
    "ASSERTION_KEYS",
    "PLANNED",
    "PUBLISHED",
    "PUBLICATION_STATUSES",
    "SCHEDULED",
    "UNKNOWN",
    "classify_date",
    "effective_status",
]
