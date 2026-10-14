"""google-reader sidecar.

Two listeners in one process:
  :8000  API  — Docker-network only. Called by the Open WebUI tool with a shared API key
               and the Open WebUI user id. Returns text/JSON only, never Google tokens.
  :8001  LINK — browser-facing pages where a user connects/disconnects their Google
               account via OAuth (PKCE). The only port published to the host.
"""

import asyncio
import base64
import hashlib
import html
import logging
import os
import secrets
import sys
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Form, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import extract
from .google_api import (
    LOGIN_SCOPES,
    REVOKE_URL,
    SCOPES,
    TOKEN_URL,
    USERINFO_URL,
    Google,
    GoogleError,
    MissingScope,
    NotLinked,
    drop_cached_token,
)
from .store import Store

log = logging.getLogger("google-reader")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

LINK_TTL = 15 * 60
SERVICE_NAMES = {v: k for k, v in SCOPES.items()}


class Config:
    def __init__(self):
        missing = [
            k
            for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "TOKEN_ENCRYPTION_KEY", "SIDECAR_API_KEY", "PUBLIC_BASE_URL")
            if not os.environ.get(k)
        ]
        if missing:
            sys.exit(f"missing required env vars: {', '.join(missing)}")
        self.client_id = os.environ["GOOGLE_CLIENT_ID"]
        self.client_secret = os.environ["GOOGLE_CLIENT_SECRET"]
        self.encryption_key = os.environ["TOKEN_ENCRYPTION_KEY"]
        self.api_key = os.environ["SIDECAR_API_KEY"]
        if len(self.api_key) < 32:
            sys.exit("SIDECAR_API_KEY must be at least 32 characters")
        self.public_base_url = os.environ["PUBLIC_BASE_URL"].rstrip("/")
        # Default: Desktop-app loopback redirect + paste-back (works with LAN IPs, which
        # Google does not accept as web redirect URIs). Set to <PUBLIC_BASE_URL>/oauth/callback
        # with a Web-application client if the link pages are on a real HTTPS hostname.
        self.redirect_uri = os.environ.get("OAUTH_REDIRECT_URI", "http://127.0.0.1:53682/")
        self.direct_callback = self.redirect_uri.startswith(self.public_base_url + "/")
        self.db_path = os.environ.get("DB_PATH", "/data/google-reader.db")


cfg = Config()
store = Store(cfg.db_path, cfg.encryption_key)
http = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10))


# ═════════════════════════════ API (internal) ═════════════════════════════
api = FastAPI(title="google-reader API", docs_url=None, redoc_url=None, openapi_url=None)


def user_google(authorization: str = Header(""), x_owui_user_id: str = Header("")) -> Google:
    if not secrets.compare_digest(authorization.encode(), f"Bearer {cfg.api_key}".encode()):
        raise HTTPException(401, "invalid api key")
    if not x_owui_user_id:
        raise HTTPException(400, "missing X-OWUI-User-Id header")
    return Google(http, store, cfg, x_owui_user_id)


@api.exception_handler(NotLinked)
async def _not_linked(request: Request, exc: NotLinked):
    return JSONResponse({"error": "not_linked", "message": str(exc)}, status_code=409)


@api.exception_handler(MissingScope)
async def _missing_scope(request: Request, exc: MissingScope):
    return JSONResponse(
        {
            "error": "missing_scope",
            "message": f"The linked Google account did not grant access to {exc.service}. "
            "Re-link the account and tick that permission on Google's consent screen.",
        },
        status_code=403,
    )


@api.exception_handler(GoogleError)
async def _google_error(request: Request, exc: GoogleError):
    status = exc.status if exc.status in (400, 403, 404, 413, 415, 429) else 502
    return JSONResponse({"error": "google_error", "status": exc.status, "message": exc.message}, status_code=status)


@api.exception_handler(ValueError)
async def _value_error(request: Request, exc: ValueError):
    return JSONResponse({"error": "bad_request", "message": str(exc)}, status_code=400)


@api.get("/healthz")
async def healthz():
    return {"ok": True}


@api.get("/v1/status")
async def status(g: Google = Depends(user_google)):
    acct = store.get_account(g.user_id)
    if not acct:
        return {"linked": False}
    return {
        "linked": True,
        "email": acct["email"],
        "services": sorted(SERVICE_NAMES[s] for s in acct["scopes"] if s in SERVICE_NAMES),
    }


@api.post("/v1/link")
async def create_link(g: Google = Depends(user_google)):
    token = store.new_link_token(g.user_id, LINK_TTL)
    return {"url": f"{cfg.public_base_url}/link/{token}", "expires_in_minutes": LINK_TTL // 60}


@api.get("/v1/drive/search")
async def drive_search(
    query: str = "",
    file_type: str | None = None,
    modified_after: str | None = None,
    max_results: int = Query(10, ge=1, le=50),
    g: Google = Depends(user_google),
):
    return {"results": await g.drive_search(query, file_type, modified_after, max_results)}


@api.get("/v1/drive/files/{file_id}")
async def drive_read(
    file_id: str,
    offset: int = Query(0, ge=0),
    max_chars: int = Query(20000, ge=1000, le=200000),
    g: Google = Depends(user_google),
):
    info, text = await g.drive_read(file_id)
    return {"file": info, **extract.page_text(text, offset, max_chars)}


@api.get("/v1/gmail/search")
async def gmail_search(query: str = "", max_results: int = Query(10, ge=1, le=50), g: Google = Depends(user_google)):
    return {"results": await g.gmail_search(query, max_results)}


@api.get("/v1/gmail/messages/{message_id}")
async def gmail_message(
    message_id: str,
    offset: int = Query(0, ge=0),
    max_chars: int = Query(20000, ge=1000, le=200000),
    g: Google = Depends(user_google),
):
    return extract.page_text(await g.gmail_message(message_id), offset, max_chars)


@api.get("/v1/gmail/threads/{thread_id}")
async def gmail_thread(
    thread_id: str,
    offset: int = Query(0, ge=0),
    max_chars: int = Query(20000, ge=1000, le=200000),
    g: Google = Depends(user_google),
):
    return extract.page_text(await g.gmail_thread(thread_id), offset, max_chars)


@api.get("/v1/calendar/calendars")
async def calendar_list(g: Google = Depends(user_google)):
    return {"results": await g.calendar_list()}


@api.get("/v1/calendar/events")
async def calendar_events(
    calendar_id: str = "primary",
    time_min: str | None = None,
    time_max: str | None = None,
    query: str | None = None,
    max_results: int = Query(25, ge=1, le=250),
    g: Google = Depends(user_google),
):
    return await g.calendar_events(calendar_id, time_min, time_max, query, max_results)


@api.get("/v1/calendar/event")
async def calendar_event(event_id: str, calendar_id: str = "primary", g: Google = Depends(user_google)):
    return await g.calendar_event(calendar_id, event_id)


@api.get("/v1/contacts/search")
async def contacts_search(query: str, max_results: int = Query(10, ge=1, le=30), g: Google = Depends(user_google)):
    return {"results": await g.contacts_search(query, max_results)}


@api.get("/v1/tasks/lists")
async def task_lists(g: Google = Depends(user_google)):
    return {"results": await g.task_lists()}


@api.get("/v1/tasks")
async def tasks(
    tasklist_id: str = "@default",
    show_completed: bool = False,
    max_results: int = Query(100, ge=1, le=100),
    g: Google = Depends(user_google),
):
    return {"results": await g.tasks(tasklist_id, show_completed, max_results)}


# ═════════════════════════════ LINK (browser) ═════════════════════════════
link = FastAPI(title="google-reader link", docs_url=None, redoc_url=None, openapi_url=None)

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>
:root{{color-scheme:light dark;--fg:#1d1d1f;--bg:#fafafa;--muted:#666;--card:#fff;--line:#ddd;--accent:#1a73e8}}
@media (prefers-color-scheme:dark){{:root{{--fg:#eee;--bg:#161616;--muted:#aaa;--card:#222;--line:#333;--accent:#8ab4f8}}}}
body{{font:16px/1.5 system-ui,sans-serif;color:var(--fg);background:var(--bg);margin:0;padding:24px 16px}}
main{{max-width:620px;margin:auto;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px}}
h1{{font-size:1.3rem;margin-top:0}} .muted{{color:var(--muted)}} code{{word-break:break-all}}
a.btn,button{{display:inline-block;background:var(--accent);color:#fff;border:0;border-radius:8px;padding:10px 16px;
font:inherit;text-decoration:none;cursor:pointer}} button.secondary{{background:#b3261e}}
input[type=text]{{width:100%;box-sizing:border-box;padding:10px;font:inherit;border:1px solid var(--line);border-radius:8px;
background:var(--bg);color:var(--fg);margin:8px 0}} ol li{{margin:6px 0}}
</style></head><body><main><h1>{title}</h1>{body}</main></body></html>"""


def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(PAGE.format(title=html.escape(title), body=body), status_code=status)


def expired_page() -> HTMLResponse:
    return page(
        "Link expired",
        "<p>This link is invalid or has expired. Ask the assistant in Open WebUI to connect your "
        "Google account again to get a new link.</p>",
        404,
    )


@link.get("/link/{token}")
async def link_page(token: str):
    user_id = store.link_user(token)
    if not user_id:
        return expired_page()
    acct = store.get_account(user_id)
    t = html.escape(token)
    parts = []
    if acct:
        services = ", ".join(sorted(SERVICE_NAMES[s] for s in acct["scopes"] if s in SERVICE_NAMES)) or "none"
        parts.append(
            f"<p>Currently linked: <strong>{html.escape(acct['email'] or 'unknown')}</strong><br>"
            f"<span class='muted'>Read-only access to: {html.escape(services)}</span></p>"
            f"<form method='post' action='/link/{t}/disconnect'>"
            "<button class='secondary' type='submit'>Disconnect and revoke access</button></form><hr>"
        )
    if cfg.direct_callback:
        parts.append(f"<p><a class='btn' href='/link/{t}/google'>Connect Google account</a></p>")
    else:
        parts.append(
            "<ol>"
            f"<li><a class='btn' href='/link/{t}/google' target='_blank' rel='noopener'>Open Google sign-in</a> "
            "(opens a new tab). Approve the read-only permissions. Google may warn that the app is "
            "unverified: choose <em>Advanced → Go to … (unsafe)</em> — it is your own OAuth app.</li>"
            "<li>Google then redirects to a <code>127.0.0.1</code> page that <strong>fails to load</strong>. "
            "That's expected. Copy the full address from that tab's address bar.</li>"
            "<li>Paste it here:"
            f"<form method='post' action='/link/{t}/complete'>"
            "<input type='text' name='redirected_url' placeholder='http://127.0.0.1:53682/?state=…&amp;code=…' required>"
            "<button type='submit'>Finish linking</button></form></li></ol>"
        )
    parts.append("<p class='muted'>The assistant never sees your Google credentials; they stay in the google-reader service.</p>")
    return page("Connect Google account", "".join(parts))


@link.get("/link/{token}/google")
async def start_google(token: str):
    if not store.link_user(token):
        return expired_page()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = store.new_state(token, verifier, LINK_TTL)
    params = {
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "response_type": "code",
        "scope": " ".join(LOGIN_SCOPES + list(SCOPES.values())),
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params), status_code=302)


async def finish_link(code: str | None, state: str | None, error: str | None, expected_token: str | None):
    if error:
        return page("Linking cancelled", f"<p>Google returned: <code>{html.escape(error)}</code></p>", 400)
    if not code or not state:
        return page("Linking failed", "<p>That URL has no <code>code</code>/<code>state</code>. Copy the full address.</p>", 400)
    st = store.pop_state(state)
    if not st or (expected_token and st[0] != expected_token):
        return page("Linking failed", "<p>Sign-in session expired or doesn't match this link. Start again.</p>", 400)
    link_token, verifier = st
    user_id = store.link_user(link_token)
    if not user_id:
        return expired_page()

    r = await http.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "redirect_uri": cfg.redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        },
    )
    if r.status_code != 200:
        detail = r.json().get("error_description", "") if r.headers.get("content-type", "").startswith("application/json") else ""
        log.warning("code exchange failed: %s %s", r.status_code, detail)
        return page("Linking failed", f"<p>Google rejected the code: {html.escape(detail or str(r.status_code))}</p>", 400)
    tok = r.json()
    refresh = tok.get("refresh_token")
    if not refresh:
        return page("Linking failed", "<p>Google did not return a refresh token. Try again.</p>", 400)
    granted = tok.get("scope", "").split()
    ui = await http.get(USERINFO_URL, headers={"Authorization": f"Bearer {tok['access_token']}"})
    email = ui.json().get("email") if ui.status_code == 200 else None

    old = store.get_account(user_id)
    store.save_account(user_id, email, granted, refresh)
    drop_cached_token(user_id)
    if old and old["refresh_token"] != refresh:
        await http.post(REVOKE_URL, data={"token": old["refresh_token"]})
    log.info("linked owui user %s to google account %s", user_id, email)

    got = sorted(SERVICE_NAMES[s] for s in granted if s in SERVICE_NAMES)
    missing = sorted(k for k, v in SCOPES.items() if v not in granted)
    body = f"<p>Linked <strong>{html.escape(email or 'unknown')}</strong> with read-only access to: {html.escape(', '.join(got) or 'nothing')}.</p>"
    if missing:
        body += f"<p class='muted'>Not granted: {html.escape(', '.join(missing))}. Re-link and tick those boxes if you want them.</p>"
    body += "<p>You can close this tab and go back to Open WebUI.</p>"
    return page("Google account linked", body)


@link.get("/oauth/callback")
async def oauth_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    return await finish_link(code, state, error, None)


@link.post("/link/{token}/complete")
async def paste_complete(token: str, redirected_url: str = Form(...)):
    qs = parse_qs(urlparse(redirected_url.strip()).query)
    first = lambda k: (qs.get(k) or [None])[0]  # noqa: E731
    return await finish_link(first("code"), first("state"), first("error"), token)


@link.post("/link/{token}/disconnect")
async def disconnect(token: str):
    user_id = store.link_user(token)
    if not user_id:
        return expired_page()
    acct = store.get_account(user_id)
    if acct:
        await http.post(REVOKE_URL, data={"token": acct["refresh_token"]})
        store.delete_account(user_id)
        drop_cached_token(user_id)
        log.info("unlinked owui user %s", user_id)
    return page("Disconnected", "<p>Google access was revoked and the stored token deleted.</p>")


# ═════════════════════════════ entrypoint ═════════════════════════════
async def serve():
    servers = [
        uvicorn.Server(uvicorn.Config(api, host="0.0.0.0", port=8000, log_level="info")),
        # access_log off: the paste/callback URLs carry OAuth codes and link tokens
        uvicorn.Server(uvicorn.Config(link, host="0.0.0.0", port=8001, log_level="info", access_log=False)),
    ]

    async def run(server: uvicorn.Server):
        try:
            await server.serve()
        finally:
            for other in servers:
                other.should_exit = True

    await asyncio.gather(*(run(s) for s in servers))


if __name__ == "__main__":
    asyncio.run(serve())
