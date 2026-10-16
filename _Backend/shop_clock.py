"""Shop-local wall time for the existing Manila-naive SQLite timestamps.

Use an explicit timezone at external boundaries; never depend on the host's
timezone (Render uses UTC). Existing stored timestamps remain unchanged.
"""
from datetime import datetime as _datetime, date
from zoneinfo import ZoneInfo

SHOP_ZONE = ZoneInfo('Asia/Manila')


class datetime(_datetime):
    @classmethod
    def now(cls, tz=None):
        value = super().now(tz or SHOP_ZONE)
        return value if tz is not None else value.replace(tzinfo=None)


