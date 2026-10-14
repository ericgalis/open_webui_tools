"""GitHub REST client. Reads any branch; writes go only to the configured write branch.

All writes funnel through `GitHub.req`, which runs `policy.guard_write` on every non-GET
request before it leaves the container.
"""

import base64
from urllib.parse import quote

import httpx

from . import policy

API = "https://api.github.com"
MAX_CHANGES = 50
MAX_FILE_BYTES = 1024 * 1024
MAX_COMMIT_BYTES = 5 * 1024 * 1024
MAX_PATCH_CHARS = 4000


class GitHubError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _error_message(r: httpx.Response) -> str:
    try:
        data = r.json()
        msg = data.get("message", "")
        errors = data.get("errors")
        if errors:
            msg += ": " + "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in errors)
        return msg or r.text[:300]
    except ValueError:
        return r.text[:300]


def _short_commit(c: dict) -> dict:
    commit = c.get("commit", {})
    return {
        "sha": c["sha"][:12],
        "date": (commit.get("author") or {}).get("date"),
        "author": (commit.get("author") or {}).get("name"),
        "message": (commit.get("message") or "").split("\n", 1)[0],
    }


class GitHub:
    def __init__(self, http: httpx.AsyncClient, cfg):
        self.http = http
        self.cfg = cfg
        self._login: str | None = None

    async def req(self, method: str, path: str, *, params=None, json: dict | None = None, raw: bool = False, accept: str | None = None):
        if method != "GET":
            policy.guard_write(method, path, json, self.cfg.write_branch)
        headers = {
            "Authorization": f"Bearer {self.cfg.token}",
            "Accept": accept or ("application/vnd.github.raw+json" if raw else "application/vnd.github+json"),
            "X-GitHub-Api-Version": "2022-11-28",
        }
        r = await self.http.request(method, API + path, params=params, json=json, headers=headers)
        if r.status_code >= 400:
            raise GitHubError(r.status_code, _error_message(r))
        if r.status_code == 204:
            return None
        return r.content if raw else r.json()

    async def login(self) -> str:
        if self._login is None:
            self._login = (await self.req("GET", "/user"))["login"]
        return self._login

    async def resolve(self, repo: str) -> tuple[str, str]:
        default_owner = await self.login() if "/" not in (repo or "") else ""
        return policy.split_repo(repo, default_owner)

    # ── reads ──
    async def list_repos(self, query: str, max_results: int) -> list[dict]:
        out = []
        page = 1
        while len(out) < max_results and page <= 10:
            data = await self.req(
                "GET",
                "/user/repos",
                params={"affiliation": "owner,collaborator,organization_member", "sort": "pushed", "per_page": 100, "page": page},
            )
            if not data:
                break
            for r in data:
                if query and query.lower() not in (r["full_name"] + " " + (r.get("description") or "")).lower():
                    continue
                out.append(
                    {
                        "repo": r["full_name"],
                        "private": r["private"],
                        "default_branch": r["default_branch"],
                        "description": r.get("description"),
                        "language": r.get("language"),
                        "pushed_at": r.get("pushed_at"),
                        "archived": r.get("archived"),
                    }
                )
            page += 1
        return out[:max_results]

    async def repo_info(self, owner: str, repo: str) -> dict:
        r = await self.req("GET", f"/repos/{owner}/{repo}")
        branches = await self.req("GET", f"/repos/{owner}/{repo}/branches", params={"per_page": 100})
        names = [b["name"] for b in branches]
        return {
            "repo": r["full_name"],
            "private": r["private"],
            "default_branch": r["default_branch"],
            "write_branch": self.cfg.write_branch,
            "write_branch_exists": self.cfg.write_branch in names,
            "can_push": (r.get("permissions") or {}).get("push", False),
            "archived": r.get("archived"),
            "description": r.get("description"),
            "language": r.get("language"),
            "branches": names,
        }

    async def _commit_for(self, owner: str, repo: str, ref: str) -> dict:
        data = await self.req("GET", f"/repos/{owner}/{repo}/commits", params={"sha": ref, "per_page": 1})
        if not data:
            raise GitHubError(404, f"ref {ref!r} not found or has no commits")
        return data[0]

    async def tree(self, owner: str, repo: str, ref: str | None, path: str, max_entries: int) -> dict:
        ref = ref or (await self.req("GET", f"/repos/{owner}/{repo}"))["default_branch"]
        commit = await self._commit_for(owner, repo, ref)
        data = await self.req("GET", f"/repos/{owner}/{repo}/git/trees/{commit['commit']['tree']['sha']}", params={"recursive": "1"})
        prefix = path.strip("/") + "/" if path.strip("/") else ""
        entries = [
            f"{'dir ' if e['type'] == 'tree' else 'file'}  {e['path']}" + (f"  ({e['size']} B)" if e.get("size") is not None else "")
            for e in data.get("tree", [])
            if e["path"].startswith(prefix) and e["type"] in ("blob", "tree")
        ]
        return {
            "ref": ref,
            "commit": commit["sha"][:12],
            "path": prefix or "/",
            "total_entries": len(entries),
            "entries": entries[:max_entries],
            "truncated": len(entries) > max_entries or bool(data.get("truncated")),
        }

    async def read_file(self, owner: str, repo: str, path: str, ref: str | None) -> tuple[dict, str]:
        policy.check_path(path, [])
        params = {"ref": ref} if ref else None
        data = await self.req("GET", f"/repos/{owner}/{repo}/contents/{quote(path)}", params=params)
        if isinstance(data, list):
            listing = "\n".join(f"{'dir ' if e['type'] == 'dir' else 'file'}  {e['path']}" for e in data)
            return {"path": path, "type": "dir", "ref": ref}, listing
        if data.get("type") != "file":
            raise GitHubError(415, f"{path} is a {data.get('type')}, not a file")
        if data.get("encoding") == "base64" and data.get("content"):
            blob = base64.b64decode(data["content"])
        else:  # files over 1 MB come back without inline content
            blob = await self.req("GET", f"/repos/{owner}/{repo}/contents/{quote(path)}", params=params, raw=True)
        meta = {"path": path, "type": "file", "ref": ref, "size": data.get("size"), "sha": data.get("sha"), "link": data.get("html_url")}
        if b"\x00" in blob[:8192]:
            return meta, f"[binary file, {len(blob)} bytes — not shown]"
        return meta, blob.decode("utf-8", errors="replace")

    async def commits(self, owner: str, repo: str, ref: str | None, path: str | None, max_results: int) -> list[dict]:
        params = {"per_page": max_results}
        if ref:
            params["sha"] = ref
        if path:
            params["path"] = path
        return [_short_commit(c) for c in await self.req("GET", f"/repos/{owner}/{repo}/commits", params=params)]

    async def compare(self, owner: str, repo: str, base: str | None, head: str | None) -> dict:
        base = base or (await self.req("GET", f"/repos/{owner}/{repo}"))["default_branch"]
        head = head or self.cfg.write_branch
        data = await self.req("GET", f"/repos/{owner}/{repo}/compare/{quote(base, safe='/')}...{quote(head, safe='/')}")
        files = []
        for f in data.get("files", []):
            patch = f.get("patch") or ""
            if len(patch) > MAX_PATCH_CHARS:
                patch = patch[:MAX_PATCH_CHARS] + "\n… (patch truncated; read the file for full content)"
            files.append(
                {
                    "file": f["filename"],
                    "status": f["status"],
                    "additions": f.get("additions"),
                    "deletions": f.get("deletions"),
                    "patch": patch or None,
                }
            )
        return {
            "base": base,
            "head": head,
            "status": data.get("status"),
            "ahead_by": data.get("ahead_by"),
            "behind_by": data.get("behind_by"),
            "commits": [_short_commit(c) for c in data.get("commits", [])][-50:],
            "files": files,
        }

    async def search_code(self, query: str, repo: str | None, max_results: int) -> list[dict]:
        if repo:
            owner, name = await self.resolve(repo)
            q = f"{query} repo:{owner}/{name}"
        else:
            q = f"{query} user:{await self.login()}"
        data = await self.req(
            "GET", "/search/code", params={"q": q, "per_page": max_results}, accept="application/vnd.github.text-match+json"
        )
        return [
            {
                "repo": i["repository"]["full_name"],
                "path": i["path"],
                "matches": [m.get("fragment") for m in i.get("text_matches", [])][:3],
            }
            for i in data.get("items", [])
        ]

    async def pulls(self, owner: str, repo: str, state: str) -> list[dict]:
        data = await self.req("GET", f"/repos/{owner}/{repo}/pulls", params={"state": state, "per_page": 30})
        return [
            {
                "number": p["number"],
                "title": p["title"],
                "state": p["state"],
                "head": p["head"]["ref"],
                "base": p["base"]["ref"],
                "author": p["user"]["login"],
                "updated": p.get("updated_at"),
                "link": p["html_url"],
            }
            for p in data
        ]

    # ── writes (write branch only) ──
    async def _ensure_write_branch(self, owner: str, repo: str, default_branch: str) -> tuple[str, bool]:
        wb = self.cfg.write_branch
        try:
            ref = await self.req("GET", f"/repos/{owner}/{repo}/git/ref/heads/{wb}")
            return ref["object"]["sha"], False
        except GitHubError as e:
            if e.status != 404:
                raise
        try:
            base = await self.req("GET", f"/repos/{owner}/{repo}/git/ref/heads/{default_branch}")
        except GitHubError as e:
            if e.status in (404, 409):
                raise GitHubError(409, "repository is empty; push an initial commit first") from e
            raise
        await self.req("POST", f"/repos/{owner}/{repo}/git/refs", json={"ref": f"refs/heads/{wb}", "sha": base["object"]["sha"]})
        return base["object"]["sha"], True

    async def commit(self, owner: str, repo: str, message: str, changes: list[dict], requested_by: str | None) -> dict:
        if not message or not message.strip():
            raise policy.PolicyError("commit message is required")
        if not changes:
            raise policy.PolicyError("changes must contain at least one file")
        if len(changes) > MAX_CHANGES:
            raise policy.PolicyError(f"at most {MAX_CHANGES} files per commit")
        seen, total = set(), 0
        for c in changes:
            path = policy.check_path(c.get("path"), self.cfg.blocked_prefixes)
            if path in seen:
                raise policy.PolicyError(f"duplicate path: {path}")
            seen.add(path)
            if c.get("delete"):
                continue
            content = c.get("content")
            if not isinstance(content, str):
                raise policy.PolicyError(f"{path}: content must be a string (or set delete: true)")
            size = len(content.encode())
            if size > MAX_FILE_BYTES:
                raise policy.PolicyError(f"{path}: larger than {MAX_FILE_BYTES // 1024} KB")
            total += size
        if total > MAX_COMMIT_BYTES:
            raise policy.PolicyError(f"commit larger than {MAX_COMMIT_BYTES // 1024 // 1024} MB")

        info = await self.req("GET", f"/repos/{owner}/{repo}")
        if info.get("archived"):
            raise GitHubError(403, "repository is archived")
        head_sha, created = await self._ensure_write_branch(owner, repo, info["default_branch"])
        base_tree = (await self.req("GET", f"/repos/{owner}/{repo}/git/commits/{head_sha}"))["tree"]["sha"]

        # Preserve existing file modes (e.g. executable scripts) where the tree listing allows.
        listing = await self.req("GET", f"/repos/{owner}/{repo}/git/trees/{base_tree}", params={"recursive": "1"})
        modes = {e["path"]: e["mode"] for e in listing.get("tree", []) if e["type"] == "blob"}
        entries = []
        for c in changes:
            mode = modes.get(c["path"]) if modes.get(c["path"]) in ("100644", "100755") else "100644"
            if c.get("delete"):
                if c["path"] not in modes and not listing.get("truncated"):
                    raise policy.PolicyError(f"cannot delete {c['path']}: not found on {self.cfg.write_branch}")
                entries.append({"path": c["path"], "mode": mode, "type": "blob", "sha": None})
            else:
                entries.append({"path": c["path"], "mode": mode, "type": "blob", "content": c["content"]})

        tree = await self.req("POST", f"/repos/{owner}/{repo}/git/trees", json={"base_tree": base_tree, "tree": entries})
        if tree["sha"] == base_tree:
            return {"branch": self.cfg.write_branch, "created_branch": created, "committed": False, "message": "no changes; files already match"}

        full_message = message.strip()
        if requested_by:
            full_message += f"\n\nRequested-via: Open WebUI ({requested_by})"
        commit = await self.req(
            "POST",
            f"/repos/{owner}/{repo}/git/commits",
            json={"message": full_message, "tree": tree["sha"], "parents": [head_sha]},
        )
        try:
            await self.req(
                "PATCH",
                f"/repos/{owner}/{repo}/git/refs/heads/{self.cfg.write_branch}",
                json={"sha": commit["sha"], "force": False},
            )
        except GitHubError as e:
            if e.status == 422:
                raise GitHubError(409, f"{self.cfg.write_branch} changed while committing; re-read and retry") from e
            raise
        return {
            "branch": self.cfg.write_branch,
            "created_branch": created,
            "committed": True,
            "commit": commit["sha"][:12],
            "link": f"https://github.com/{owner}/{repo}/commit/{commit['sha']}",
            "files": [{"path": c["path"], "action": "delete" if c.get("delete") else "write"} for c in changes],
        }

    async def open_pull_request(self, owner: str, repo: str, title: str, body: str, draft: bool, requested_by: str | None) -> dict:
        wb = self.cfg.write_branch
        if not title or not title.strip():
            raise policy.PolicyError("title is required")
        base = (await self.req("GET", f"/repos/{owner}/{repo}"))["default_branch"]
        if base == wb:
            raise policy.PolicyError(f"the default branch is {wb}; nothing to open a PR into")
        existing = await self.req(
            "GET", f"/repos/{owner}/{repo}/pulls", params={"head": f"{owner}:{wb}", "base": base, "state": "open"}
        )
        if existing:
            p = existing[0]
            return {"created": False, "number": p["number"], "title": p["title"], "link": p["html_url"], "message": "an open PR from this branch already exists"}
        cmp = await self.compare(owner, repo, base, wb)
        if not cmp["ahead_by"]:
            raise policy.PolicyError(f"{wb} has no commits ahead of {base}")
        full_body = (body or "").strip()
        if requested_by:
            full_body += f"\n\n---\n_Opened via Open WebUI ({requested_by})._"
        p = await self.req(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            json={"title": title.strip(), "body": full_body, "head": wb, "base": base, "draft": draft},
        )
        return {"created": True, "number": p["number"], "title": p["title"], "base": base, "head": wb, "link": p["html_url"]}
