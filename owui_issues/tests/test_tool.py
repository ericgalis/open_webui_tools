"""The Issues tool against a fake github-tool: what it refuses before any call, and how it moves statuses."""
import asyncio
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))
import build  # noqa: E402
import tool  # noqa: E402

REPO = "ericgalis/docker_compose_home_servers"
USER = {"id": "u1", "name": "Alice"}

GOOD_FEATURE = """## Goal
A health page.

## Requirements
- GET /health returns 200.

## Security requirements
- Exposure: tools network only.

## Acceptance checks
- [ ] curl /health returns 200
- [ ] curl /health without the key returns 401

## Stacks affected
- `weather`: new route

## Not in scope
- Metrics.

## Tasks
- [ ] Add the route in app/main.py
"""
GOOD_EPIC = "## Intent\nI want a health page.\n\n## Intake\n- who: me\n- data: none\n"


class Fake:
    """Stands in for _call: records requests, answers from a table of issues."""
    def __init__(self, issues=None):
        self.issues = issues or {}
        self.sent = []

    async def __call__(self, valves, user, method, path, params=None, body=None):
        self.sent.append((method, path, params, body))
        if (method, path) == ("GET", "/v1/issue"):
            return self.issues[params["number"]]
        if path == "/v1/issue/update":
            return {"number": body["number"], "labels": body.get("labels", [])}
        return {"number": 7}


@pytest.fixture
def t(monkeypatch):
    fake = Fake()
    monkeypatch.setattr(tool, "_call", fake)
    tl = tool.Tools()
    tl.fake = fake
    return tl


def run(coro):
    return json.loads(asyncio.run(coro))


def feature(n, status, body=GOOD_FEATURE, extra=()):
    return {"number": n, "labels": ["type:feature", f"status:{status}", *extra], "body": body, "title": "t"}


def test_issues_json_is_up_to_date():
    assert json.loads((HERE.parent / "issues.json").read_text()) == build.build()


def test_the_template_in_the_tool_passes_its_own_check_once_filled_in():
    assert tool.check_feature_body(GOOD_FEATURE) == []
    assert tool.FEATURE_TEMPLATE == ""      # the source holds a placeholder; build.py fills it into issues.json
    assert "Security requirements" in build.build()[0]["content"]


def test_epic_needs_intent_and_intake(t):
    assert run(t.epic_create(REPO, "x", "## Intent\nonly this\n", __user__=USER))["problems"]
    assert t.fake.sent == []
    run(t.epic_create(REPO, "x", GOOD_EPIC, __user__=USER))
    assert t.fake.sent[0][3]["labels"] == ["type:epic"]


@pytest.mark.parametrize("broken", [
    GOOD_FEATURE.replace("## Security requirements\n- Exposure: tools network only.\n", ""),
    GOOD_FEATURE.replace("- [ ] curl /health returns 200\n- [ ] curl /health without the key returns 401", "just words"),
    GOOD_FEATURE.replace("- [ ] Add the route in app/main.py", "add the route"),
    GOOD_FEATURE.replace("- `weather`: new route", ""),
])
def test_feature_must_follow_the_template(t, broken):
    assert run(t.feature_create(REPO, 1, "x", broken, __user__=USER))["problems"]
    assert t.fake.sent == []


def test_feature_is_created_as_a_draft_under_its_epic(t):
    run(t.feature_create(REPO, 1, "x", GOOD_FEATURE, __user__=USER))
    body = t.fake.sent[0][3]
    assert body["labels"] == ["type:feature", "status:draft"] and body["parent"] == 1


def test_only_listed_repos(t):
    assert "not one of the repos" in run(t.epic_create("someone/else", "x", GOOD_EPIC, __user__=USER))["error"]
    assert t.fake.sent == []


def test_ready_needs_the_users_approval_and_a_review_first(t):
    t.fake.issues = {5: feature(5, "draft"), 6: feature(6, "review"), 7: feature(7, "in-progress")}
    assert "explicit approval" in run(t.feature_set_status(REPO, 6, "ready", __user__=USER))["error"]
    assert t.fake.sent == []
    out = run(t.feature_set_status(REPO, 6, "ready", True, __user__=USER))
    assert out["from"] == "review" and out["to"] == "ready"
    assert t.fake.sent[-1][3]["labels"] == ["type:feature", "status:ready"]
    # the runner's states are not the PO's to set or leave
    assert "cannot move" in run(t.feature_set_status(REPO, 7, "ready", True, __user__=USER))["error"]
    assert "status must be" in run(t.feature_set_status(REPO, 6, "in-pr", True, __user__=USER))["error"]


def test_status_change_keeps_other_labels_and_swaps_only_the_status(t):
    t.fake.issues = {5: feature(5, "draft", extra=("security",))}
    run(t.feature_set_status(REPO, 5, "review", __user__=USER))
    assert t.fake.sent[-1][3]["labels"] == ["type:feature", "security", "status:review"]


def test_ready_is_refused_if_the_body_lost_its_structure(t):
    t.fake.issues = {5: feature(5, "review", body="## Goal\nnothing else")}
    assert run(t.feature_set_status(REPO, 5, "ready", True, __user__=USER))["problems"]


def test_body_changes_only_while_not_ready_or_building(t):
    t.fake.issues = {5: feature(5, "ready"), 6: feature(6, "review"), 7: {"number": 7, "labels": ["type:epic"], "body": "", "title": ""}}
    assert "Set it back to draft" in run(t.feature_update(REPO, 5, GOOD_FEATURE, __user__=USER))["error"]
    assert run(t.feature_update(REPO, 6, GOOD_FEATURE, __user__=USER))["number"] == 6
    assert "not a Feature" in run(t.feature_update(REPO, 7, GOOD_FEATURE, __user__=USER))["error"]


def test_work_status_groups_features_by_status(t, monkeypatch):
    async def fake(valves, user, method, path, params=None, body=None):
        if params["labels"] == "type:feature":
            return {"results": [{"number": 2, "title": "a", "labels": ["type:feature", "status:ready"], "parent": 1},
                                {"number": 3, "title": "b", "labels": ["type:feature", "status:draft"], "parent": 1}]}
        return {"results": [{"number": 1, "title": "epic"}]}
    monkeypatch.setattr(tool, "_call", fake)
    out = run(t.work_status(REPO, __user__=USER))
    assert out["by_status"] == {"draft": [{"number": 3, "title": "b", "epic": 1}], "ready": [{"number": 2, "title": "a", "epic": 1}]}
    assert out["epics"] == [{"number": 1, "title": "epic"}]
