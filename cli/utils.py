from datetime import datetime
from typing import Union


PASSWORD_ENVIRON_NAME = "MARZBAN_ADMIN_PASSWORD"


def readable_datetime(date_time: Union[datetime, int, None], include_date: bool = True, include_time: bool = True):
    def get_datetime_format():
        dt_format = ""
        if include_date:
            dt_format += "%d %B %Y"
        if include_time:
            if dt_format:
                dt_format += ", "
            dt_format += "%H:%M:%S"

        return dt_format

    if isinstance(date_time, int):
        date_time = datetime.fromtimestamp(date_time)

    return date_time.strftime(get_datetime_format()) if date_time else "-"
