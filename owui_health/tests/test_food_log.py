import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import food_log as fl

USER = {"id": "user-1"}
CANONICAL = Path(os.environ.get("HEALTH_CONSTITUTION", "/bulk/virtualdisks/docker/compose/health/food_constitution.json"))


@pytest.fixture
def tool(tmp_path):
    for sub in ("pending", "processed", "quarantine", "withdrawn"):
        (tmp_path / "enrique" / sub).mkdir(parents=True)
    t = fl.Tools()
    t.valves.INBOX_DIR = str(tmp_path)
    t.valves.PERSON_KEYS = json.dumps({"user-1": {"key": "enrique", "timezone": "America/New_York"}})
    return t


def call(coro):
    return json.loads(asyncio.run(coro))


def recent_local(hours_ago=1):
    t = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return t.astimezone(fl.ZoneInfo("America/New_York")).strftime("%Y-%m-%dT%H:%M")


EGGS = [{"name": "  Egg,   Scrambled ", "quantity": 2, "unit": "Piece"}]


@pytest.mark.skipif(not CANONICAL.exists(), reason="compose repo not mounted")
def test_embedded_constitution_matches_the_ingest_copy():
    assert fl.CONSTITUTION == json.loads(CANONICAL.read_text())


def test_valid_entry_is_normalised_and_written(tool, tmp_path):
    r = call(tool.food_log_add("Breakfast", recent_local(), EGGS, __user__=USER))
    assert r["saved"] is True
    saved = json.loads((tmp_path / "enrique" / "pending" / f"{r['entry_id']}.json").read_text())
    assert saved["items"] == [{"name": "egg, scrambled", "quantity": 2, "unit": "piece", "portion_estimated": False}]
    assert saved["meal"] == "breakfast" and saved["owui_user_id"] == "user-1" and saved["person"] == "enrique"
    assert saved["eaten_at"].endswith(("-04:00", "-05:00"))
    assert not list((tmp_path / "enrique" / "pending").glob(".tmp-*"))


def test_violations_name_the_rule_and_save_nothing(tool, tmp_path):
    r = call(tool.food_log_add("brunch", recent_local(), [{"name": "rice", "quantity": 1, "unit": "bowl"}], __user__=USER))
    assert r["saved"] is False
    assert {v["rule"] for v in r["violations"]} == {"R1", "R4"}
    future = call(tool.food_log_add("lunch", recent_local(hours_ago=-3), EGGS, __user__=USER))
    assert future["violations"][0]["rule"] == "R2"
    made_up = call(tool.food_log_add("lunch", recent_local(), [{"name": "rice", "quantity": 1, "unit": "cup", "kcal": 200}],
                                     __user__=USER))
    assert made_up["saved"] is False
    assert not list((tmp_path / "enrique" / "pending").iterdir())


def test_corrections_and_withdrawal(tool, tmp_path):
    first = call(tool.food_log_add("lunch", recent_local(), EGGS, __user__=USER))
    unknown = call(tool.food_log_add("lunch", recent_local(), EGGS, supersedes="00000000-0000-4000-8000-000000000000",
                                     __user__=USER))
    assert unknown["violations"][0]["rule"] == "R8"
    void = call(tool.food_log_add("lunch", recent_local(), [], supersedes=first["entry_id"], void=True, __user__=USER))
    assert void["saved"] is True
    assert len(call(tool.food_log_recent(__user__=USER))["entries"]) == 2

    # Loaded entries can't be withdrawn; pending ones can
    (tmp_path / "enrique" / "pending" / f"{first['entry_id']}.json").rename(
        tmp_path / "enrique" / "processed" / f"{first['entry_id']}.json")
    assert "Already loaded" in call(tool.food_log_withdraw(first["entry_id"], __user__=USER))["error"]
    assert call(tool.food_log_withdraw(void["entry_id"], __user__=USER))["withdrawn"] is True
    assert "UUID" in call(tool.food_log_withdraw("../../etc/passwd", __user__=USER))["error"]


def test_items_as_json_string_and_unmapped_user(tool):
    assert call(tool.food_log_add("snack", "now", json.dumps(EGGS), __user__=USER))["saved"] is True
    assert "not set up" in call(tool.food_log_add("snack", "now", EGGS, __user__={"id": "other"}))["error"]
