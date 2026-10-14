# Google Workspace Reader for Open WebUI

Read-only access for Open WebUI models to each user's own Google **Drive (Docs, Sheets, Slides,
PDFs, text), Gmail, Calendar, Contacts and Tasks**.

```
 Open WebUI container                     google-reader container (this repo)
 ┌──────────────────────────┐  dmz net   ┌───────────────────────────────────────┐
 │ Tool: google_workspace_  │──:8000────▶│ API  (not published to host)          │──▶ Google APIs
 │ reader.py                │ API key +  │  - holds client secret + encrypted    │    (GET only,
 │ (no Google creds)        │ OWUI user  │    refresh tokens (SQLite, Fernet)    │    *.readonly
 └──────────────────────────┘    id      │ LINK pages :8001 → host :8766         │    scopes)
                 user's browser ────────▶│  connect / disconnect Google account  │
                                         └───────────────────────────────────────┘
```

* **Credentials never reach the model or Open WebUI.** The client secret, refresh tokens and access
  tokens exist only in the sidecar. The tool only holds a sidecar API key, and responses contain only
  extracted text and metadata.
* **Read-only twice over.** Only `*.readonly` scopes are requested, and the sidecar's Google client
  only issues `GET` requests (the one `POST` goes to the OAuth token endpoint).
* **Per-user.** Each Open WebUI user links their own Google account. Data is keyed by Open WebUI user id.

## Setup

### 1. Google Cloud project (one time)

1. <https://console.cloud.google.com> → create a project.
2. **APIs & Services → Library** → enable: Google Drive API, Google Sheets API, Gmail API,
   Google Calendar API, People API, Google Tasks API.
3. **OAuth consent screen** (Google Auth Platform): User type **External**. Under *Data access* add
   the scopes `drive.readonly`, `gmail.readonly`, `calendar.readonly`, `contacts.readonly`,
   `contacts.other.readonly`, `tasks.readonly`, `openid`, `userinfo.email`.
4. **Audience → Publish app** (status *In production*). **Don't skip this.** In *Testing* status, Google
   expires refresh tokens after 7 days. You don't need to submit for verification: users see an
   "unverified app" warning and click *Advanced → Go to … (unsafe)*. Unverified apps are capped at
   100 users.
5. **Clients → Create client → Application type: Desktop app**. Copy the client ID and secret.

### 2. Sidecar

```bash
cd ~/code/owui_google_reader
cp .env.example .env && chmod 600 .env
python3 -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"  # → TOKEN_ENCRYPTION_KEY
openssl rand -hex 32                                                                       # → SIDECAR_API_KEY
$EDITOR .env      # fill GOOGLE_CLIENT_ID/SECRET, the two keys, check PUBLIC_BASE_URL / LINK_BIND_ADDR
docker compose up -d --build
docker compose logs -f
```

The service joins the existing external `dmz` network as `google-reader`, so Open WebUI reaches it at
`http://google-reader:8000`. Only the link pages are published on the host (`LINK_BIND_ADDR:LINK_PORT`,
default `192.168.4.73:8766`).

Back up the `google-reader-data` volume and `TOKEN_ENCRYPTION_KEY` together. Without the key, the
tokens can't be read, and users just re-link.

### 3. Open WebUI tool

1. **Workspace → Tools → +** and paste `openwebui_tool/google_workspace_reader.py`. Save.
2. Tool **⚙ Valves**: set `SIDECAR_API_KEY` to the same value as in `.env`. Leave `SIDECAR_URL` as is.
3. Control who gets it: set tool access to the users or groups who should have it, then enable it on
   the models you want (Workspace → Models → *model* → Tools), or per chat via the ＋ menu.
   Models with native function calling work best.

### 4. Link an account (each user)

In a chat with the tool enabled: *"Connect my Google account."* The model calls `google_link_account`
and returns a one-time link (valid for 15 minutes) to `http://192.168.4.73:8766/link/…`. On that page:

1. Click **Open Google sign-in** and approve.
2. Google redirects to `http://127.0.0.1:53682/?state=…&code=…`, and that page **fails to load**.
   This is expected (it's the Desktop-app loopback flow, which works without a public hostname).
3. Copy that tab's full URL, paste it into the link page, and click **Finish linking**.

The pasted code is only usable together with the client secret and PKCE verifier, which stay inside
the sidecar. The model never sees it. The same link page has **Disconnect and revoke access**.

*Optional:* if you expose the link pages on a real HTTPS hostname (e.g. via `perimeter_proxy`), use a
*Web application* client with redirect URI `https://<host>/oauth/callback`, and set `PUBLIC_BASE_URL`
and `OAUTH_REDIRECT_URI` accordingly. Linking then completes without the paste step.

## Tools exposed to the model

| Tool | What it does |
|---|---|
| `google_account_status`, `google_link_account` | link state / one-time connect link |
| `drive_search`, `drive_read_file` | name + full-text search; read Docs (markdown), Sheets (CSV per tab), Slides, PDFs, text, folders |
| `gmail_search`, `gmail_read_message`, `gmail_read_thread` | Gmail query syntax; bodies as text, attachment names only |
| `calendar_list_calendars`, `calendar_list_events`, `calendar_get_event` | time-range + text search |
| `contacts_search` | Contacts + "other contacts" |
| `tasks_list_tasklists`, `tasks_list_tasks` | Google Tasks |

Long content is paged with `offset` / `next_offset`.

## Security notes

* **Trust boundary:** whoever holds `SIDECAR_API_KEY` *and* can reach `google-reader:8000` on the `dmz`
  network can read the data of any linked user, because the sidecar trusts the `X-OWUI-User-Id` the tool
  sends. The key lives in Open WebUI's admin-only valves, so only install tools/functions you trust.
  It can't be used to obtain the Google tokens themselves.
* **Prompt injection:** emails and documents are attacker-controllable text. The tool marks content as
  untrusted, and read-only scopes mean an injected instruction can't send mail or modify files. But if the
  same model also has tools that reach the internet (web search, URL fetch, code execution), injected text
  could try to exfiltrate data through them. Consider not enabling this tool alongside those on the same model.
* Link pages are plain HTTP on the LAN by default. Put them behind TLS if the LAN isn't trusted.

## Tests

```bash
docker run --rm -u 0 -v "$PWD/sidecar/tests:/srv/tests:ro" --entrypoint sh google-reader:local \
  -c 'pip install -q pytest==8.3.4 && python -m pytest -q -p no:cacheprovider tests'
```
