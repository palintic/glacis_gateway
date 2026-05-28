from datetime import datetime

from dateutil.parser import ParserError
from dateutil.parser import parse as dateutil_parse


def parse_event_time(value: str) -> datetime:
    """Parse an event timestamp to datetime, handling non-ISO formats (e.g. '28/04/2026 09:42 WIB')."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            return dateutil_parse(value)
        except ParserError:
            raise ValueError(f"Cannot parse event_time: {value!r}") from None
