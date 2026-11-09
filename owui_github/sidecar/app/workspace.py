"""Clones and fetches into the coding workspace (/fast/workspace) for the coding-agent runner.

Clone, fetch and publish run here, over HTTPS. The token reaches git through the environment, as an Authorization
header for github.com only; it is never put in a URL, an argument or .git/config. Clones get a plain
https://github.com/<owner>/<repo>.git origin. The only push is publish, which updates the write branch (dev) and
never forces: a local branch is merged onto the write branch, without touching the clone's work tree.
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
BRANCH_RE = re.compile(r"^(?!-)(?!.*\.\.)(?!.*//)[A-Za-z0-9_.\-/]{1,200}(?<![/.])$")
IDENTITY = {"GIT_AUTHOR_NAME": "github-tool", "GIT_AUTHOR_EMAIL": "github-tool@localhost",
            "GIT_COMMITTER_NAME": "github-tool", "GIT_COMMITTER_EMAIL": "github-tool@localhost"}
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
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


def check_branch(branch: str) -> str:
    if not BRANCH_RE.fullmatch(branch or "") or branch.endswith(".lock") or "@{" in branch:
        raise policy.PolicyError(f"invalid branch name: {branch!r}")
    return branch


class Workspace:
    def __init__(self, root: Path, token: str, write_branch: str = "dev", blocked_prefixes: tuple = ()):
        self.root = root
        self.token = token
        self.write_branch = write_branch
        self.blocked_prefixes = list(blocked_prefixes)
        self.locks: dict[str, asyncio.Lock] = {}
        os.umask(0o007)     # the workspace is mode 770: owner and group only (git inherits it)

    def _env(self) -> dict:
        auth = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
        return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/tmp", "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"http.{GITHUB}/.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {auth}"}

    async def _git(self, *args: str, cwd: Path | None = None, env: dict | None = None, check: bool = True) -> str:
        proc = await asyncio.create_subprocess_exec("git", *args, cwd=cwd, env={**self._env(), **(env or {})},
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise WorkspaceError(504, f"git {args[0]} timed out after {TIMEOUT} s")
        if proc.returncode and not check:
            return out.decode()
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

    async def _repo(self, name: str) -> tuple[Path, str, str]:
        name = policy.check_name(name, "name")
        dest = self.root / name
        if not (dest / ".git").exists():
            raise WorkspaceError(404, f"no repository /fast/workspace/{name}")
        origin = (await self._git("remote", "get-url", "origin", cwd=dest)).strip()
        try:
            owner, repo_name = parse_repo(origin)
        except policy.PolicyError:
            raise WorkspaceError(400, f"origin of {name} is not a GitHub repository: {origin}")
        return dest, owner, repo_name

    async def _fetch(self, dest: Path, owner: str, repo_name: str) -> None:
        # the clone's own origin URL (often SSH) is left as it is; the fetch goes over HTTPS into origin/*
        await self._git("fetch", "--quiet", "--prune", "--", f"{GITHUB}/{owner}/{repo_name}.git",
                        "+refs/heads/*:refs/remotes/origin/*", cwd=dest)

    async def fetch(self, name: str) -> dict:
        dest, owner, repo_name = await self._repo(name)
        async with self._lock(dest.name):
            await self._fetch(dest, owner, repo_name)
            refs = await self._git("for-each-ref", "--format=%(refname:lstrip=3) %(objectname)", "refs/remotes/origin",
                                   cwd=dest)
            branches = dict(line.split(" ", 1) for line in refs.splitlines() if line and not line.startswith("HEAD "))
            return {"name": dest.name, "source": f"{owner}/{repo_name}", "origin": branches}

    async def publish(self, name: str, branch: str) -> dict:
        """Put a local branch on GitHub's write branch: a fast-forward when it can be, otherwise a merge commit of
        the branch onto it. Never forced; a conflict or a blocked path (e.g. .github/workflows/) refuses."""
        branch = check_branch(branch)
        dest, owner, repo_name = await self._repo(name)
        target = self.write_branch
        async with self._lock(dest.name):
            await self._fetch(dest, owner, repo_name)
            source = (await self._git("rev-parse", "-q", "--verify", f"refs/heads/{branch}^{{commit}}", cwd=dest,
                                      check=False)).strip()
            if not source:
                raise WorkspaceError(404, f"no local branch {branch} in {dest.name}")
            current = (await self._git("rev-parse", "-q", "--verify", f"refs/remotes/origin/{target}", cwd=dest,
                                       check=False)).strip()
            result = {"name": dest.name, "source": f"{owner}/{repo_name}", "branch": branch, "published_to": target,
                      "link": f"https://github.com/{owner}/{repo_name}/tree/{target}"}
            if current and await self._is_ancestor(dest, source, current):
                return {**result, "commit": current, "how": f"already on {target}; nothing to push"}
            if not current or await self._is_ancestor(dest, current, source):
                commit, how = source, "fast-forward" if current else f"created {target} from the branch"
            else:
                tree = await self._git("merge-tree", "--write-tree", "--name-only", "--no-messages", current, source,
                                       cwd=dest, check=False)
                lines = tree.splitlines()
                if len(lines) != 1:
                    raise WorkspaceError(409, f"{branch} conflicts with {target} on GitHub in: "
                                              f"{', '.join(lines[1:][:20]) or 'unknown files'}")
                commit = (await self._git("commit-tree", lines[0], "-p", current, "-p", source, "-m",
                                          f"Merge {branch} into {target}", cwd=dest, env=IDENTITY)).strip()
                how = f"merged into {target}"
            # what GitHub's write branch gains (a new one: against the default branch)
            before = current or (await self._git("rev-parse", "-q", "--verify", "refs/remotes/origin/HEAD", cwd=dest,
                                                 check=False)).strip() or EMPTY_TREE
            changed = await self._git("diff", "--name-only", "-z", before, commit, cwd=dest)
            for path in filter(None, changed.split("\0")):
                policy.check_path(path, self.blocked_prefixes)
            await self._git("push", "--quiet", "--", f"{GITHUB}/{owner}/{repo_name}.git",
                            f"{commit}:refs/heads/{target}", cwd=dest)
            await self._git("update-ref", f"refs/remotes/origin/{target}", commit, cwd=dest)
            return {**result, "commit": commit, "how": how,
                    "files": [p for p in changed.split("\0") if p][:50]}

    async def _is_ancestor(self, dest: Path, a: str, b: str) -> bool:
        proc = await asyncio.create_subprocess_exec("git", "merge-base", "--is-ancestor", a, b, cwd=dest,
                                                    env=self._env())
        return await proc.wait() == 0
