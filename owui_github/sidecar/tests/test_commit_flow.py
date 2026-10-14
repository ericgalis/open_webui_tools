"""Run GitHub.commit / open_pull_request against a fake GitHub API and inspect every write."""

import asyncio
import json
import types

import httpx
import pytest

from app import policy
from app.gh import GitHub


class FakeGitHub:
    def __init__(self, dev_exists=False):
        self.refs = {"main": "m1"} | ({"dev": "m1"} if dev_exists else {})
        self.writes = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        m, p = request.method, request.url.path
        body = json.loads(request.content) if request.content else None
        if m != "GET":
            self.writes.append((m, p, body))
        if (m, p) == ("GET", "/user"):
            return httpx.Response(200, json={"login": "me"})
        if (m, p) == ("GET", "/repos/me/proj"):
            return httpx.Response(200, json={"default_branch": "main", "archived": False})
        if m == "GET" and p.startswith("/repos/me/proj/git/ref/heads/"):
            name = p.rsplit("/", 1)[1]
            if name in self.refs:
                return httpx.Response(200, json={"object": {"sha": self.refs[name]}})
            return httpx.Response(404, json={"message": "Not Found"})
        if (m, p) == ("POST", "/repos/me/proj/git/refs"):
            self.refs[body["ref"].removeprefix("refs/heads/")] = body["sha"]
            return httpx.Response(201, json={})
        if m == "GET" and p.startswith("/repos/me/proj/git/commits/"):
            return httpx.Response(200, json={"tree": {"sha": "t1"}})
        if (m, p) == ("GET", "/repos/me/proj/git/trees/t1"):
            return httpx.Response(200, json={"truncated": False, "tree": [
                {"path": "run.sh", "mode": "100755", "type": "blob"},
                {"path": "README.md", "mode": "100644", "type": "blob"},
                {"path": "src", "mode": "040000", "type": "tree"},
            ]})
        if (m, p) == ("POST", "/repos/me/proj/git/trees"):
            return httpx.Response(201, json={"sha": "t2"})
        if (m, p) == ("POST", "/repos/me/proj/git/commits"):
            return httpx.Response(201, json={"sha": "c2" * 20})
        if (m, p) == ("PATCH", "/repos/me/proj/git/refs/heads/dev"):
            self.refs["dev"] = body["sha"]
            return httpx.Response(200, json={})
        if (m, p) == ("GET", "/repos/me/proj/pulls"):
            return httpx.Response(200, json=[])
        if m == "GET" and p.startswith("/repos/me/proj/compare/"):
            return httpx.Response(200, json={"status": "ahead", "ahead_by": 1, "behind_by": 0, "commits": [], "files": []})
        if (m, p) == ("POST", "/repos/me/proj/pulls"):
            return httpx.Response(201, json={"number": 7, "title": body["title"], "html_url": "https://github.com/me/proj/pull/7"})
        return httpx.Response(500, json={"message": f"unexpected {m} {p}"})


def make(fake):
    cfg = types.SimpleNamespace(token="t", write_branch="dev", blocked_prefixes=[".github/workflows/"])
    return GitHub(httpx.AsyncClient(transport=httpx.MockTransport(fake)), cfg)


def run(coro):
    return asyncio.run(coro)


def test_commit_creates_dev_and_writes_only_dev():
    fake = FakeGitHub(dev_exists=False)
    gh = make(fake)
    res = run(gh.commit("me", "proj", "Update docs", [
        {"path": "README.md", "content": "hi\n"},
        {"path": "run.sh", "content": "#!/bin/sh\necho hi\n"},
    ], "Alice"))
    assert res["committed"] and res["created_branch"] and res["branch"] == "dev"
    methods = [(m, p) for m, p, _ in fake.writes]
    assert methods == [
        ("POST", "/repos/me/proj/git/refs"),
        ("POST", "/repos/me/proj/git/trees"),
        ("POST", "/repos/me/proj/git/commits"),
        ("PATCH", "/repos/me/proj/git/refs/heads/dev"),
    ]
    assert fake.writes[0][2]["ref"] == "refs/heads/dev"
    tree = {e["path"]: e for e in fake.writes[1][2]["tree"]}
    assert tree["run.sh"]["mode"] == "100755" and tree["README.md"]["mode"] == "100644"
    assert fake.writes[2][2]["message"].endswith("Requested-via: Open WebUI (Alice)")
    assert fake.writes[3][2]["force"] is False
    assert fake.refs["main"] == "m1"


def test_delete_missing_file_rejected_before_any_write():
    fake = FakeGitHub(dev_exists=True)
    with pytest.raises(policy.PolicyError):
        run(make(fake).commit("me", "proj", "rm", [{"path": "nope.txt", "delete": True}], None))
    assert fake.writes == []


def test_workflow_path_rejected_before_any_request():
    fake = FakeGitHub(dev_exists=True)
    with pytest.raises(policy.PolicyError):
        run(make(fake).commit("me", "proj", "ci", [{"path": ".github/workflows/x.yml", "content": "on: push"}], None))
    assert fake.writes == []


def test_pull_request_dev_into_default():
    fake = FakeGitHub(dev_exists=True)
    res = run(make(fake).open_pull_request("me", "proj", "Docs", "body", False, "Alice"))
    assert res["created"] and res["number"] == 7
    (m, p, body), = fake.writes
    assert (m, p) == ("POST", "/repos/me/proj/pulls") and body["head"] == "dev" and body["base"] == "main"


def test_guard_runs_inside_client():
    fake = FakeGitHub()
    with pytest.raises(policy.PolicyError):
        run(make(fake).req("PATCH", "/repos/me/proj/git/refs/heads/main", json={"sha": "x", "force": False}))
    assert fake.writes == []
