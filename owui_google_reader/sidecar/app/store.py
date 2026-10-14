"""SQLite storage. Refresh tokens are Fernet-encrypted at rest; the key only exists in this container's env."""

import json
import secrets
import sqlite3
import threading
import time

from cryptography.fernet import Fernet


class Store:
    def __init__(self, path: str, key: str):
        self.fernet = Fernet(key.encode())
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                user_id TEXT PRIMARY KEY, email TEXT, scopes TEXT, refresh_token BLOB, linked_at INTEGER);
            CREATE TABLE IF NOT EXISTS link_tokens (
                token TEXT PRIMARY KEY, user_id TEXT, expires_at INTEGER);
            CREATE TABLE IF NOT EXISTS oauth_states (
                state TEXT PRIMARY KEY, link_token TEXT, verifier BLOB, expires_at INTEGER);
            """
        )

    def _purge(self):
        now = int(time.time())
        self.db.execute("DELETE FROM link_tokens WHERE expires_at < ?", (now,))
        self.db.execute("DELETE FROM oauth_states WHERE expires_at < ?", (now,))

    # ── accounts ──
    def get_account(self, user_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT email, scopes, refresh_token, linked_at FROM accounts WHERE user_id = ?", (user_id,)
            ).fetchone()
        if not row:
            return None
        return {
            "email": row[0],
            "scopes": json.loads(row[1]),
            "refresh_token": self.fernet.decrypt(row[2]).decode(),
            "linked_at": row[3],
        }

    def save_account(self, user_id: str, email: str, scopes: list[str], refresh_token: str):
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO accounts VALUES (?, ?, ?, ?, ?)",
                (user_id, email, json.dumps(scopes), self.fernet.encrypt(refresh_token.encode()), int(time.time())),
            )

    def delete_account(self, user_id: str):
        with self.lock, self.db:
            self.db.execute("DELETE FROM accounts WHERE user_id = ?", (user_id,))

    # ── link tokens (one-time browser links bound to an Open WebUI user) ──
    def new_link_token(self, user_id: str, ttl: int) -> str:
        token = secrets.token_urlsafe(32)
        with self.lock, self.db:
            self._purge()
            self.db.execute("INSERT INTO link_tokens VALUES (?, ?, ?)", (token, user_id, int(time.time()) + ttl))
        return token

    def link_user(self, token: str) -> str | None:
        with self.lock:
            row = self.db.execute(
                "SELECT user_id FROM link_tokens WHERE token = ? AND expires_at >= ?", (token, int(time.time()))
            ).fetchone()
        return row[0] if row else None

    # ── OAuth state + PKCE verifier ──
    def new_state(self, link_token: str, verifier: str, ttl: int) -> str:
        state = secrets.token_urlsafe(32)
        with self.lock, self.db:
            self._purge()
            self.db.execute(
                "INSERT INTO oauth_states VALUES (?, ?, ?, ?)",
                (state, link_token, self.fernet.encrypt(verifier.encode()), int(time.time()) + ttl),
            )
        return state

    def pop_state(self, state: str) -> tuple[str, str] | None:
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT link_token, verifier FROM oauth_states WHERE state = ? AND expires_at >= ?",
                (state, int(time.time())),
            ).fetchone()
            self.db.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
        if not row:
            return None
        return row[0], self.fernet.decrypt(row[1]).decode()
