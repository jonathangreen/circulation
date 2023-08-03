import datetime
from unittest.mock import patch

import pytest
import pytz

from core.util.datetime_helpers import (
    datetime_utc,
    from_timestamp,
    previous_months,
    strptime_utc,
    to_utc,
    utc_now,
)


class TestDatetimeUTC:
    @pytest.mark.parametrize(
        "time,formatted,isoformat",
        [
            ([2021, 1, 1], "2021-01-01T00:00:00", "2021-01-01T00:00:00+00:00"),
            ([1955, 11, 5, 12], "1955-11-05T12:00:00", "1955-11-05T12:00:00+00:00"),
            ([2015, 10, 21, 4, 29], "2015-10-21T04:29:00", "2015-10-21T04:29:00+00:00"),
            (
                [2015, 5, 9, 9, 30, 15],
                "2015-05-09T09:30:15",
                "2015-05-09T09:30:15+00:00",
            ),
        ],
    )
    def test_datetime_utc(self, time, formatted, isoformat):
        """`datetime_utc` is a wrapper around `datetime.datetime` but it also
        includes UTC information when it is created.
        """
        time_format = "%Y-%m-%dT%H:%M:%S"
        dt = datetime.datetime(*time, tzinfo=pytz.UTC)
        util_dt = datetime_utc(*time)

        # The util function is the same as the datetime function with
        # pytz UTC information.
        assert dt == util_dt
        # A datetime object is returned and works like any datetime object.
        assert util_dt.tzinfo == pytz.UTC
        assert util_dt.strftime(time_format) == formatted
        assert util_dt.isoformat() == isoformat
        assert util_dt.year == time[0]
        assert util_dt.month == time[1]
        assert util_dt.day == time[2]


class TestFromTimestamp:
    def test_from_timestamp(self):
        """`from_timestamp` is a wrapper around `datetime.fromtimestamp`
        that also includes UTC information.
        """
        ts = 0
        datetime_from_ts = datetime.datetime.fromtimestamp(ts, tz=pytz.UTC)
        util_from_ts = from_timestamp(ts)

        # The util function returns the right datetime object from a timestamp.
        assert datetime_from_ts == util_from_ts
        assert datetime_from_ts.strftime("%Y-%m-%d") == "1970-01-01"
        assert util_from_ts.strftime("%Y-%m-%d") == "1970-01-01"

        # The UTC information for this datetime object is the pytz UTC value.
        assert util_from_ts.tzinfo is not None
        assert util_from_ts.tzinfo == pytz.UTC


class TestUTCNow:
    def test_utc_now(self):
        """`utc_now` is a wrapper around `datetime.now` but it also includes
        UTC information.
        """
        datetime_now = datetime.datetime.now(tz=pytz.UTC)
        util_now = utc_now()

        # Same time but it's going to be off by a few milliseconds.
        assert (datetime_now - util_now).total_seconds() < 2

        # The UTC information for this datetime object is the pytz UTC value.
        assert util_now.tzinfo == pytz.UTC


class TestToUTC:
    def test_to_utc(self):
        # `utc` marks a naive datetime object as being UTC, or
        # converts a timezone-aware datetime object to UTC.
        d1 = datetime.datetime(2021, 1, 1)
        d2 = datetime.datetime.strptime("2020", "%Y")

        assert d1.tzinfo is None
        assert d2.tzinfo is None

        d1_utc = to_utc(d1)
        d2_utc = to_utc(d2)

        # The wrapper function is the same as the `replace` function,
        # just less verbose.
        assert d1_utc == d1.replace(tzinfo=pytz.UTC)
        assert d2_utc == d2.replace(tzinfo=pytz.UTC)
        # The timezone information is from pytz UTC.
        assert d1_utc.tzinfo == pytz.UTC
        assert d2_utc.tzinfo == pytz.UTC

        # Passing in None gets you None.
        assert to_utc(None) == None

        # Passing in a datetime that's already UTC is a no-op.
        assert d1_utc == to_utc(d1_utc)

        # Passing in a datetime from some other timezone converts to the
        # same time in UTC.
        d1 = datetime.datetime(2021, 1, 1)
        d1_eastern = d1_utc.astimezone(pytz.timezone("US/Eastern"))
        assert d1_utc == to_utc(d1_eastern)

    @pytest.mark.parametrize(
        "expect,date_string,format",
        [
            ([2021, 1, 1], "2021-01-01", "%Y-%m-%d"),
            ([1955, 11, 5, 12], "1955-11-05T12:00:00", "%Y-%m-%dT%H:%M:%S"),
        ],
    )
    def test_strptime_utc(self, expect, date_string, format):
        assert strptime_utc(date_string, format) == datetime_utc(*expect)

    def test_strptime_utc_error(self):
        # You can only use strptime_utc for time formats that don't
        # mention a timezone.
        with pytest.raises(ValueError) as excinfo:
            strptime_utc("2020-01-01T12:00:00+0300", "%Y-%m-%dT%H:%M:%S%z")
        assert (
            "Cannot use strptime_utc with timezone-aware format %Y-%m-%dT%H:%M:%S%z"
            in str(excinfo.value)
        )


class TestPreviousMonths:
    @pytest.mark.parametrize(
        "start,until,months",
        [
            (datetime.date(2000, 6, 1), datetime.date(2000, 12, 1), 6),
            (datetime.date(1999, 6, 1), datetime.date(2000, 12, 1), 18),
            (datetime.date(1990, 6, 1), datetime.date(2000, 12, 1), 126),
            (datetime.date(1999, 12, 1), datetime.date(2000, 12, 1), 12),
        ],
    )
    def test_boundaries(self, start, until, months):
        with patch("core.util.datetime_helpers.utc_now") as mock_utc_now:
            mock_utc_now.return_value = datetime.datetime(
                2000, 12, 15, 0, 0, 0, 0, tzinfo=pytz.UTC
            )
            actual_start, actual_until = previous_months(number_of_months=months)
            assert actual_start == start
            assert actual_until == until
