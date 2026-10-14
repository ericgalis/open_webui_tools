import pytest

from app import policy

WB = "dev"


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "/repos/o/r/git/trees", {}),
        ("POST", "/repos/o/r/git/commits", {}),
        ("POST", "/repos/o/r/git/refs", {"ref": "refs/heads/dev", "sha": "x"}),
        ("PATCH", "/repos/o/r/git/refs/heads/dev", {"sha": "x", "force": False}),
        ("POST", "/repos/o/r/pulls", {"head": "dev", "base": "main"}),
        ("POST", "/repos/o/r/pulls", {"head": "o:dev", "base": "main"}),
    ],
)
def test_allowed_writes(method, path, body):
    policy.guard_write(method, path, body, WB)


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("PATCH", "/repos/o/r/git/refs/heads/main", {"sha": "x", "force": False}),
        ("PATCH", "/repos/o/r/git/refs/heads/dev", {"sha": "x", "force": True}),
        ("PATCH", "/repos/o/r/git/refs/heads/dev", {"sha": "x"}),
        ("POST", "/repos/o/r/git/refs", {"ref": "refs/heads/main", "sha": "x"}),
        ("POST", "/repos/o/r/git/refs", {"ref": "refs/tags/v1", "sha": "x"}),
        ("POST", "/repos/o/r/pulls", {"head": "feature", "base": "main"}),
        ("POST", "/repos/o/r/pulls", {"head": "evil:dev", "base": "main"}),
        ("PUT", "/repos/o/r/contents/README.md", {"branch": "dev"}),
        ("PUT", "/repos/o/r/pulls/1/merge", {}),
        ("DELETE", "/repos/o/r/git/refs/heads/dev", {}),
        ("DELETE", "/repos/o/r", {}),
        ("PATCH", "/repos/o/r", {"default_branch": "dev"}),
        ("POST", "/user/repos", {"name": "x"}),
        ("POST", "/repos/o/r/merges", {"base": "main", "head": "dev"}),
    ],
)
def test_blocked_writes(method, path, body):
    with pytest.raises(policy.PolicyError):
        policy.guard_write(method, path, body, WB)


@pytest.mark.parametrize("path", ["../x", "a/../b", "/abs", "a//b", ".git/config", "a/.GIT/hooks", "dir/", "a\\b", ""])
def test_bad_paths(path):
    with pytest.raises(policy.PolicyError):
        policy.check_path(path, [])


def test_blocked_prefix_case_insensitive():
    with pytest.raises(policy.PolicyError):
        policy.check_path(".GitHub/Workflows/ci.yml", [".github/workflows/"])
    assert policy.check_path(".github/dependabot.yml", [".github/workflows/"]) == ".github/dependabot.yml"


def test_split_repo():
    assert policy.split_repo("name", "me") == ("me", "name")
    assert policy.split_repo("org/name", "me") == ("org", "name")
    for bad in ("a/b/c", "../x", "o/.."):
        with pytest.raises(policy.PolicyError):
            policy.split_repo(bad, "me")
