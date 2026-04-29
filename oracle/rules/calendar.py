"""TARGET2 business-day calendar.

TARGET2 (the Eurosystem large-value payment system) is closed on:

* Saturdays and Sundays
* New Year's Day (1 January)
* Good Friday
* Easter Monday
* Labour Day (1 May)
* Christmas Day (25 December)
* Boxing Day (26 December)

The Easter-driven dates vary per year, so the holiday table is hardcoded
for 2024 through 2027 (dates cross-checked against the ECB TARGET2 closing
days calendar). Extending the table is a one-line edit per new year.

Design note — we do **not** pull ``python-holidays``: that package is
broad-scoped (national/regional holidays of many countries), not
TARGET2-specific, and introduces a transitive dependency we don't need.
A 24-line frozenset is auditable and cannot drift.
"""

from __future__ import annotations

from datetime import date, timedelta


# Fixed + computed TARGET2 closing days, 2024-2027.
# Easter dates reconstructed from the Gregorian computus; values match the
# official ECB TARGET2 calendar.
TARGET2_HOLIDAYS: frozenset[date] = frozenset(
    {
        # 2024 (Easter Sunday 31 Mar)
        date(2024, 1, 1),
        date(2024, 3, 29),   # Good Friday
        date(2024, 4, 1),    # Easter Monday
        date(2024, 5, 1),
        date(2024, 12, 25),
        date(2024, 12, 26),
        # 2025 (Easter Sunday 20 Apr)
        date(2025, 1, 1),
        date(2025, 4, 18),   # Good Friday
        date(2025, 4, 21),   # Easter Monday
        date(2025, 5, 1),
        date(2025, 12, 25),
        date(2025, 12, 26),
        # 2026 (Easter Sunday 5 Apr)
        date(2026, 1, 1),
        date(2026, 4, 3),    # Good Friday
        date(2026, 4, 6),    # Easter Monday
        date(2026, 5, 1),
        date(2026, 12, 25),
        date(2026, 12, 26),
        # 2027 (Easter Sunday 28 Mar)
        date(2027, 1, 1),
        date(2027, 3, 26),   # Good Friday
        date(2027, 3, 29),   # Easter Monday
        date(2027, 5, 1),
        date(2027, 12, 25),
        date(2027, 12, 26),
    }
)


SUPPORTED_YEARS: range = range(2024, 2028)  # end-exclusive — covers 2024-2027


def is_business_day(d: date) -> bool:
    """Return True iff ``d`` is a TARGET2 business day.

    Raises :class:`NotImplementedError` for years outside :data:`SUPPORTED_YEARS`
    so callers cannot silently evaluate against a year whose holidays have
    not been reviewed.
    """

    if d.year not in SUPPORTED_YEARS:
        raise NotImplementedError(
            f"TARGET2 calendar only covers {SUPPORTED_YEARS.start}-"
            f"{SUPPORTED_YEARS.stop - 1}; got {d.year}. Extend "
            f"TARGET2_HOLIDAYS in oracle/rules/calendar.py after reviewing "
            f"the official TARGET2 closing-days schedule for that year."
        )
    # weekday(): Mon=0 ... Sat=5, Sun=6
    if d.weekday() >= 5:
        return False
    return d not in TARGET2_HOLIDAYS


def add_business_days(start: date, n: int) -> date:
    """Advance ``start`` by ``n`` TARGET2 business days.

    Semantics:

    * ``n == 0`` returns ``start`` unchanged (even if ``start`` itself is a
      holiday or weekend — callers pick their own semantics for "today").
    * ``n > 0`` advances forward, skipping weekends and holidays. The first
      business day *strictly after* ``start`` counts as day 1.
    * Negative ``n`` raises ``ValueError``: ISDA grace periods are
      forward-only, and we'd rather fail loud than accidentally walk back.
    """

    if n < 0:
        raise ValueError(f"add_business_days: n must be non-negative, got {n}")
    current = start
    remaining = n
    while remaining > 0:
        current = current + timedelta(days=1)
        if is_business_day(current):
            remaining -= 1
    return current
