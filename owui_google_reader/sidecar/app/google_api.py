"""Read-only Google API client for one Open WebUI user.

Read-only is enforced twice: the OAuth grant only contains *.readonly scopes, and this
class only issues GET requests to Google APIs. The only POSTs go to the OAuth token
endpoint (refresh) — see `_access_token`.
"""

import asyncio
import html
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from . import extract

TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
DRIVE = "https://www.googleapis.com/drive/v3"
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
CAL = "https://www.googleapis.com/calendar/v3"
PEOPLE = "https://people.googleapis.com/v1"
TASKS = "https://tasks.googleapis.com/tasks/v1"

SCOPES = {
    "drive": "https://www.googleapis.com/auth/drive.readonly",
    "gmail": "https://www.googleapis.com/auth/gmail.readonly",
    "calendar": "https://www.googleapis.com/auth/calendar.readonly",
    "contacts": "https://www.googleapis.com/auth/contacts.readonly",
    "other_contacts": "https://www.googleapis.com/auth/contacts.other.readonly",
    "tasks": "https://www.googleapis.com/auth/tasks.readonly",
}
LOGIN_SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email"]

MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024

# user_id -> (access_token, expires_at). Access tokens live only in memory.
_token_cache: dict[str, tuple[str, float]] = {}
_people_warmed: set[str] = set()


class NotLinked(Exception):
    pass


class MissingScope(Exception):
    def __init__(self, service: str):
        super().__init__(service)
        self.service = service


class GoogleError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def drop_cached_token(user_id: str):
    _token_cache.pop(user_id, None)


def _error_message(r: httpx.Response) -> str:
    try:
        err = r.json().get("error")
        if isinstance(err, dict):
            return err.get("message") or str(err.get("status"))
        return str(err or r.text[:300])
    except ValueError:
        return r.text[:300]


class Google:
    def __init__(self, http: httpx.AsyncClient, store, cfg, user_id: str):
        self.http = http
        self.store = store
        self.cfg = cfg
        self.user_id = user_id
        self._account = None

    def account(self) -> dict:
        if self._account is None:
            self._account = self.store.get_account(self.user_id)
        if not self._account:
            raise NotLinked("No Google account is linked for this Open WebUI user.")
        return self._account

    def has(self, service: str) -> bool:
        return SCOPES[service] in self.account()["scopes"]

    def require(self, service: str):
        if not self.has(service):
            raise MissingScope(service)

    async def _access_token(self, force: bool = False) -> str:
        acct = self.account()
        cached = _token_cache.get(self.user_id)
        if cached and not force and cached[1] > time.time() + 60:
            return cached[0]
        r = await self.http.post(
            TOKEN_URL,
            data={
                "client_id": self.cfg.client_id,
                "client_secret": self.cfg.client_secret,
                "refresh_token": acct["refresh_token"],
                "grant_type": "refresh_token",
            },
        )
        if r.status_code in (400, 401) and _error_message(r) == "invalid_grant":
            self.store.delete_account(self.user_id)
            drop_cached_token(self.user_id)
            raise NotLinked("Google access was revoked or expired. The account must be linked again.")
        if r.status_code != 200:
            raise GoogleError(r.status_code, f"token refresh failed: {_error_message(r)}")
        tok = r.json()
        _token_cache[self.user_id] = (tok["access_token"], time.time() + int(tok.get("expires_in", 3600)))
        return tok["access_token"]

    async def get(self, url: str, params=None, *, raw: bool = False):
        for attempt in (0, 1):
            token = await self._access_token(force=attempt == 1)
            r = await self.http.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
            if r.status_code != 401:
                break
        if r.status_code >= 400:
            raise GoogleError(r.status_code, _error_message(r))
        return r.content if raw else r.json()

    # ── Drive / Docs / Sheets / Slides ──
    async def drive_search(self, query, file_type, modified_after, max_results) -> list[dict]:
        self.require("drive")
        params = {
            "q": extract.drive_query(query, file_type, modified_after),
            "pageSize": max_results,
            "fields": "files(id,name,mimeType,modifiedTime,size,webViewLink,owners(displayName,emailAddress))",
            "corpora": "allDrives",
            "includeItemsFromAllDrives": "true",
            "supportsAllDrives": "true",
        }
        if not query:  # Drive rejects orderBy on fullText queries
            params["orderBy"] = "modifiedTime desc"
        data = await self.get(f"{DRIVE}/files", params)
        return [
            {
                "id": f["id"],
                "name": f["name"],
                "type": extract.friendly_type(f["mimeType"]),
                "modified": f.get("modifiedTime"),
                "owner": next((o.get("emailAddress") for o in f.get("owners", [])), None),
                "link": f.get("webViewLink"),
            }
            for f in data.get("files", [])
        ]

    async def _export(self, file_id: str, mime: str) -> str:
        data = await self.get(f"{DRIVE}/files/{file_id}/export", {"mimeType": mime}, raw=True)
        return data.decode("utf-8", errors="replace")

    async def _sheet_text(self, file_id: str) -> str:
        meta = await self.get(f"{SHEETS}/{file_id}", {"fields": "sheets.properties(title,sheetType)"})
        titles = [
            s["properties"]["title"]
            for s in meta.get("sheets", [])
            if s["properties"].get("sheetType", "GRID") == "GRID"
        ]
        if not titles:
            return ""
        params = [("ranges", "'" + t.replace("'", "''") + "'") for t in titles]
        params.append(("valueRenderOption", "FORMATTED_VALUE"))
        values = await self.get(f"{SHEETS}/{file_id}/values:batchGet", params)
        return "\n\n".join(
            extract.sheet_to_text(t, vr.get("values", [])) for t, vr in zip(titles, values.get("valueRanges", []))
        )

    async def drive_read(self, file_id: str) -> tuple[dict, str]:
        self.require("drive")
        extract.check_id(file_id, "file_id")
        meta = await self.get(
            f"{DRIVE}/files/{file_id}",
            {"fields": "id,name,mimeType,modifiedTime,size,webViewLink", "supportsAllDrives": "true"},
        )
        mime = meta["mimeType"]
        info = {
            "id": meta["id"],
            "name": meta["name"],
            "type": extract.friendly_type(mime),
            "modified": meta.get("modifiedTime"),
            "link": meta.get("webViewLink"),
        }
        if mime == extract.GOOGLE_DOC:
            try:
                text = await self._export(file_id, "text/markdown")
            except GoogleError:
                text = await self._export(file_id, "text/plain")
        elif mime == extract.GOOGLE_SHEET:
            text = await self._sheet_text(file_id)
        elif mime == extract.GOOGLE_SLIDES:
            text = await self._export(file_id, "text/plain")
        elif mime == extract.GOOGLE_FOLDER:
            children = await self.get(
                f"{DRIVE}/files",
                {
                    "q": f"'{file_id}' in parents and trashed = false",
                    "pageSize": 200,
                    "fields": "files(id,name,mimeType,modifiedTime)",
                    "includeItemsFromAllDrives": "true",
                    "supportsAllDrives": "true",
                },
            )
            text = "\n".join(
                f"{c['id']}\t{extract.friendly_type(c['mimeType'])}\t{c.get('modifiedTime')}\t{c['name']}"
                for c in children.get("files", [])
            )
        elif mime.startswith("application/vnd.google-apps."):
            raise GoogleError(415, f"cannot read {mime} as text")
        else:
            if int(meta.get("size") or 0) > MAX_DOWNLOAD_BYTES:
                raise GoogleError(413, f"file is larger than {MAX_DOWNLOAD_BYTES // 1024 // 1024} MB")
            if mime == "application/pdf":
                data = await self.get(f"{DRIVE}/files/{file_id}", {"alt": "media", "supportsAllDrives": "true"}, raw=True)
                try:
                    text = extract.pdf_to_text(data)
                except Exception as e:
                    raise GoogleError(415, f"could not extract PDF text: {type(e).__name__}")
            elif mime.startswith("text/") or mime in extract.TEXTUAL_MIMES:
                data = await self.get(f"{DRIVE}/files/{file_id}", {"alt": "media", "supportsAllDrives": "true"}, raw=True)
                text = data.decode("utf-8", errors="replace")
            else:
                raise GoogleError(415, f"unsupported file type for text extraction: {mime}")
        return info, text

    # ── Gmail ──
    async def gmail_search(self, query: str, max_results: int) -> list[dict]:
        self.require("gmail")
        data = await self.get(f"{GMAIL}/messages", {"q": query, "maxResults": max_results})
        sem = asyncio.Semaphore(8)
        meta_params = [("format", "metadata")] + [("metadataHeaders", h) for h in ("From", "To", "Subject", "Date")]

        async def one(mid: str) -> dict:
            async with sem:
                m = await self.get(f"{GMAIL}/messages/{mid}", meta_params)
            h = extract.headers_dict(m.get("payload", {}).get("headers", []))
            return {
                "id": m["id"],
                "thread_id": m.get("threadId"),
                "date": h.get("date"),
                "from": h.get("from"),
                "to": h.get("to"),
                "subject": h.get("subject"),
                "snippet": html.unescape(m.get("snippet", "")),
                "labels": m.get("labelIds", []),
            }

        return list(await asyncio.gather(*(one(m["id"]) for m in data.get("messages", []))))

    async def gmail_message(self, message_id: str) -> str:
        self.require("gmail")
        extract.check_id(message_id, "message_id")
        msg = await self.get(f"{GMAIL}/messages/{message_id}", {"format": "full"})
        return extract.format_gmail_message(msg)

    async def gmail_thread(self, thread_id: str) -> str:
        self.require("gmail")
        extract.check_id(thread_id, "thread_id")
        thread = await self.get(f"{GMAIL}/threads/{thread_id}", {"format": "full"})
        return "\n\n==========\n\n".join(extract.format_gmail_message(m) for m in thread.get("messages", []))

    # ── Calendar ──
    async def calendar_list(self) -> list[dict]:
        self.require("calendar")
        data = await self.get(f"{CAL}/users/me/calendarList", {"maxResults": 250})
        return [
            {
                "id": c["id"],
                "summary": c.get("summaryOverride") or c.get("summary"),
                "primary": c.get("primary", False),
                "time_zone": c.get("timeZone"),
                "access_role": c.get("accessRole"),
            }
            for c in data.get("items", [])
        ]

    async def _calendar_tz(self, calendar_id: str) -> str:
        try:
            cal = await self.get(f"{CAL}/calendars/{quote(calendar_id, safe='')}")
            return cal.get("timeZone") or "UTC"
        except GoogleError:
            return "UTC"

    async def calendar_events(self, calendar_id, time_min, time_max, query, max_results) -> dict:
        self.require("calendar")
        tz = await self._calendar_tz(calendar_id)
        tmin = extract.to_rfc3339(time_min, tz) if time_min else datetime.now(timezone.utc).isoformat()
        tmax = (
            extract.to_rfc3339(time_max, tz, end_of_day=True)
            if time_max
            else (datetime.fromisoformat(tmin) + timedelta(days=14)).isoformat()
        )
        params = {
            "timeMin": tmin,
            "timeMax": tmax,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": max_results,
        }
        if query:
            params["q"] = query
        data = await self.get(f"{CAL}/calendars/{quote(calendar_id, safe='')}/events", params)
        return {
            "calendar_time_zone": tz,
            "time_min": tmin,
            "time_max": tmax,
            "events": [extract.event_summary(e, desc_chars=300) for e in data.get("items", [])],
        }

    async def calendar_event(self, calendar_id: str, event_id: str) -> dict:
        self.require("calendar")
        extract.check_id(event_id, "event_id")
        e = await self.get(f"{CAL}/calendars/{quote(calendar_id, safe='')}/events/{event_id}")
        return extract.event_summary(e)

    # ── Contacts ──
    async def contacts_search(self, query: str, max_results: int) -> list[dict]:
        if not (self.has("contacts") or self.has("other_contacts")):
            raise MissingScope("contacts")
        results: list[dict] = []
        size = min(max_results, 30)
        if self.has("contacts"):
            mask = "names,emailAddresses,phoneNumbers,organizations"
            if self.user_id not in _people_warmed:  # People API asks for a warm-up call before searching
                await self.get(f"{PEOPLE}/people:searchContacts", {"query": "", "readMask": mask})
                _people_warmed.add(self.user_id)
            data = await self.get(
                f"{PEOPLE}/people:searchContacts", {"query": query, "readMask": mask, "pageSize": size}
            )
            results += [extract.person_summary(r["person"], "contacts") for r in data.get("results", [])]
        if self.has("other_contacts"):
            data = await self.get(
                f"{PEOPLE}/otherContacts:search",
                {"query": query, "readMask": "names,emailAddresses,phoneNumbers", "pageSize": size},
            )
            results += [extract.person_summary(r["person"], "other_contacts") for r in data.get("results", [])]
        return results[:max_results]

    # ── Tasks ──
    async def task_lists(self) -> list[dict]:
        self.require("tasks")
        data = await self.get(f"{TASKS}/users/@me/lists", {"maxResults": 100})
        return [{"id": t["id"], "title": t.get("title"), "updated": t.get("updated")} for t in data.get("items", [])]

    async def tasks(self, tasklist_id: str, show_completed: bool, max_results: int) -> list[dict]:
        self.require("tasks")
        if tasklist_id != "@default":
            extract.check_id(tasklist_id, "tasklist_id")
        flag = "true" if show_completed else "false"
        data = await self.get(
            f"{TASKS}/lists/{tasklist_id}/tasks",
            {"showCompleted": flag, "showHidden": flag, "maxResults": max_results},
        )
        out = []
        for t in data.get("items", []):
            notes = t.get("notes") or ""
            item = {
                "id": t["id"],
                "title": t.get("title"),
                "status": t.get("status"),
                "due": t.get("due"),
                "completed": t.get("completed"),
                "parent": t.get("parent"),
                "notes": notes[:500] + ("…" if len(notes) > 500 else ""),
                "link": t.get("webViewLink"),
            }
            out.append({k: v for k, v in item.items() if v not in (None, "")})
        return out
