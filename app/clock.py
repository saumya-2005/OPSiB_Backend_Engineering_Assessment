"""The ONLY place scenario code gets 'now' from.

Rule/routing code must call `now()` (or receive it as an argument) and never
call datetime.now(). That keeps results deterministic and testable.
"""
from datetime import datetime

from app.config import get_settings


def now() -> datetime:
    return get_settings().evaluation_time
