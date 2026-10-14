"""Write policy. Every non-GET request to GitHub must pass `guard_write`, which only permits
the handful of calls needed to commit to the write branch and open a PR from it."""

import re

NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,100}$")

_REPO = r"/repos/(?P<owner>[A-Za-z0-9_.\-]+)/(?P<repo>[A-Za-z0-9_.\-]+)"


class PolicyError(Exception):
    pass


def check_name(value: str, what: str) -> str:
    if not NAME_RE.fullmatch(value or "") or value in (".", ".."):
        raise PolicyError(f"invalid {what}: {value!r}")
    return value


def split_repo(repo: str, default_owner: str) -> tuple[str, str]:
    parts = (repo or "").strip().strip("/").split("/")
    if len(parts) == 1:
        parts = [default_owner, parts[0]]
    if len(parts) != 2:
        raise PolicyError(f"repo must be 'owner/name' or 'name', got {repo!r}")
    return check_name(parts[0], "owner"), check_name(parts[1], "repo")


def check_path(path: str, blocked_prefixes: list[str]) -> str:
    if not isinstance(path, str) or not path or len(path) > 4096 or "\x00" in path or "\\" in path:
        raise PolicyError(f"invalid path: {path!r}")
    if path.startswith("/") or path.endswith("/"):
        raise PolicyError(f"path must be relative to the repo root without a trailing slash: {path!r}")
    segments = path.split("/")
    if any(s in ("", ".", "..") for s in segments) or any(s.lower() == ".git" for s in segments):
        raise PolicyError(f"invalid path: {path!r}")
    lowered = path.lower()
    for prefix in blocked_prefixes:
        if lowered.startswith(prefix.lower()):
            raise PolicyError(f"writing under {prefix} is not allowed by this tool")
    return path


def guard_write(method: str, path: str, body: dict | None, write_branch: str) -> None:
    """Raise PolicyError unless (method, path, body) is one of the allowed write calls."""
    m = re.fullmatch(_REPO + r"(?P<rest>/.*)", path)
    if not m:
        raise PolicyError(f"blocked write: {method} {path}")
    rest = m.group("rest")
    body = body or {}
    allowed = (
        (method == "POST" and rest == "/git/trees")
        or (method == "POST" and rest == "/git/commits")
        or (method == "POST" and rest == "/git/refs" and body.get("ref") == f"refs/heads/{write_branch}")
        or (method == "PATCH" and rest == f"/git/refs/heads/{write_branch}" and body.get("force") is False)
        or (
            method == "POST"
            and rest == "/pulls"
            and body.get("head") in (write_branch, f"{m.group('owner')}:{write_branch}")
        )
    )
    if not allowed:
        raise PolicyError(f"blocked write: {method} {path}")


def page_text(text: str, offset: int, max_chars: int) -> dict:
    chunk = text[offset : offset + max_chars]
    end = offset + len(chunk)
    return {
        "content": chunk,
        "offset": offset,
        "total_chars": len(text),
        "next_offset": end if end < len(text) else None,
    }
