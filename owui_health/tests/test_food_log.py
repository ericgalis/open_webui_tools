import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import food_log as fl

USER = {"id": "user-1"}
CANONICAL = Path(os.environ.get("HEALTH_CONSTITUTION", "/bulk/virtualdisks/docker/compose/health/food_constitution.json"))
FOODMATCH = Path(os.environ.get("HEALTH_FOODMATCH", "/bulk/virtualdisks/docker/compose/health/healthingest/foodmatch.py"))


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


@pytest.mark.skipif(not FOODMATCH.exists(), reason="compose repo not mounted")
def test_embedded_foodmatch_matches_the_ingest_copy():
    src = Path(fl.__file__).read_text()
    begin = src.index("\n", src.index("# ---- BEGIN foodmatch.py")) + 1
    assert src[begin:src.index("# ---- END foodmatch.py")] == FOODMATCH.read_text()


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


# ---- USDA check (rule R11), with a fake FoodData Central

FDC = {
    "egg, scrambled": [{"fdcId": 1, "dataType": "Survey (FNDDS)", "description": "Egg, scrambled"}],
    "carrot tempura": [{"fdcId": 9, "dataType": "Survey (FNDDS)", "description": "Carrots, cooked"}],
    "carrot tempura, raw": [],
    "miso soup": [{"fdcId": 2, "dataType": "Survey (FNDDS)", "description": "Soup, miso or tofu"}],
}
DETAILS = {
    1: {"description": "Egg, scrambled", "foodPortions": [{"portionDescription": "1 egg", "gramWeight": 61}]},
    2: {"description": "Soup, miso or tofu", "foodPortions": [{"portionDescription": "1 cup", "gramWeight": 245}]},
}


@pytest.fixture
def usda(tool, monkeypatch):
    calls = []

    def fake(api_key, method, path, body=None):
        calls.append((method, path, body and body["query"]))
        if path == "/foods/search":
            return {"foods": FDC.get(body["query"], [])}
        return DETAILS[int(path.rsplit("/", 1)[1])]

    monkeypatch.setattr(fl, "_usda", fake)
    tool.valves.USDA_FDC_API_KEY = "test-key"
    return calls


def test_unmatched_items_block_the_save_until_the_user_decides(tool, tmp_path, usda):
    items = [{"name": "egg, scrambled", "quantity": 2, "unit": "piece"},
             {"name": "carrot tempura", "quantity": 1, "unit": "piece"},
             {"name": "miso soup", "quantity": 1, "unit": "serving"}]
    r = call(tool.food_log_add("dinner", recent_local(), items, __user__=USER))
    assert r["saved"] is False and not list((tmp_path / "enrique" / "pending").iterdir())
    by_name = {c["name"]: c for c in r["unmatched"]}
    assert by_name["carrot tempura"] == {"name": "carrot tempura", "status": "no_match",
                                         "usda_suggestions": ["carrots, cooked"]}
    assert by_name["miso soup"]["status"] == "no_portion" and by_name["miso soup"]["usda_portions"] == ["1 cup"]
    assert r["matched"] == [{"name": "egg, scrambled", "status": "matched", "usda_food": "Egg, scrambled",
                             "grams": 122.0, "grams_from": "usda_portion"}]

    saved = call(tool.food_log_add("dinner", recent_local(), items, accept_unmatched=True, __user__=USER))
    assert saved["saved"] is True and [c["status"] for c in saved["usda"]] == ["matched", "no_match", "no_portion"]


def test_matched_entry_reports_usda_foods_and_category_names_are_refused(tool, usda):
    r = call(tool.food_log_add("breakfast", recent_local(), EGGS, __user__=USER))
    assert r["saved"] is True and r["usda"][0]["usda_food"] == "Egg, scrambled"
    greens = call(tool.food_log_add("lunch", recent_local(), [{"name": "Greens", "quantity": 3, "unit": "cup"}],
                                    __user__=USER))
    assert greens["violations"] == [{"rule": "R3", "path": "items/0/name",
                                     "message": "'greens' is a category, not a specific food: name the food (rule R3)"}]
    assert not any(q == "greens" for _, _, q in usda)  # refused before any USDA call


def test_usda_outage_does_not_block_logging(tool, monkeypatch):
    def down(api_key, method, path, body=None):
        raise LookupError("USDA foods: HTTP 503")
    monkeypatch.setattr(fl, "_usda", down)
    tool.valves.USDA_FDC_API_KEY = "test-key"
    r = call(tool.food_log_add("snack", "now", EGGS, __user__=USER))
    assert r["saved"] is True and r["usda"][0] == {"name": "egg, scrambled", "status": "unchecked",
                                                   "error": "USDA foods: HTTP 503"}


def test_without_a_key_the_check_is_skipped(tool):
    r = call(tool.food_log_add("snack", "now", EGGS, __user__=USER))
    assert r["saved"] is True and r["usda"].startswith("not checked")


def test_usda_errors_hide_the_key(monkeypatch):
    monkeypatch.setattr(fl.time, "sleep", lambda s: None)
    monkeypatch.setattr(fl.requests, "request", lambda *a, **k: type("R", (), {"status_code": 403})())
    with pytest.raises(LookupError) as e:
        fl._usda("SECRET-KEY", "GET", "/food/1")
    assert str(e.value) == "USDA food: HTTP 403"
