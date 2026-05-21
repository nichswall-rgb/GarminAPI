import os
import threading

from garminconnect import Garmin

from .config import settings

_lock = threading.Lock()
_client: Garmin | None = None

# Holds an in-progress login that is waiting for an MFA code.
# Only relevant during the one-time bootstrap.
_pending: dict = {}


class LoginRequired(Exception):
    """No usable Garmin session — call /garmin/login."""


def _ensure_token_dir() -> str:
    os.makedirs(settings.garmin_token_dir, exist_ok=True)
    return settings.garmin_token_dir


def get_client() -> Garmin:
    """Return a logged-in client, loading persisted tokens if needed."""
    global _client
    with _lock:
        if _client is not None:
            return _client
        tokendir = _ensure_token_dir()
        g = Garmin()
        try:
            g.login(tokendir)  # loads + refreshes persisted tokens
        except Exception as exc:  # noqa: BLE001 - any failure means re-auth
            raise LoginRequired(str(exc))
        _client = g
        return _client


def start_login(email: str, password: str) -> dict:
    """Begin login. Returns {'mfa_required': bool}."""
    global _client
    g = Garmin(email=email, password=password, return_on_mfa=True)
    result1, result2 = g.login()
    if result1 == "needs_mfa":
        _pending["client"] = g
        _pending["state"] = result2
        return {"mfa_required": True}
    g.garth.dump(_ensure_token_dir())
    with _lock:
        _client = g
    return {"mfa_required": False}


def resume_login(mfa_code: str) -> dict:
    """Complete a login that required an MFA code."""
    global _client
    g = _pending.get("client")
    state = _pending.get("state")
    if g is None:
        raise RuntimeError("No login awaiting MFA. Call /garmin/login first.")
    g.resume_login(mfa_code, state)
    g.garth.dump(_ensure_token_dir())
    with _lock:
        _client = g
    _pending.clear()
    return {"mfa_required": False, "logged_in": True}


def has_tokens() -> bool:
    d = settings.garmin_token_dir
    return os.path.isdir(d) and any(os.scandir(d))
