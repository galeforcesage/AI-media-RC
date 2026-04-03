"""
auth.py
Authentication and session management.

Three tiers:
  1. App-level: shared password, 2-week cookie
  2. Admin-level: username/password, session-only cookie
  3. MCP-level: server uses its own credentials (no browser creds)

Passwords stored as PBKDF2-SHA256 hashes.
Cookies are HMAC-signed tokens.
"""

from __future__ import annotations
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Password hashing (PBKDF2-SHA256, stdlib only)
# ------------------------------------------------------------------

_ITERATIONS = 260_000
_SALT_LEN = 16


def hash_password(password: str) -> str:
    """Hash a password with PBKDF2-SHA256. Returns 'pbkdf2:salt_hex:hash_hex'."""
    salt = os.urandom(_SALT_LEN)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2:{salt.hex()}:{dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a password against a stored PBKDF2 hash."""
    try:
        _, salt_hex, hash_hex = stored_hash.split(":")
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
        return hmac.compare_digest(dk, expected)
    except (ValueError, AttributeError):
        return False


# ------------------------------------------------------------------
# Token generation & signing
# ------------------------------------------------------------------

def _sign_token(payload: str, secret: str) -> str:
    """Create an HMAC-signed token: payload.signature."""
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _verify_token(token: str, secret: str) -> Optional[str]:
    """Verify and return payload, or None if invalid."""
    try:
        payload, sig = token.rsplit(".", 1)
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, expected):
            return payload
        return None
    except (ValueError, AttributeError):
        return None


# ------------------------------------------------------------------
# Auth Manager
# ------------------------------------------------------------------

APP_COOKIE = "app_session"
ADMIN_COOKIE = "admin_session"
APP_MAX_AGE = 14 * 24 * 3600  # 2 weeks


class AuthManager:
    """Manages authentication state, config loading, and token validation."""

    def __init__(self, config_path: str = "auth.json"):
        self._config_path = config_path
        self._secret: str = ""
        self._app_hash: str = ""
        self._admin_users: Dict[str, str] = {}  # username -> hash
        self._admin_sessions: Dict[str, float] = {}  # token_id -> expiry_ts
        self._load_config()

    def _load_config(self) -> None:
        """Load or create auth config."""
        if os.path.exists(self._config_path):
            with open(self._config_path) as f:
                data = json.load(f)
            self._secret = data.get("secret_key", "")
            self._app_hash = data.get("app_password_hash", "")
            self._admin_users = data.get("admin_users", {})
            logger.info("Auth config loaded: %d admin users", len(self._admin_users))
        else:
            logger.warning("No auth config at %s — generating defaults", self._config_path)
            self._secret = secrets.token_hex(32)
            self._app_hash = hash_password("ai-media-rc")
            self._admin_users = {"admin": hash_password("admin")}
            self._save_config()
            logger.info("Auth config created with default passwords — CHANGE THEM via setup-auth.sh")

    def _save_config(self) -> None:
        """Persist auth config to disk."""
        data = {
            "secret_key": self._secret,
            "app_password_hash": self._app_hash,
            "admin_users": self._admin_users,
        }
        with open(self._config_path, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(self._config_path, 0o600)

    # --- App auth ---

    def verify_app_password(self, password: str) -> bool:
        return verify_password(password, self._app_hash)

    def create_app_token(self) -> str:
        """Create a signed app session token (2-week validity encoded in token)."""
        expires = int(time.time()) + APP_MAX_AGE
        payload = f"app:{expires}:{secrets.token_urlsafe(16)}"
        return _sign_token(payload, self._secret)

    def validate_app_token(self, token: str) -> bool:
        """Validate an app session token."""
        payload = _verify_token(token, self._secret)
        if not payload:
            return False
        try:
            parts = payload.split(":")
            if parts[0] != "app":
                return False
            expires = int(parts[1])
            return time.time() < expires
        except (IndexError, ValueError):
            return False

    # --- Admin auth ---

    def verify_admin(self, username: str, password: str) -> bool:
        stored = self._admin_users.get(username)
        if not stored:
            return False
        return verify_password(password, stored)

    def create_admin_token(self, username: str) -> str:
        """Create a short-lived admin session token (4-hour validity)."""
        token_id = secrets.token_urlsafe(24)
        expires = time.time() + 4 * 3600  # 4 hours
        self._admin_sessions[token_id] = expires
        self._cleanup_expired()
        payload = f"admin:{username}:{token_id}"
        return _sign_token(payload, self._secret)

    def validate_admin_token(self, token: str) -> Optional[str]:
        """Validate admin token. Returns username if valid, None otherwise."""
        payload = _verify_token(token, self._secret)
        if not payload:
            return None
        try:
            parts = payload.split(":")
            if parts[0] != "admin":
                return None
            username = parts[1]
            token_id = parts[2]
            expires = self._admin_sessions.get(token_id)
            if not expires or time.time() > expires:
                self._admin_sessions.pop(token_id, None)
                return None
            return username
        except (IndexError, ValueError):
            return None

    def revoke_admin_token(self, token: str) -> None:
        """Explicitly revoke an admin session."""
        payload = _verify_token(token, self._secret)
        if payload:
            try:
                token_id = payload.split(":")[2]
                self._admin_sessions.pop(token_id, None)
            except IndexError:
                pass

    def _cleanup_expired(self) -> None:
        """Remove expired admin sessions."""
        now = time.time()
        expired = [k for k, v in self._admin_sessions.items() if now > v]
        for k in expired:
            del self._admin_sessions[k]

    # --- Password management ---

    def set_app_password(self, new_password: str) -> None:
        self._app_hash = hash_password(new_password)
        self._save_config()

    def set_admin_password(self, username: str, new_password: str) -> None:
        self._admin_users[username] = hash_password(new_password)
        self._save_config()
