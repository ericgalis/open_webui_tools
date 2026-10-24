import asyncio
import json
import os
from datetime import date

import pytest

import health_data as hd

TODAY = date(2026, 10, 23)  # a Friday


# ---------------------------------------------------------------- pure functions

def test_dates_and_ranges():
    assert hd.parse_day("", TODAY, TODAY) == TODAY
    assert hd.parse_day("yesterday", TODAY, TODAY) == date(2026, 10, 22)
    assert hd.parse_day("3 weeks ago", TODAY, TODAY) == date(2026, 10, 2)
    assert hd.parse_day("1 month ago", date(2026, 3, 31), TODAY) == date(2026, 2, 28)
    assert hd.resolve_range("", "", TODAY) == (date(2026, 9, 23), date(2026, 10, 22))
    assert hd.resolve_range("last 7 days", "", TODAY) == (date(2026, 10, 16), date(2026, 10, 22))
    assert hd.resolve_range("last 3 months", "2026-10-22", TODAY) == (date(2026, 7, 23), date(2026, 10, 22))
    assert hd.resolve_range("last week", "", TODAY) == (date(2026, 10, 12), date(2026, 10, 18))
    assert hd.resolve_range("last month", "", TODAY) == (date(2026, 9, 1), date(2026, 9, 30))
    assert hd.resolve_range("this month", "", TODAY) == (date(2026, 10, 1), date(2026, 10, 22))
    with pytest.raises(hd.ToolError):
        hd.resolve_range("2026-10-10", "2026-10-01", TODAY)
    with pytest.raises(hd.ToolError):
        hd.parse_day("next tuesday", TODAY, TODAY)


def test_statistics():
    assert hd.quantile([1, 2, 3, 4], 0.5) == 2.5
    assert hd.quantile([10, 20, 30, 40, 50], 0.1) == 14.0
    assert hd.sd([2, 4, 4, 4, 5, 5, 7, 9]) == pytest.approx(2.13809, rel=1e-5)
    slope, r2 = hd.linfit([(0, 1), (1, 3), (2, 5), (3, 7)])
    assert slope == 2 and r2 == 1
    assert hd.pearson([1, 2, 3, 4], [2, 4, 6, 9]) == pytest.approx(11.5 / 133.75 ** 0.5)
    assert hd.spearman([1, 2, 3, 4], [10, 20, 25, 100]) == 1.0
    assert hd.pearson([1, 1, 1], [1, 2, 3]) is None


def test_baseline_direction_uses_metric_polarity():
    base = {"mean": 52.0, "sd": 1.0, "days_with_data": 28}
    worse = hd.compare_to_baseline({"mean": 55.0}, base, "resting_hr")
    assert worse["z"] == 3.0 and worse["direction"] == "worse" and worse["notable"]
    better = hd.compare_to_baseline({"mean": 7.0}, {"mean": 6.0, "sd": 0.5, "days_with_data": 28}, "sleep_h")
    assert better["direction"] == "better"
    neutral = hd.compare_to_baseline({"mean": 79.0}, {"mean": 80.0, "sd": 0.5, "days_with_data": 28}, "weight_kg")
    assert neutral["direction"] == "lower"
    few = hd.compare_to_baseline({"mean": 60.0}, {"mean": 52.0, "sd": 1.0, "days_with_data": 3}, "resting_hr")
    assert few["z"] is None and not few["notable"]


def test_targets_daily_and_weekly():
    days = [(date(2026, 10, d), v) for d, v in [(1, 7.0), (2, 8.0), (3, 7.6)]]
    t = hd.check_target("sleep_h", {"min": 7.5}, days, 3)
    assert t["days_met"] == 2 and t["met"] is True
    w = hd.check_target("strength_sessions", {"min": 3, "per": "week"}, [(date(2026, 10, d), 1) for d in range(1, 3)], 7)
    assert w["value_per_week"] == 2.0 and w["met"] is False


def test_sql_guard():
    assert hd.guard_sql(" select 1; ") == "select 1"
    assert hd.guard_sql("WITH a AS (SELECT 1) SELECT * FROM a") .startswith("WITH")
    for bad in ("DELETE FROM core.sleep", "select 1; drop table x", "copy core.sleep to stdout", ""):
        with pytest.raises(hd.ToolError):
            hd.guard_sql(bad)


# ---------------------------------------------------------------- against a seeded database

DSN = os.environ.get("HEALTH_TEST_RO_DSN")
needs_db = pytest.mark.skipif(not DSN, reason="needs the seeded scratch database (run_tests.sh)")
USER = {"id": "user-1"}


def tool():
    t = hd.Tools()
    t.valves.PERSON_DSNS = json.dumps({"user-1": DSN})
    return t


def call(coro):
    return json.loads(asyncio.run(coro))


@needs_db
def test_weekly_report_flags_the_change_and_checks_targets():
    r = call(tool().health_report("week", "2026-10-21", __user__=USER))
    assert r["period"] == {"type": "week", "start": "2026-10-15", "end": "2026-10-21", "days": 7, "days_with_any_data": 7}
    notable = {n["metric"]: n for n in r["notable_changes"]}
    assert notable["sleep_h"]["direction"] == "worse" and notable["resting_hr"]["direction"] == "worse"
    sleep = r["metrics"]["sleep_h"]
    assert sleep["mean"] == pytest.approx(6.0 + 0.25 * (0 + 1 + 2 + 0 + 1 + 2 + 0) / 7, abs=0.01)
    targets = {t["metric"]: t for t in r["targets"]}
    assert targets["sleep_h"]["met"] is False
    assert targets["strength_sessions"]["value_per_week"] == 3.0 and targets["strength_sessions"]["met"]
    assert "food log" in r["data_gaps"] and len(r["activities"]) == 3


@needs_db
def test_results_are_deterministic_and_match_sql():
    t = tool()
    args = ("sleep_h,steps", "2026-08-01", "2026-08-31", "2026-10-01", "2026-10-21")
    first = asyncio.run(t.health_compare(*args, __user__=USER))
    assert first == asyncio.run(t.health_compare(*args, __user__=USER))
    check = call(t.health_sql("SELECT round(avg(steps), 2) FROM api.daily WHERE day BETWEEN '2026-08-01' AND '2026-08-31'",
                              __user__=USER))
    assert json.loads(first)["metrics"]["steps"]["a"]["mean"] == check["rows"][0][0]


@needs_db
def test_trend_and_correlation():
    t = call(tool().health_trend("weight_kg", "last 12 weeks", "2026-10-21", "week", __user__=USER))
    assert t["slope_per_week"] == pytest.approx(-0.14, abs=0.005)  # 0.02 kg/day downward
    c = call(tool().health_correlate("sleep_h", "resting_hr", "last 60 days", "2026-10-21", __user__=USER))
    assert c["pairs"] == 60 and c["pearson_r"] < -0.5


@needs_db
def test_sql_is_read_only_and_bounded():
    t = tool()
    assert "error" in call(t.health_sql("WITH d AS (DELETE FROM core.sleep RETURNING 1) SELECT * FROM d", __user__=USER))
    assert "error" in call(t.health_sql("SELECT * FROM raw.payload", __user__=USER))
    t.valves.STATEMENT_TIMEOUT_MS = 500
    assert "error" in call(t.health_sql("SELECT pg_sleep(3)", __user__=USER))


@needs_db
def test_unmapped_user_gets_an_error():
    assert "No health database" in call(tool().health_profile(__user__={"id": "someone-else"}))["error"]
