"""Seed a scratch person database with deterministic data for the tool tests. Runs in the health-ingest
image (it uses the ingest's own schema and loaders): python seed.py <key>"""

import os
import subprocess
import sys
from datetime import date, datetime, time, timedelta, timezone

from healthingest import __main__ as cli
from healthingest import db, garmin, wyze
from healthingest.config import DB_HOST, DB_PORT, Person

END = date(2026, 10, 21)          # a Wednesday
DAYS = 120


def main(key: str) -> None:
    person = Person(key=key, display_name=key, timezone="UTC", sources={"garmin": {}, "wyze": {}, "food": {}})
    subprocess.run(["psql", "-h", DB_HOST, "-p", str(DB_PORT), "-U", "postgres", "-d", "postgres", "-q"],
                   input=cli.add_person_sql(person), text=True, check=True,
                   env=os.environ | {"PGPASSWORD": os.environ["HEALTH_TEST_ADMIN_PASSWORD"]})
    cli.migrate(person)
    with db.connect(person) as conn:
        conn.execute("UPDATE core.profile SET data = %s::jsonb", (
            '{"goals": ["sleep more"], "targets": {"sleep_h": {"min": 7.5}, "steps": {"min": 8000},'
            ' "strength_sessions": {"min": 3, "per": "week"}}}',))
        for i in range(DAYS):
            day = END - timedelta(days=i)
            recent = i < 7                            # the last week: less sleep, higher resting HR
            sleep_h = (6.0 if recent else 7.5) + (i % 3) * 0.25
            start = datetime.combine(day - timedelta(days=1), time(23, 0), tzinfo=timezone.utc)
            garmin.apply_day(conn, day, {
                "user_summary": {"totalSteps": 8000 + (i % 7) * 500, "totalKilocalories": 2400,
                                 "restingHeartRate": (58 if recent else 52) + i % 2},
                "sleep": {"dailySleepDTO": {
                    "sleepTimeSeconds": int(sleep_h * 3600),
                    "sleepStartTimestampGMT": int(start.timestamp() * 1000),
                    "sleepEndTimestampGMT": int((start + timedelta(hours=sleep_h)).timestamp() * 1000)}},
            })
            if i % 2 == 0:  # weigh-ins every other day, slowly falling
                ts = datetime.combine(day, time(7, 0), tzinfo=timezone.utc)
                wyze.apply_record(conn, {"data_id": f"w{i}", "measure_ts": int(ts.timestamp() * 1000),
                                         "weight": round(80 + i * 0.02, 2), "body_fat": 20.0}, person.timezone)
            if day.weekday() in (0, 2, 4):  # strength Mon/Wed/Fri
                garmin.apply_activity(conn, {
                    "activityId": 1000 + i, "startTimeLocal": f"{day} 18:00:00", "startTimeGMT": f"{day} 18:00:00",
                    "activityType": {"typeKey": "strength_training"}, "activityName": "Strength", "duration": 3000,
                }, None)
        conn.execute("INSERT INTO core.source_status (source, last_success_at, last_data_day)"
                     " VALUES ('garmin', now(), %s)", (END,))
        conn.commit()
    print(f"seeded {person.db_name}")


if __name__ == "__main__":
    main(sys.argv[1])
