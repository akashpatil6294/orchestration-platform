"""Timezone-aware cron parsing and next-run calculation.

Supports the standard five-field cron syntax (``minute hour day-of-month month
day-of-week``) with ``*``, ``a-b`` ranges, ``*/n`` steps, comma lists and the
common ``@daily``/``@hourly``/... macros. Day-of-month and day-of-week follow
the traditional cron OR rule: when both are restricted, either may match.

Implementations deliberately avoid an extra dependency so the scheduling
contract is explicit and testable, and DST transitions are handled by resolving
the local wall-clock time and letting the zone's offset decide the instant.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

FIELD_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
FIELD_NAMES = ("minute", "hour", "day of month", "month", "day of week")
MONTH_NAMES = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
DOW_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
DAY_NAMES = {0: "Sundays", 1: "Mondays", 2: "Tuesdays", 3: "Wednesdays", 4: "Thursdays", 5: "Fridays", 6: "Saturdays"}

MAX_SEARCH_MINUTES = 60 * 24 * 366 * 4  # four years, covers any valid expression


class CronError(ValueError):
    """Raised when a cron expression cannot be parsed."""


def normalize_expression(expression: str) -> str:
    text = (expression or "").strip().lower()
    if not text:
        raise CronError("Cron expression is required")
    if text.startswith("@"):
        if text not in MACROS:
            raise CronError(f"Unknown schedule macro '{text}'")
        return MACROS[text]
    return text


def _expand_field(text: str, index: int) -> set[int]:
    low, high = FIELD_BOUNDS[index]
    values: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"Empty value in {FIELD_NAMES[index]} field")
        step = 1
        if "/" in part:
            part, _, step_text = part.partition("/")
            if not step_text.isdigit() or int(step_text) < 1:
                raise CronError(f"Invalid step value in {FIELD_NAMES[index]} field")
            step = int(step_text)
        if part in {"*", ""}:
            start, end = low, high
        elif "-" in part.lstrip("-"):
            start_text, _, end_text = part.partition("-")
            start, end = _resolve(start_text, index), _resolve(end_text, index)
            if start > end:
                raise CronError(f"Descending range in {FIELD_NAMES[index]} field")
        else:
            start = end = _resolve(part, index)
        for value in range(start, end + 1, step):
            # Cron accepts both 0 and 7 for Sunday; normalise to 0.
            values.add(0 if index == 4 and value == 7 else value)
    if not values:
        raise CronError(f"No values in {FIELD_NAMES[index]} field")
    for value in values:
        if not low <= value <= high:
            raise CronError(f"{FIELD_NAMES[index].capitalize()} {value} is out of range {low}-{high}")
    return values


def _resolve(token: str, index: int) -> int:
    token = token.strip().lower()
    if token.isdigit():
        value = int(token)
        # Day of week accepts 7 as an alias for Sunday.
        return 0 if index == 4 and value == 7 else value
    table = MONTH_NAMES if index == 3 else DOW_NAMES if index == 4 else None
    if table and token[:3] in table:
        return table[token[:3]]
    raise CronError(f"Invalid value '{token}' in {FIELD_NAMES[index]} field")


class CronSchedule:
    """A parsed, validated cron expression."""

    __slots__ = ("expression", "minutes", "hours", "days", "months", "weekdays", "_dom_restricted", "_dow_restricted")

    def __init__(self, expression: str) -> None:
        self.expression = normalize_expression(expression)
        fields = self.expression.split()
        if len(fields) != 5:
            raise CronError("Cron expression must have exactly five fields: minute hour day-of-month month day-of-week")
        self.minutes = _expand_field(fields[0], 0)
        self.hours = _expand_field(fields[1], 1)
        self.days = _expand_field(fields[2], 2)
        self.months = _expand_field(fields[3], 3)
        self.weekdays = _expand_field(fields[4], 4)
        self._dom_restricted = fields[2].strip() != "*"
        self._dow_restricted = fields[4].strip() != "*"

    # ---------------------------------------------------------------- matching
    def matches(self, moment: datetime) -> bool:
        if moment.minute not in self.minutes or moment.hour not in self.hours or moment.month not in self.months:
            return False
        dom_match = moment.day in self.days
        dow_match = (moment.weekday() + 1) % 7 in self.weekdays
        if self._dom_restricted and self._dow_restricted:
            return dom_match or dow_match
        if self._dom_restricted:
            return dom_match
        if self._dow_restricted:
            return dow_match
        return True

    # ------------------------------------------------------------- next run
    def next_after(self, moment: datetime, tz: ZoneInfo | timezone = timezone.utc) -> datetime:
        """First matching local time strictly after ``moment``, returned in UTC."""
        local = moment.astimezone(tz).replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(MAX_SEARCH_MINUTES):
            if self.matches(local):
                return local.astimezone(timezone.utc)
            local += timedelta(minutes=1)
        raise CronError(f"No matching time found within four years for '{self.expression}'")

    def previous_before(self, moment: datetime, tz: ZoneInfo | timezone = timezone.utc) -> datetime:
        local = moment.astimezone(tz).replace(second=0, microsecond=0) - timedelta(minutes=1)
        for _ in range(MAX_SEARCH_MINUTES):
            if self.matches(local):
                return local.astimezone(timezone.utc)
            local -= timedelta(minutes=1)
        raise CronError(f"No matching time found within four years for '{self.expression}'")

    def describe(self) -> str:
        """A short human phrase for the UI, falling back to the raw expression."""
        if self.minutes == set(range(60)) and self.hours == set(range(24)):
            return "Every minute"
        every_minute = self.minutes == set(range(60))
        single_minute = len(self.minutes) == 1
        single_hour = len(self.hours) == 1
        if single_minute and single_hour:
            minute = next(iter(self.minutes))
            hour = next(iter(self.hours))
            clock = f"{hour:02d}:{minute:02d}"
            if not self._dom_restricted and not self._dow_restricted:
                return "Daily at 00:00" if clock == "00:00" else f"Daily at {clock}"
            if self._dom_restricted and not self._dow_restricted:
                day = min(self.days)
                return f"Monthly on day {day} at {clock}"
            if self._dow_restricted and not self._dom_restricted:
                if self.weekdays == {0, 1, 2, 3, 4, 5, 6}:
                    return f"Daily at {clock}"
                if self.weekdays == {1, 2, 3, 4, 5}:
                    return f"Weekdays at {clock}"
                if self.weekdays == {0, 6}:
                    return f"Weekends at {clock}"
                names = [DAY_NAMES[day] for day in sorted(self.weekdays)]
                return f"{', '.join(names)} at {clock}"
        if single_minute and self.hours == set(range(24)):
            minute = next(iter(self.minutes))
            return "Hourly, on the hour" if minute == 0 else f"Hourly at :{minute:02d}"
        if every_minute and single_hour:
            return f"Every minute during hour {next(iter(self.hours)):02d}"
        if len(self.minutes) == 1 and len(self.hours) == 1:
            return self.expression
        if len(self.minutes) > 1 and len(self.hours) == 1:
            minutes = ", ".join(f"{minute:02d}" for minute in sorted(self.minutes))
            return f"At :{minutes} past hour {next(iter(self.hours)):02d}"
        return self.expression


def parse(expression: str) -> CronSchedule:
    return CronSchedule(expression)


def validate_expression(expression: str) -> str | None:
    """Return an error message, or ``None`` when the expression is valid."""
    try:
        CronSchedule(expression)
    except CronError as exc:
        return str(exc)
    return None


def resolve_timezone(name: str) -> ZoneInfo | timezone:
    if not name:
        return timezone.utc
    if name.upper() in {"UTC", "ETC/UTC", "GMT"}:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CronError(f"Unknown timezone '{name}'") from exc


def next_run_at(expression: str, timezone_name: str, *, after: datetime | None = None) -> datetime:
    """Compute the next UTC fire time for a schedule."""
    tz = resolve_timezone(timezone_name)
    reference = after or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return CronSchedule(expression).next_after(reference, tz)


def previous_run_at(expression: str, timezone_name: str, *, before: datetime | None = None) -> datetime:
    tz = resolve_timezone(timezone_name)
    reference = before or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return CronSchedule(expression).previous_before(reference, tz)


def upcoming_runs(expression: str, timezone_name: str, count: int = 5, *, after: datetime | None = None) -> list[datetime]:
    """The next ``count`` fire times, for the schedule preview in the UI."""
    tz = resolve_timezone(timezone_name)
    schedule = CronSchedule(expression)
    reference = after or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    results: list[datetime] = []
    cursor = reference
    for _ in range(max(1, min(count, 20))):
        cursor = schedule.next_after(cursor, tz)
        results.append(cursor)
    return results


def slot_key(moment: datetime) -> str:
    """Canonical identity for one schedule occurrence (minute resolution, UTC)."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def parse_iso(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CronError(f"Invalid ISO-8601 timestamp '{value}'") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


CRON_EXAMPLE_PATTERN = re.compile(r"^\s*(@[a-z]+|\S+\s+\S+\s+\S+\s+\S+\s+\S+)\s*$")

__all__ = [
    "CronError",
    "CronSchedule",
    "next_run_at",
    "parse",
    "parse_iso",
    "previous_run_at",
    "resolve_timezone",
    "slot_key",
    "upcoming_runs",
    "validate_expression",
]
