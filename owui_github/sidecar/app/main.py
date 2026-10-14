"""github-tool sidecar.

Holds the GitHub token so Open WebUI never does. Listens on :8000 on the Docker network only
(no published port). The Open WebUI tool authenticates with a shared API key and the Open
WebUI user id. Reads work on any branch; writes only ever touch WRITE_BRANCH.
"""

import logging
import os
import secrets
import sys

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import policy
from .gh import GitHub, GitHubError

log = logging.getLogger("github-tool")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


class Config:
    def __init__(self):
        missing = [k for k in ("GITHUB_TOKEN", "SIDECAR_API_KEY") if not os.environ.get(k)]
        if missing:
            sys.exit(f"missing required env vars: {', '.join(missing)}")
        self.token = os.environ["GITHUB_TOKEN"]
        self.api_key = os.environ["SIDECAR_API_KEY"]
        if len(self.api_key) < 32:
            sys.exit("SIDECAR_API_KEY must be at least 32 characters")
        self.write_branch = policy.check_name(os.environ.get("WRITE_BRANCH", "dev"), "WRITE_BRANCH")
        self.blocked_prefixes = [
            p.strip() for p in os.environ.get("BLOCKED_PATH_PREFIXES", ".github/workflows/").split(",") if p.strip()
        ]
        self.allowed_users = {u.strip() for u in os.environ.get("ALLOWED_OWUI_USER_IDS", "").split(",") if u.strip()}


cfg = Config()
http = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10))
github = GitHub(http, cfg)

app = FastAPI(title="github-tool", docs_url=None, redoc_url=None, openapi_url=None)


def authorize(authorization: str = Header(""), x_owui_user_id: str = Header("")) -> str:
    if not secrets.compare_digest(authorization.encode(), f"Bearer {cfg.api_key}".encode()):
        raise HTTPException(401, "invalid api key")
    if not x_owui_user_id:
        raise HTTPException(400, "missing X-OWUI-User-Id header")
    if cfg.allowed_users and x_owui_user_id not in cfg.allowed_users:
        raise HTTPException(403, "this Open WebUI user is not allowed to use the GitHub tool")
    return x_owui_user_id


@app.exception_handler(policy.PolicyError)
async def _policy_error(request: Request, exc: policy.PolicyError):
    return JSONResponse({"error": "not_allowed", "message": str(exc)}, status_code=400)


@app.exception_handler(GitHubError)
async def _github_error(request: Request, exc: GitHubError):
    status = exc.status if exc.status in (400, 403, 404, 409, 413, 415, 422, 429) else 502
    return JSONResponse({"error": "github_error", "status": exc.status, "message": exc.message}, status_code=status)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/v1/whoami")
async def whoami(user: str = Depends(authorize)):
    return {
        "github_login": await github.login(),
        "write_branch": cfg.write_branch,
        "blocked_path_prefixes": cfg.blocked_prefixes,
    }


@app.get("/v1/repos")
async def list_repos(query: str = "", max_results: int = Query(30, ge=1, le=100), user: str = Depends(authorize)):
    return {"results": await github.list_repos(query, max_results)}


@app.get("/v1/repo")
async def repo_info(repo: str, user: str = Depends(authorize)):
    return await github.repo_info(*await github.resolve(repo))


@app.get("/v1/tree")
async def tree(
    repo: str,
    ref: str | None = None,
    path: str = "",
    max_entries: int = Query(500, ge=1, le=5000),
    user: str = Depends(authorize),
):
    return await github.tree(*await github.resolve(repo), ref, path, max_entries)


@app.get("/v1/file")
async def read_file(
    repo: str,
    path: str,
    ref: str | None = None,
    offset: int = Query(0, ge=0),
    max_chars: int = Query(40000, ge=1000, le=200000),
    user: str = Depends(authorize),
):
    meta, text = await github.read_file(*await github.resolve(repo), path, ref)
    return {"file": meta, **policy.page_text(text, offset, max_chars)}


@app.get("/v1/commits")
async def commits(
    repo: str,
    ref: str | None = None,
    path: str | None = None,
    max_results: int = Query(20, ge=1, le=100),
    user: str = Depends(authorize),
):
    return {"results": await github.commits(*await github.resolve(repo), ref, path, max_results)}


@app.get("/v1/compare")
async def compare(repo: str, base: str | None = None, head: str | None = None, user: str = Depends(authorize)):
    return await github.compare(*await github.resolve(repo), base, head)


@app.get("/v1/search/code")
async def search_code(
    query: str, repo: str | None = None, max_results: int = Query(20, ge=1, le=50), user: str = Depends(authorize)
):
    return {"results": await github.search_code(query, repo, max_results)}


@app.get("/v1/pulls")
async def pulls(repo: str, state: str = Query("open", pattern="^(open|closed|all)$"), user: str = Depends(authorize)):
    return {"results": await github.pulls(*await github.resolve(repo), state)}


class Change(BaseModel):
    path: str
    content: str | None = None
    delete: bool = False


class CommitRequest(BaseModel):
    repo: str
    message: str
    changes: list[Change] = Field(min_length=1)
    requested_by: str | None = None


class PullRequestRequest(BaseModel):
    repo: str
    title: str
    body: str = ""
    draft: bool = False
    requested_by: str | None = None


@app.post("/v1/commit")
async def commit(req: CommitRequest, user: str = Depends(authorize)):
    owner, repo = await github.resolve(req.repo)
    result = await github.commit(owner, repo, req.message, [c.model_dump() for c in req.changes], req.requested_by)
    log.info("owui user %s committed to %s/%s@%s: %s", user, owner, repo, cfg.write_branch, result.get("commit"))
    return result


@app.post("/v1/pull-request")
async def pull_request(req: PullRequestRequest, user: str = Depends(authorize)):
    owner, repo = await github.resolve(req.repo)
    result = await github.open_pull_request(owner, repo, req.title, req.body, req.draft, req.requested_by)
    log.info("owui user %s pull request %s/%s: %s", user, owner, repo, result.get("link"))
    return result


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
