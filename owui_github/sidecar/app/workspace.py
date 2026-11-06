"""Clones and fetches into the coding workspace (/fast/workspace) for the coding-agent runner.

Only `git clone` and `git fetch` run here, over HTTPS. The token reaches git through the environment, as an
Authorization header for github.com only; it is never put in a URL, an argument or .git/config, and nothing
in this module pushes. Clones get a plain https://github.com/<owner>/<repo>.git origin.
"""

import asyncio
import base64
import os
import re
import secrets
import shutil
from pathlib import Path

from . import policy

GITHUB = os.environ.get("GITHUB_GIT_URL", "https://github.com").rstrip("/")
TIMEOUT = 900
REMOTE_RE = re.compile(r"github\.com[:/](?P<owner>[A-Za-z0-9_.\-]+)/(?P<repo>[A-Za-z0-9_.\-]+?)(?:\.git)?/?$")


class WorkspaceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def parse_repo(text: str) -> tuple[str, str]:
    """(owner, repo) from 'owner/repo', an https://github.com/... URL or git@github.com:owner/repo.git."""
    text = (text or "").strip()
    m = REMOTE_RE.search(text)
    if m:
        return policy.check_name(m["owner"], "owner"), policy.check_name(m["repo"], "repo")
    if "://" in text or "@" in text:
        raise policy.PolicyError(f"only GitHub repositories can be cloned: {text!r}")
    parts = text.strip("/").split("/")
    if len(parts) != 2:
        raise policy.PolicyError(f"repo must be 'owner/name' or a GitHub URL, got {text!r}")
    return policy.check_name(parts[0], "owner"), policy.check_name(parts[1].removesuffix(".git"), "repo")


class Workspace:
    def __init__(self, root: Path, token: str):
        self.root = root
        self.token = token
        self.locks: dict[str, asyncio.Lock] = {}
        os.umask(0o007)     # the workspace is mode 770: owner and group only (git inherits it)

    def _env(self) -> dict:
        auth = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
        return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/tmp", "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"http.{GITHUB}/.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {auth}"}

    async def _git(self, *args: str, cwd: Path | None = None) -> str:
        proc = await asyncio.create_subprocess_exec("git", *args, cwd=cwd, env=self._env(),
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise WorkspaceError(504, f"git {args[0]} timed out after {TIMEOUT} s")
        if proc.returncode:
            raise WorkspaceError(502, f"git {args[0]} failed: {err.decode(errors='replace').strip()[-500:]}")
        return out.decode()

    def _lock(self, name: str) -> asyncio.Lock:
        return self.locks.setdefault(name, asyncio.Lock())

    async def clone(self, repo: str, name: str | None) -> dict:
        owner, repo_name = parse_repo(repo)
        name = policy.check_name(name or repo_name, "name")
        dest = self.root / name
        async with self._lock(name):
            if dest.exists():
                raise WorkspaceError(409, f"/fast/workspace/{name} already exists; refresh it instead")
            # clone beside it under a hidden name, so a failed clone never leaves a half repo that looks real
            tmp = self.root / f".cloning-{name}-{secrets.token_hex(3)}"
            try:
                await self._git("clone", "--quiet", "--", f"{GITHUB}/{owner}/{repo_name}.git", str(tmp))
                tmp.rename(dest)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            head = (await self._git("rev-parse", "--abbrev-ref", "HEAD", cwd=dest)).strip()
            return {"name": name, "source": f"{owner}/{repo_name}", "branch": head,
                    "commit": (await self._git("rev-parse", "HEAD", cwd=dest)).strip()}

    async def fetch(self, name: str) -> dict:
        name = policy.check_name(name, "name")
        dest = self.root / name
        if not (dest / ".git").exists():
            raise WorkspaceError(404, f"no repository /fast/workspace/{name}")
        async with self._lock(name):
            origin = (await self._git("remote", "get-url", "origin", cwd=dest)).strip()
            try:
                owner, repo_name = parse_repo(origin)
            except policy.PolicyError:
                raise WorkspaceError(400, f"origin of {name} is not a GitHub repository: {origin}")
            # the clone's own origin URL (often SSH) is left as it is; the fetch goes over HTTPS into origin/*
            await self._git("fetch", "--quiet", "--prune", "--", f"{GITHUB}/{owner}/{repo_name}.git",
                            "+refs/heads/*:refs/remotes/origin/*", cwd=dest)
            refs = await self._git("for-each-ref", "--format=%(refname:lstrip=3) %(objectname)", "refs/remotes/origin",
                                   cwd=dest)
            branches = dict(line.split(" ", 1) for line in refs.splitlines() if line and not line.startswith("HEAD "))
            return {"name": name, "source": f"{owner}/{repo_name}", "origin": branches}
