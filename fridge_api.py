"""Supabase-backed data layer for FridgeIntel.

Deliberately free of Flet imports so it can be exercised on its own.

Every quantity change goes through a Postgres function (see supabase/schema.sql)
rather than a read-modify-write from Python. With four roommates on four phones,
reading a quantity here and writing it back would silently lose whichever edit
landed second.
"""

import contextlib
import json
import os
import threading

import httpx

import config

# How long any single request may take. Phones on flaky wifi need a real
# timeout rather than hanging the UI thread forever.
TIMEOUT = 20.0


class ApiError(Exception):
    """A message already fit to show the user."""


def _session_file() -> str:
    return os.path.join(os.getenv("FLET_APP_STORAGE_DATA", "."), "session.json")


class FridgeApi:
    """Talks to Supabase over REST. One instance per running app."""

    def __init__(self, url: str | None = None, anon_key: str | None = None):
        self.url = (url or config.SUPABASE_URL).rstrip("/")
        self.anon_key = anon_key or config.SUPABASE_ANON_KEY
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.email: str | None = None
        self._lock = threading.Lock()
        self._client = httpx.Client(timeout=TIMEOUT)

    # ----- plumbing ----------------------------------------------------

    def _headers(self, authed: bool = True) -> dict:
        token = self.access_token if (authed and self.access_token) else self.anon_key
        return {
            "apikey": self.anon_key,
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _explain(response: httpx.Response) -> str:
        """Turn a Supabase error body into something worth showing a roommate."""
        try:
            body = response.json()
        except Exception:
            return f"Request failed ({response.status_code})."
        for key in ("message", "error_description", "msg", "error", "hint"):
            value = body.get(key) if isinstance(body, dict) else None
            if value:
                # Postgres RAISE EXCEPTION text arrives here verbatim.
                return str(value)
        return f"Request failed ({response.status_code})."

    def _request(self, method: str, path: str, *, authed=True, retry=True, **kw):
        try:
            r = self._client.request(
                method, self.url + path, headers=self._headers(authed), **kw
            )
        except httpx.RequestError as e:
            raise ApiError(f"Cannot reach the fridge server ({type(e).__name__}).")

        # An expired access token is normal: refresh once, then replay.
        if r.status_code == 401 and authed and retry and self.refresh_token:
            if self._refresh():
                return self._request(method, path, authed=authed, retry=False, **kw)

        if r.status_code >= 400:
            raise ApiError(self._explain(r))
        return r

    def _rpc(self, name: str, payload: dict | None = None):
        r = self._request("POST", f"/rest/v1/rpc/{name}", json=payload or {})
        return r.json() if r.content else None

    # ----- auth --------------------------------------------------------

    def _adopt(self, data: dict) -> None:
        self.access_token = data.get("access_token")
        self.refresh_token = data.get("refresh_token")
        self.email = (data.get("user") or {}).get("email")
        self._save_session()

    def sign_up(self, email: str, password: str) -> None:
        email, password = email.strip(), password or ""
        if not email or "@" not in email:
            raise ApiError("Enter a valid email address.")
        if len(password) < 6:
            raise ApiError("Password must be at least 6 characters.")
        r = self._request(
            "POST", "/auth/v1/signup", authed=False,
            json={"email": email, "password": password},
        )
        data = r.json()
        if not data.get("access_token"):
            raise ApiError("Account created. Check your email to confirm it, then log in.")
        self._adopt(data)

    def sign_in(self, email: str, password: str) -> None:
        if not (email or "").strip() or not password:
            raise ApiError("Enter your email and password.")
        r = self._request(
            "POST", "/auth/v1/token?grant_type=password", authed=False,
            json={"email": email.strip(), "password": password},
        )
        self._adopt(r.json())

    def _refresh(self) -> bool:
        try:
            r = self._client.post(
                self.url + "/auth/v1/token?grant_type=refresh_token",
                headers=self._headers(authed=False),
                json={"refresh_token": self.refresh_token},
            )
        except httpx.RequestError:
            return False
        if r.status_code >= 400:
            self.sign_out()
            return False
        self._adopt(r.json())
        return True

    def sign_out(self) -> None:
        self.access_token = self.refresh_token = self.email = None
        with contextlib.suppress(OSError):
            os.remove(_session_file())

    # ----- staying logged in across launches ---------------------------

    def _save_session(self) -> None:
        if not self.refresh_token:
            return
        with contextlib.suppress(OSError):
            with open(_session_file(), "w") as fh:
                json.dump(
                    {"refresh_token": self.refresh_token, "email": self.email}, fh
                )

    def restore_session(self) -> bool:
        """Resume a previous login. Returns True if the app can skip the login screen."""
        try:
            with open(_session_file()) as fh:
                saved = json.load(fh)
        except (OSError, ValueError):
            return False
        self.refresh_token = saved.get("refresh_token")
        self.email = saved.get("email")
        return bool(self.refresh_token) and self._refresh()

    @property
    def signed_in(self) -> bool:
        return bool(self.access_token)

    # ----- household ---------------------------------------------------

    def whoami(self) -> dict:
        return self._rpc("whoami") or {}

    def create_household(self, name: str, display_name: str) -> str:
        if not (name or "").strip():
            raise ApiError("Give your household a name.")
        if not (display_name or "").strip():
            raise ApiError("Enter your own name.")
        return self._rpc(
            "create_household",
            {"name": name.strip(), "display_name": display_name.strip()},
        )

    def join_household(self, code: str, display_name: str) -> None:
        code = (code or "").strip()
        if len(code) != 36 or code.count("-") != 4:
            raise ApiError("That does not look like a join code.")
        if not (display_name or "").strip():
            raise ApiError("Enter your own name.")
        self._rpc(
            "join_household", {"code": code, "display_name": display_name.strip()}
        )

    # ----- the fridge --------------------------------------------------

    def fridge_state(self) -> dict:
        """Capacity, items and history in one round trip."""
        state = self._rpc("fridge_state") or {}
        return {
            "capacity": state.get("capacity") or 0,
            "items": state.get("items") or [],
            "history": state.get("history") or [],
        }

    def save_items(self, entries: list[dict]) -> None:
        """Commit staged items. Capacity is enforced by Postgres, not here."""
        if not entries:
            raise ApiError("Nothing to save yet.")
        self._rpc("save_items", {"entries": entries})

    def adjust_item(self, item_id: str, delta: int) -> None:
        self._rpc("adjust_item", {"item_id": item_id, "delta": delta})

    def remove_item(self, item_id: str) -> None:
        self._request(
            "DELETE", f"/rest/v1/items?id=eq.{item_id}"
        )
