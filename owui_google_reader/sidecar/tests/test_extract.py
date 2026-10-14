import base64

from app import extract


def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def test_drive_query_escapes_and_filters():
    q = extract.drive_query("bob's \\ plan", "document", "2026-01-02")
    assert "name contains 'bob\\'s \\\\ plan'" in q
    assert "mimeType = 'application/vnd.google-apps.document'" in q
    assert "modifiedTime > '2026-01-02T00:00:00'" in q
    assert extract.drive_query("") == "trashed = false"


def test_drive_query_rejects_bad_input():
    for kwargs in ({"file_type": "exe"}, {"modified_after": "yesterday' or 1=1"}):
        try:
            extract.drive_query("x", **kwargs)
        except ValueError:
            continue
        raise AssertionError(kwargs)


def test_check_id():
    assert extract.check_id("1AbC_d-9") == "1AbC_d-9"
    for bad in ("../v1/status", "a/b", "", "a b"):
        try:
            extract.check_id(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_gmail_prefers_plain_and_lists_attachments():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "multipart/alternative", "parts": [
                {"mimeType": "text/plain", "body": {"data": b64("héllo plain")}},
                {"mimeType": "text/html", "body": {"data": b64("<p>hello html</p>")}},
            ]},
            {"mimeType": "application/pdf", "filename": "a.pdf", "body": {"attachmentId": "x", "size": 10}},
        ],
    }
    body, att = extract.parse_gmail_payload(payload)
    assert body == "héllo plain"
    assert att == [{"filename": "a.pdf", "mime_type": "application/pdf", "size": 10}]


def test_gmail_html_only_and_charset():
    html = "<html><head><style>x{}</style></head><body><p>Hi&nbsp;there</p><ul><li>one</li></ul><script>bad()</script></body></html>"
    payload = {"mimeType": "text/html", "headers": [{"name": "Content-Type", "value": 'text/html; charset="iso-8859-1"'}],
               "body": {"data": base64.urlsafe_b64encode(html.encode("latin-1")).decode()}}
    body, _ = extract.parse_gmail_payload(payload)
    assert "Hi there" in body and "- one" in body and "bad()" not in body and "x{}" not in body


def test_page_text():
    p = extract.page_text("abcdef", 2, 3)
    assert p["content"] == "cde" and p["next_offset"] == 5 and p["total_chars"] == 6
    assert extract.page_text("abc", 0, 10)["next_offset"] is None


def test_to_rfc3339():
    assert extract.to_rfc3339("2026-10-14", "America/New_York") == "2026-10-14T00:00:00-04:00"
    assert extract.to_rfc3339("2026-10-14", "UTC", end_of_day=True) == "2026-10-15T00:00:00+00:00"
    assert extract.to_rfc3339("2026-10-14T09:30:00Z", "America/New_York") == "2026-10-14T09:30:00+00:00"


def test_sheet_and_event():
    assert extract.sheet_to_text("S 1", [["a", "b,c"], ["1"]]) == '## Sheet: S 1\na,"b,c"\r\n1\r\n'
    e = extract.event_summary({"id": "e1", "summary": "Sync", "start": {"date": "2026-10-14"},
                               "description": "<b>agenda</b> " + "x" * 400,
                               "attendees": [{"email": "a@x.com", "responseStatus": "accepted"}]}, desc_chars=20)
    assert e["start"] == "2026-10-14" and e["attendees"] == ["a@x.com (accepted)"]
    assert e["description"].startswith("agenda") and e["description"].endswith("…")
