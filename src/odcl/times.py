"""Times and spans as people write them: 7d or 12h back from now, or ISO 8601."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .store import CatalogError, require


def span(value: str) -> timedelta:
    """7d or 12h as a length of time."""
    found = re.fullmatch(r"(\d{1,4})([dh])", value or "")
    require(found, "since", f"{value!r} is not a span of time: write days or hours, like 7d or 12h")
    return timedelta(**{"days" if found.group(2) == "d" else "hours": int(found.group(1))})


def moment(value: str, name: str) -> datetime:
    """A time as ISO 8601 (a date, or a date and time; UTC unless it says), or a span back from now such as 7d."""
    from . import store
    if re.fullmatch(r"\d{1,4}[dh]", value or ""):
        return store.now() - span(value)
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        raise CatalogError(name, f"{value!r} is not a time: write 2026-10-05T09:00:00Z, 2026-10-05, or a span back from now like 12h") from None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)
