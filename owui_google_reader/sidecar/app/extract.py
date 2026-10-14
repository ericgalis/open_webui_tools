"""Pure helpers: Drive query building, MIME/HTML/PDF/Sheet text extraction, paging."""

import base64
import csv
import io
import re
from datetime import datetime, timedelta
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

GOOGLE_DOC = "application/vnd.google-apps.document"
GOOGLE_SHEET = "application/vnd.google-apps.spreadsheet"
GOOGLE_SLIDES = "application/vnd.google-apps.presentation"
GOOGLE_FOLDER = "application/vnd.google-apps.folder"

FILE_TYPES = {
    "document": GOOGLE_DOC,
    "spreadsheet": GOOGLE_SHEET,
    "presentation": GOOGLE_SLIDES,
    "folder": GOOGLE_FOLDER,
    "pdf": "application/pdf",
}

TEXTUAL_MIMES = {
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-yaml",
    "application/yaml",
    "application/csv",
    "application/x-sh",
}

ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,1024}$")


def check_id(value: str, what: str = "id") -> str:
    if not ID_RE.fullmatch(value or ""):
        raise ValueError(f"invalid {what}: {value!r}")
    return value


def friendly_type(mime: str) -> str:
    for name, m in FILE_TYPES.items():
        if m == mime:
            return name
    return mime


def drive_query(text: str | None, file_type: str | None = None, modified_after: str | None = None) -> str:
    parts = ["trashed = false"]
    if text:
        esc = text.replace("\\", "\\\\").replace("'", "\\'")
        parts.append(f"(name contains '{esc}' or fullText contains '{esc}')")
    if file_type:
        mime = FILE_TYPES.get(file_type)
        if not mime:
            raise ValueError(f"file_type must be one of {sorted(FILE_TYPES)}")
        parts.append(f"mimeType = '{mime}'")
    if modified_after:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", modified_after):
            modified_after += "T00:00:00"
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?", modified_after):
            raise ValueError("modified_after must be YYYY-MM-DD or RFC3339")
        parts.append(f"modifiedTime > '{modified_after}'")
    return " and ".join(parts)


def page_text(text: str, offset: int, max_chars: int) -> dict:
    chunk = text[offset : offset + max_chars]
    end = offset + len(chunk)
    return {
        "content": chunk,
        "offset": offset,
        "total_chars": len(text),
        "next_offset": end if end < len(text) else None,
    }


def b64url_decode(data: str, charset: str = "utf-8") -> str:
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


class _HTMLText(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "table", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "pre"}
    SKIP = {"script", "style", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")
            if tag == "li":
                self.out.append("- ")
        elif tag in ("td", "th"):
            self.out.append(" | ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    parser = _HTMLText()
    parser.feed(html)
    parser.close()
    text = "".join(parser.out)
    text = re.sub(r"[ \t\r\f\v ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def headers_dict(headers: list[dict]) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in headers or []}


def parse_gmail_payload(payload: dict) -> tuple[str, list[dict]]:
    """Return (body_text, attachments) from a Gmail API message payload (format=full)."""
    plain: list[str] = []
    html: list[str] = []
    attachments: list[dict] = []

    def walk(part: dict):
        mime = part.get("mimeType", "")
        body = part.get("body") or {}
        if part.get("filename"):
            attachments.append({"filename": part["filename"], "mime_type": mime, "size": body.get("size", 0)})
            return
        if body.get("data"):
            ctype = headers_dict(part.get("headers", [])).get("content-type", "")
            m = re.search(r'charset="?([\w\-]+)', ctype, re.I)
            text = b64url_decode(body["data"], m.group(1) if m else "utf-8")
            if mime == "text/plain":
                plain.append(text)
            elif mime == "text/html":
                html.append(text)
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    body = "\n\n".join(plain) if plain else html_to_text("\n".join(html))
    return body.strip(), attachments


def format_gmail_message(msg: dict) -> str:
    h = headers_dict(msg.get("payload", {}).get("headers", []))
    body, attachments = parse_gmail_payload(msg.get("payload", {}))
    lines = [f"Message-Id: {msg.get('id')}"]
    for key in ("from", "to", "cc", "date", "subject"):
        if h.get(key):
            lines.append(f"{key.capitalize()}: {h[key]}")
    if attachments:
        lines.append(
            "Attachments: " + ", ".join(f"{a['filename']} ({a['mime_type']}, {a['size']} bytes)" for a in attachments)
        )
    return "\n".join(lines) + "\n\n" + body


def sheet_to_text(title: str, rows: list[list]) -> str:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return f"## Sheet: {title}\n{buf.getvalue()}"


def pdf_to_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join(
        f"--- page {i} ---\n{(page.extract_text() or '').strip()}" for i, page in enumerate(reader.pages, 1)
    )


def to_rfc3339(value: str, tz: str, end_of_day: bool = False) -> str:
    """Parse a date or datetime; naive values are interpreted in the calendar's time zone."""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        dt = datetime.fromisoformat(value)
        if end_of_day:
            dt += timedelta(days=1)
    else:
        dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz))
    return dt.isoformat()


def _when(t: dict | None) -> str | None:
    if not t:
        return None
    return t.get("dateTime") or t.get("date")


def event_summary(e: dict, desc_chars: int | None = None) -> dict:
    desc = e.get("description") or ""
    if "<" in desc:
        desc = html_to_text(desc)
    if desc_chars is not None and len(desc) > desc_chars:
        desc = desc[:desc_chars] + "…"
    out = {
        "id": e.get("id"),
        "summary": e.get("summary"),
        "start": _when(e.get("start")),
        "end": _when(e.get("end")),
        "location": e.get("location"),
        "status": e.get("status"),
        "organizer": (e.get("organizer") or {}).get("email"),
        "attendees": [f"{a.get('email')} ({a.get('responseStatus')})" for a in e.get("attendees", [])],
        "meet_link": e.get("hangoutLink"),
        "link": e.get("htmlLink"),
        "description": desc or None,
    }
    if e.get("recurringEventId"):
        out["recurring_event_id"] = e["recurringEventId"]
    return {k: v for k, v in out.items() if v not in (None, [], "")}


def person_summary(p: dict, source: str) -> dict:
    out = {
        "source": source,
        "name": next((n.get("displayName") for n in p.get("names", [])), None),
        "emails": [e.get("value") for e in p.get("emailAddresses", [])],
        "phones": [ph.get("value") for ph in p.get("phoneNumbers", [])],
        "organizations": [
            " — ".join(x for x in (o.get("name"), o.get("title")) if x) for o in p.get("organizations", [])
        ],
    }
    return {k: v for k, v in out.items() if v not in (None, [], "")}
