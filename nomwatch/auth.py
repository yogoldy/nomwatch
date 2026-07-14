"""Local users, opaque sessions, invitations, and security policy."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .state import LocalState, StateError

IDLE_SECONDS = 12 * 60 * 60
ABSOLUTE_SECONDS = 7 * 24 * 60 * 60
BOOTSTRAP_SECONDS = 15 * 60
INVITATION_SECONDS = 30 * 60
LAST_SEEN_COALESCE_SECONDS = 5 * 60
ROLE_LEVEL = {"viewer": 1, "operator": 2, "owner": 3}


@dataclass(frozen=True)
class IssuedSession:
    token: str
    csrf: str
    user_id: str


class AuthService:
    def __init__(self, state: LocalState, *, clock=time.time, token_bytes=secrets.token_bytes):
        self.state = state
        self.clock = clock
        self.token_bytes = token_bytes
        self.passwords = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, hash_len=32, salt_len=16)
        key_hex = state.secret_get_or_create("session_hmac_key")
        self.digest_key = bytes.fromhex(key_hex)

    @staticmethod
    def normalize_username(value: str) -> str:
        return unicodedata.normalize("NFKC", value).strip().casefold()

    def digest(self, value: str) -> str:
        return hmac.new(self.digest_key, value.encode(), hashlib.sha256).hexdigest()

    def has_users(self) -> bool:
        with self.state.connect() as conn:
            return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    def ensure_bootstrap(self) -> Optional[str]:
        if self.has_users():
            return None
        now = self.clock()
        with self.state.transaction(immediate=True) as conn:
            current = conn.execute(
                "SELECT 1 FROM activation_tokens WHERE purpose='bootstrap' AND consumed_at IS NULL AND expires_at>?",
                (now,),
            ).fetchone()
            if current:
                return None
            raw = secrets.token_urlsafe(24)
            conn.execute(
                "INSERT INTO activation_tokens VALUES (?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, self.digest(raw), "bootstrap", "owner", None,
                 now + BOOTSTRAP_SECONDS, None, now),
            )
            return raw

    def _consume_activation(self, conn, raw: str, purpose: str):
        now = self.clock()
        row = conn.execute(
            "SELECT * FROM activation_tokens WHERE token_digest=? AND purpose=? AND consumed_at IS NULL AND expires_at>?",
            (self.digest(raw), purpose, now),
        ).fetchone()
        if not row:
            raise StateError("invalid or expired activation code")
        conn.execute("UPDATE activation_tokens SET consumed_at=? WHERE id=?", (now, row["id"]))
        return row

    def claim_owner(self, code: str, username: str, display_name: str, password: str) -> IssuedSession:
        username = self.normalize_username(username)
        self._validate_credentials(username, password)
        now = self.clock()
        with self.state.transaction(immediate=True) as conn:
            if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                raise StateError("this installation has already been claimed")
            self._consume_activation(conn, code, "bootstrap")
            user_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO users VALUES (?,?,?,?,?,NULL,1,?,?)",
                (user_id, username, display_name.strip() or username, self.passwords.hash(password), "owner", now, now),
            )
        self.state.audit("owner.claim", "success", actor=user_id, source="loopback")
        return self.create_session(user_id, "loopback")

    @staticmethod
    def _validate_credentials(username: str, password: str) -> None:
        if not username or len(username) > 64 or any(c.isspace() for c in username):
            raise StateError("username must be 1-64 characters without whitespace")
        if len(password) < 12:
            raise StateError("password must contain at least 12 characters")

    def create_invitation(self, role: str, actor_id: str) -> str:
        if role not in ROLE_LEVEL:
            raise StateError("invalid role")
        raw = secrets.token_urlsafe(24)
        now = self.clock()
        with self.state.connect() as conn:
            conn.execute(
                "INSERT INTO activation_tokens VALUES (?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, self.digest(raw), "invitation", role, None,
                 now + INVITATION_SECONDS, None, now),
            )
        self.state.audit("invitation.create", "success", actor=actor_id, detail={"role": role})
        return raw

    def accept_invitation(self, code: str, username: str, display_name: str, password: str,
                          origin_class: str) -> IssuedSession:
        username = self.normalize_username(username)
        self._validate_credentials(username, password)
        now = self.clock()
        with self.state.transaction(immediate=True) as conn:
            activation = self._consume_activation(conn, code, "invitation")
            user_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO users VALUES (?,?,?,?,?,NULL,1,?,?)",
                (user_id, username, display_name.strip() or username, self.passwords.hash(password), activation["role"], now, now),
            )
        self.state.audit("invitation.accept", "success", actor=user_id, source=origin_class)
        return self.create_session(user_id, origin_class)

    def _rate_limited(self, username: str, source: str) -> bool:
        cutoff = self.clock() - 5 * 60
        with self.state.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM login_attempts WHERE username=? AND source=? AND success=0 AND attempted_at>?",
                (username, source, cutoff),
            ).fetchone()[0]
        return count >= 5

    def login(self, username: str, password: str, source: str, origin_class: str) -> IssuedSession:
        username = self.normalize_username(username)
        if self._rate_limited(username, source):
            self.state.audit("auth.login", "rate_limited", source=origin_class)
            raise StateError("too many login attempts; wait five minutes")
        with self.state.connect() as conn:
            user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        ok = False
        if user and user["disabled_at"] is None:
            try:
                ok = self.passwords.verify(user["password_hash"], password)
            except (VerifyMismatchError, InvalidHashError):
                ok = False
        with self.state.connect() as conn:
            conn.execute(
                "INSERT INTO login_attempts(username,source,attempted_at,success) VALUES (?,?,?,?)",
                (username, source, self.clock(), int(ok)),
            )
        if not ok:
            self.state.audit("auth.login", "failure", source=origin_class)
            raise StateError("invalid username or password")
        if self.passwords.check_needs_rehash(user["password_hash"]):
            with self.state.connect() as conn:
                conn.execute("UPDATE users SET password_hash=?,updated_at=? WHERE id=?", (self.passwords.hash(password), self.clock(), user["id"]))
        self.state.audit("auth.login", "success", actor=user["id"], source=origin_class)
        return self.create_session(user["id"], origin_class)

    def create_session(self, user_id: str, origin_class: str) -> IssuedSession:
        token = self.token_bytes(32).hex()
        csrf = self.token_bytes(32).hex()
        now = self.clock()
        session_id = uuid.uuid4().hex
        with self.state.connect() as conn:
            user = conn.execute("SELECT session_version FROM users WHERE id=?", (user_id,)).fetchone()
            if not user:
                raise StateError("unknown user")
            conn.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (session_id, self.digest(token), self.digest(csrf), user_id, user[0], origin_class,
                 now, now, now + IDLE_SECONDS, now + ABSOLUTE_SECONDS, now, None, "{}"),
            )
        return IssuedSession(token, csrf, user_id)

    def authenticate(self, token: str, *, origin_class: Optional[str] = None, touch: bool = True):
        now = self.clock()
        with self.state.transaction() as conn:
            row = conn.execute(
                "SELECT s.*,u.username,u.display_name,u.role,u.disabled_at,u.session_version FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_digest=?",
                (self.digest(token),),
            ).fetchone()
            if not row or row["revoked_at"] is not None or row["disabled_at"] is not None:
                return None
            if row["idle_expires_at"] <= now or row["absolute_expires_at"] <= now:
                conn.execute("UPDATE sessions SET revoked_at=? WHERE id=?", (now, row["id"]))
                return None
            if row["user_session_version"] != row["session_version"]:
                conn.execute("UPDATE sessions SET revoked_at=? WHERE id=?", (now, row["id"]))
                return None
            if origin_class is not None and row["origin_class"] != origin_class:
                return None
            if touch and now - row["last_seen"] >= LAST_SEEN_COALESCE_SECONDS:
                conn.execute(
                    "UPDATE sessions SET last_seen=?,idle_expires_at=? WHERE id=?",
                    (now, min(now + IDLE_SECONDS, row["absolute_expires_at"]), row["id"]),
                )
        return dict(row)

    def verify_csrf(self, session, raw_csrf: str) -> bool:
        return bool(raw_csrf) and hmac.compare_digest(session["csrf_digest"], self.digest(raw_csrf))

    def revoke(self, session_id: str, actor_id: str) -> bool:
        with self.state.connect() as conn:
            cur = conn.execute("UPDATE sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (self.clock(), session_id))
        self.state.audit("session.revoke", "success" if cur.rowcount else "not_found", actor=actor_id, object_id=session_id)
        return bool(cur.rowcount)

    def reauthenticate(self, session_id: str, password: str) -> None:
        with self.state.connect() as conn:
            row = conn.execute("SELECT s.user_id,u.password_hash FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.id=?", (session_id,)).fetchone()
            if not row:
                raise StateError("session not found")
            try:
                ok = self.passwords.verify(row["password_hash"], password)
            except (VerifyMismatchError, InvalidHashError):
                ok = False
            if not ok:
                raise StateError("password is incorrect")
            conn.execute("UPDATE sessions SET reauthenticated_at=? WHERE id=?", (self.clock(), session_id))

    def recent_reauth(self, session, seconds: int = 600) -> bool:
        return bool(session.get("reauthenticated_at") and session["reauthenticated_at"] >= self.clock() - seconds)

    def list_sessions(self, user_id: Optional[str] = None) -> list[dict]:
        query = "SELECT id,user_id,origin_class,issued_at,last_seen,idle_expires_at,absolute_expires_at,revoked_at FROM sessions"
        args = ()
        if user_id:
            query += " WHERE user_id=?"
            args = (user_id,)
        with self.state.connect() as conn:
            return [dict(row) for row in conn.execute(query, args).fetchall()]

    def list_users(self) -> list[dict]:
        with self.state.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT id,username,display_name,role,disabled_at,created_at,updated_at FROM users ORDER BY username"
            ).fetchall()]

    def require_role(self, session, role: str) -> bool:
        return ROLE_LEVEL.get(session["role"], 0) >= ROLE_LEVEL[role]

    def update_user(self, user_id: str, *, role: Optional[str] = None, disabled: Optional[bool] = None) -> None:
        if role is not None and role not in ROLE_LEVEL:
            raise StateError("invalid role")
        now = self.clock()
        with self.state.transaction(immediate=True) as conn:
            user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not user:
                raise StateError("user not found")
            new_role = role or user["role"]
            new_disabled = now if disabled else (None if disabled is False else user["disabled_at"])
            if user["role"] == "owner" and (new_role != "owner" or new_disabled is not None):
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM users WHERE role='owner' AND disabled_at IS NULL AND id<>?", (user_id,)
                ).fetchone()[0]
                if remaining == 0:
                    raise StateError("the last enabled owner cannot be disabled or demoted")
            conn.execute(
                "UPDATE users SET role=?,disabled_at=?,session_version=session_version+1,updated_at=? WHERE id=?",
                (new_role, new_disabled, now, user_id),
            )
