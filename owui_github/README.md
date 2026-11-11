# GitHub (dev branch) tool for Open WebUI

Lets Open WebUI models **read any branch** of your GitHub repos, **commit only to `dev`**, and
**open pull requests from `dev` into the default branch** for you to review and merge.

```
 Open WebUI container                       github-tool container (this folder)
 ┌────────────────────────────┐  dmz net   ┌────────────────────────────────────────┐
 │ Tool: github_dev_branch    │──:8000────▶│ holds GITHUB_TOKEN (fine-grained PAT)  │──▶ api.github.com
 │ (../github_dev_branch.json)│ API key +  │ write policy: only refs/heads/dev,     │
 │ no GitHub token            │ OWUI user  │ PRs from dev, no .github/workflows/    │
 └────────────────────────────┘    id      └────────────────────────────────────────┘
```

## Why a sidecar?

GitHub tokens can't be limited to a branch. A fine-grained PAT with `Contents: write` can push to
every branch. So the **sidecar** enforces "dev only", and the token never leaves it:

* Every non-GET request passes `app/policy.py:guard_write`, which only allows: create `refs/heads/dev`,
  fast-forward `refs/heads/dev` (never force), create trees/commits, and open a PR whose head is `dev`.
  Merging, force-pushing, deleting branches, and repo settings are all impossible through the tool.
* Paths under `.github/workflows/` are refused (configurable). A workflow committed to `dev` could run
  Actions with your repository secrets.
* The model never sees the token. The Open WebUI tool only holds a sidecar API key, and the sidecar
  has no published port.

**What this does *not* protect against:** anyone who gets the PAT itself can write to any branch.
Keep `.env` at `chmod 600`, give the token an expiry, and don't grant extra permissions.

## Setup

### 1. Fine-grained personal access token

GitHub → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**

| Setting | Value |
|---|---|
| Resource owner | your account |
| Expiration | your choice (put the renewal in your calendar) |
| Repository access | **All repositories** |
| Permissions → Repository | **Contents: Read and write**, **Pull requests: Read and write** (Metadata: Read is added automatically) |

**Don't** add *Workflows*, *Administration*, *Secrets*, or any account permissions. Without the
*Workflows* permission, GitHub itself also rejects changes to workflow files, as a second layer.
Organization repos need a separate token whose resource owner is that organization.

### 2. Sidecar

```bash
cd ~/code/openwebui_tools/owui_github
cp .env.example .env && chmod 600 .env
openssl rand -hex 32          # → SIDECAR_API_KEY
$EDITOR .env                  # GITHUB_TOKEN, SIDECAR_API_KEY (optionally ALLOWED_OWUI_USER_IDS)
docker compose up -d --build
docker compose logs -f
```

It joins the external `dmz` network as `github-tool`, and Open WebUI reaches it at `http://github-tool:8000`.

### 3. Open WebUI tool

1. **Workspace → Tools → Import** and choose `../github_dev_branch.json` (repo root). Save.
2. Tool **⚙ Valves**: set `SIDECAR_API_KEY` to the same value as in `.env`.
3. Share the tool only with yourself (or the users/groups who should be able to write to your repos),
   and enable it on the models you want. Use a model with native function calling.
   For a hard server-side limit, list the allowed Open WebUI user ids in `ALLOWED_OWUI_USER_IDS`.
4. Check it: ask *"Which GitHub account are you using?"* (`github_whoami`).

## Tools exposed to the model

| Tool | What it does |
|---|---|
| `github_whoami` | GitHub login, write branch, blocked paths |
| `github_list_repos`, `github_repo_info` | repos, default branch, branches, whether `dev` exists |
| `github_list_files`, `github_read_file` | recursive listing / file contents on **any ref** (paged) |
| `github_search_code` | GitHub code search (default branch only, a GitHub limitation) |
| `github_list_commits`, `github_compare` | history; diff of default branch…`dev` (or any two refs) |
| `github_list_pull_requests` | open/closed PRs |
| `github_commit_files` | one commit to **`dev`** with full-content file writes and/or deletes (max 50 files / 5 MB). Creates `dev` from the default branch if missing. Preserves executable bits. |
| `github_open_pull_request` | PR `dev` → default branch (returns the existing one if already open). Cannot merge. |

Commits are authored as the token's account and carry a `Requested-via: Open WebUI (<user>)` trailer.

## Workspace clones (for the coding-agent runner)

With `WORKSPACE_API_KEY` set and `/fast/workspace` mounted read-write, the sidecar also serves the coding-agent
runner (not Open WebUI, which doesn't have this key):

| Endpoint | Does |
|---|---|
| `POST /v1/workspace/clone` `{"repo": "owner/name" or a GitHub URL, "name"?}` | `git clone` over HTTPS into `/fast/workspace/<name>` (409 if it exists); a failed clone leaves nothing behind |
| `POST /v1/workspace/fetch` `{"name"}` | `git fetch --prune` from the clone's GitHub repo into `origin/*`; the clone's own remote URL (often SSH) is kept |
| `POST /v1/workspace/publish` `{"name", "branch"}` | Puts a local branch on `WRITE_BRANCH` (dev): a fast-forward, or a merge commit onto it built without touching the work tree. Never forced; a conflict is a 409 listing the files, and paths under `BLOCKED_PATH_PREFIXES` are refused |

The only write to GitHub is publish, which, like the API tools, only updates `WRITE_BRANCH`. The token goes to git as an
`http.https://github.com/.extraHeader` in the environment, never in a URL, argument or `.git/config`. Run the
container as UID 1000 so clones belong to the workspace owner.

## Issues (for the PO and the runner)

Used by the Issues tool (`../issues.json`) and the runner. Open WebUI's key plus a user id, or the workspace key.

| Endpoint | What it does |
|---|---|
| `POST /v1/issue` `{"repo", "title", "body", "labels", "parent"?}` | Creates an issue, optionally as a sub-issue of an Epic |
| `POST /v1/issue/comment` `{"repo", "number", "body"}` | Comments |
| `GET /v1/issue?repo&number` | The issue, its comments and (for an Epic) its sub-issues |
| `GET /v1/issues?repo&labels=a,b&state=open&limit=50` | Issues with all the labels, oldest first |
| `POST /v1/issue/update` `{"repo", "number", "body"?, "labels"?}` | Replaces the body and/or all labels |

Rules, in the sidecar so a leaked key or a prompt injection cannot go further:

- **Trust.** Only issues and comments written by the account behind the token (plus `TRUSTED_AUTHORS`, default none) are handed on. On a public repo anyone can write an issue; their text is replaced by a placeholder, they do not appear in lists, and they cannot be updated.
- **Labels.** `type:epic`, `type:feature` and `status:draft|review|ready|in-progress|in-pr|needs-you` are the only `type:` and `status:` labels; others are free text.
- **No closing.** The sidecar never closes, reopens, retitles, assigns, locks, transfers or deletes an issue; merging a PR that says `Closes #N` closes it.

## Limits

* Text files only for writes. Content is the whole new file (no patch application).
* Commits fast-forward `dev`. If `dev` moved in the meantime, the commit fails with a "retry" error
  rather than overwriting.
* Code search covers only the default branch. Use `github_list_files` / `github_read_file` with
  `ref=dev` for work in progress.

## Tests

```bash
docker compose build
docker run --rm -u 0 -v "$PWD/sidecar/tests:/srv/tests:ro" --entrypoint sh github-tool:local \
  -c 'pip install -q pytest==8.3.4 && python -m pytest -q -p no:cacheprovider tests'
```
