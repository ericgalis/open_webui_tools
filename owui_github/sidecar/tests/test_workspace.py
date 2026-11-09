"""Workspace clone and fetch against local bare repositories standing in for github.com."""

import asyncio
import subprocess

import pytest

from app import policy, workspace

TOKEN = "ghp_test-token-value"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def github(tmp_path, monkeypatch):
    """A fake github.com with me/proj (branches main and dev); returns (workspace, upstream work tree)."""
    root = tmp_path / "github"
    work = tmp_path / "upstream"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "one")
    git(work, "branch", "dev")
    (root / "me").mkdir(parents=True)
    git(tmp_path, "clone", "-q", "--bare", str(work), str(root / "me" / "proj.git"))
    git(work, "remote", "add", "origin", str(root / "me" / "proj.git"))
    monkeypatch.setattr(workspace, "GITHUB", f"file://{root}")
    ws = tmp_path / "workspace"
    ws.mkdir()
    return workspace.Workspace(ws, TOKEN), work


def run(coro):
    return asyncio.run(coro)


def test_parse_repo():
    assert workspace.parse_repo("me/proj") == ("me", "proj")
    assert workspace.parse_repo("https://github.com/me/proj.git") == ("me", "proj")
    assert workspace.parse_repo("git@github.com:me/proj.git") == ("me", "proj")
    assert workspace.parse_repo("https://github.com/me/proj/") == ("me", "proj")
    for bad in ("https://gitlab.com/me/proj", "proj", "me/../x", "file:///etc/passwd", "me/proj/extra"):
        with pytest.raises(policy.PolicyError):
            workspace.parse_repo(bad)


def test_clone_then_conflict(github):
    ws, _ = github
    result = run(ws.clone("me/proj", None))
    assert (result["name"], result["source"], result["branch"]) == ("proj", "me/proj", "main")
    assert (ws.root / "proj" / ".git").is_dir()
    assert TOKEN not in (ws.root / "proj" / ".git" / "config").read_text()
    assert [p.name for p in ws.root.iterdir()] == ["proj"]          # no leftover temp dir
    assert (ws.root / "proj").stat().st_mode & 0o777 == 0o770
    with pytest.raises(workspace.WorkspaceError) as e:
        run(ws.clone("me/proj", None))
    assert e.value.status == 409
    assert run(ws.clone("me/proj", "proj-copy"))["name"] == "proj-copy"


def test_failed_clone_leaves_nothing(github):
    ws, _ = github
    with pytest.raises(workspace.WorkspaceError) as e:
        run(ws.clone("me/missing", None))
    assert e.value.status == 502
    assert list(ws.root.iterdir()) == []


def test_fetch_updates_origin_refs_and_keeps_the_ssh_remote(github):
    ws, upstream = github
    run(ws.clone("me/proj", None))
    clone = ws.root / "proj"
    git(clone, "remote", "set-url", "origin", "git@github.com:me/proj.git")
    git(upstream, "checkout", "-q", "dev")
    git(upstream, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "two")
    git(upstream, "push", "-q", "origin", "dev")
    result = run(ws.fetch("proj"))
    assert result["origin"]["dev"] == git(upstream, "rev-parse", "dev")
    assert git(clone, "rev-parse", "origin/dev") == git(upstream, "rev-parse", "dev")
    assert git(clone, "remote", "get-url", "origin") == "git@github.com:me/proj.git"


def test_fetch_refuses_unknown_or_non_github_repos(github):
    ws, _ = github
    with pytest.raises(workspace.WorkspaceError) as e:
        run(ws.fetch("nope"))
    assert e.value.status == 404
    run(ws.clone("me/proj", None))
    git(ws.root / "proj", "remote", "set-url", "origin", "https://gitlab.com/me/proj.git")
    with pytest.raises(workspace.WorkspaceError) as e:
        run(ws.fetch("proj"))
    assert e.value.status == 400


def test_token_goes_only_to_github_through_the_environment(github):
    ws, _ = github
    env = ws._env()
    assert env["GIT_CONFIG_KEY_0"] == f"http.{workspace.GITHUB}/.extraHeader"
    assert TOKEN not in env["GIT_CONFIG_VALUE_0"]                    # base64 in a Basic header
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def commit_file(cwd, path, text, message):
    (cwd / path).parent.mkdir(parents=True, exist_ok=True)
    (cwd / path).write_text(text)
    git(cwd, "add", path)
    git(cwd, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)


def cloned(ws):
    run(ws.clone("me/proj", None))
    clone = ws.root / "proj"
    git(clone, "remote", "set-url", "origin", "git@github.com:me/proj.git")     # as real clones look
    return clone


def bare(ws):
    return workspace.GITHUB.removeprefix("file://") + "/me/proj.git"


def test_publish_fast_forwards_then_merges_onto_dev(github):
    ws, upstream = github
    clone = cloned(ws)
    git(clone, "checkout", "-q", "-b", "constitution/bootstrap")
    commit_file(clone, ".agent/constitution.json", "{}", "draft")
    result = run(ws.publish("proj", "constitution/bootstrap"))
    assert (result["how"], result["files"]) == ("fast-forward", [".agent/constitution.json"])
    assert git(bare(ws), "rev-parse", "dev") == git(clone, "rev-parse", "constitution/bootstrap")
    assert run(ws.publish("proj", "constitution/bootstrap"))["how"].startswith("already on dev")
    # dev moves on GitHub; the next branch is merged onto it, never forced
    git(upstream, "fetch", "-q", "origin")
    git(upstream, "checkout", "-q", "-B", "dev", "origin/dev")
    commit_file(upstream, "README", "hi", "upstream work")
    git(upstream, "push", "-q", "origin", "dev")
    git(clone, "checkout", "-q", "-b", "qwen/job1", "main")
    commit_file(clone, "app.py", "x = 1\n", "job")
    result = run(ws.publish("proj", "qwen/job1"))
    assert result["how"] == "merged into dev" and result["files"] == ["app.py"]
    tip = git(bare(ws), "rev-parse", "dev")
    assert git(bare(ws), "rev-parse", f"{tip}^1") == git(upstream, "rev-parse", "dev")
    assert git(bare(ws), "rev-parse", f"{tip}^2") == git(clone, "rev-parse", "qwen/job1")
    assert git(clone, "rev-parse", "origin/dev") == tip
    assert git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "qwen/job1"     # the work tree is untouched
    assert git(clone, "status", "--porcelain") == ""


def test_publish_refuses_conflicts_blocked_paths_and_unknown_branches(github):
    ws, upstream = github
    ws.blocked_prefixes = [".github/workflows/"]
    clone = cloned(ws)
    commit_file(upstream, "a.txt", "upstream", "a")
    git(upstream, "push", "-q", "origin", "main:dev")
    git(clone, "checkout", "-q", "-b", "qwen/clash")
    commit_file(clone, "a.txt", "local", "a")
    before = git(bare(ws), "rev-parse", "dev")
    with pytest.raises(workspace.WorkspaceError) as e:
        run(ws.publish("proj", "qwen/clash"))
    assert e.value.status == 409 and "a.txt" in e.value.message
    git(clone, "checkout", "-q", "-b", "qwen/ci", "main")
    commit_file(clone, ".github/workflows/x.yml", "on: push", "ci")
    with pytest.raises(policy.PolicyError):
        run(ws.publish("proj", "qwen/ci"))
    with pytest.raises(workspace.WorkspaceError) as e:
        run(ws.publish("proj", "qwen/nope"))
    assert e.value.status == 404
    for bad in ("-x", "a..b", "a/", "x.lock", "a//b"):
        with pytest.raises(policy.PolicyError):
            run(ws.publish("proj", bad))
    assert git(bare(ws), "rev-parse", "dev") == before
