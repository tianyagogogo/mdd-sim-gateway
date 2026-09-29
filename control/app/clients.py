"""Signed-in client apps: native clients that hold a long-lived bearer token.

A browser keeps its session in a cookie, which is the right shape for a browser and the wrong
one for a native client: it has no cookie jar to rely on, its sign-in has to outlive twelve hours,
and the administrator needs to see what is signed in and throw one of them off. So a client signs
in once with the administrator's credentials, is recorded here, and is given an opaque token it
presents as ``Authorization: Bearer``. What a client may do with it is decided in authz.py.

("Client", not "device": ``/api/devices`` is the gateway's modem hardware.)

Only the token's digest is stored, for the same reason sessions store only theirs: the file must
not be a list of usable credentials. A token slides forward as it is used and expires after
TOKEN_TTL without use. Revoking the client or changing the administrator password ends it at once.

Lookups are in memory. A client woken by a push notification has seconds to reach the gateway,
so checking its token must not read a file or derive a key; the file is written only when a
client is added or removed, or when its last use has moved on by LAST_SEEN_SKEW.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time

from . import config as cfg

CLIENTS_PATH = os.path.join(cfg.DATA_DIR, "clients.json")
# "mdd_c1_": recognisable in a log or a secret scanner, and versioned so the format can change.
TOKEN_PREFIX = "mdd_c1_"
# Long enough that a phone signed in months ago still works, short enough that one forgotten in
# a drawer stops being a credential.
TOKEN_TTL = 90 * 24 * 60 * 60
# Writing "last used" on every request would rewrite the file continuously on an SD card; a
# client's activity is not interesting at a finer grain than this.
LAST_SEEN_SKEW = 5 * 60
# Every sign-in adds a record, and a record only goes when it is revoked or expires. Beyond this
# many, the one unused for longest makes room -- a phone reinstalled a few times, not a person.
MAX_CLIENTS = 20
PLATFORMS = ("ios", "android", "other")
NAME_MAX = 64
VERSION_MAX = 32

_lock = threading.RLock()
# id -> record, and the token digest -> id index the gate looks tokens up in.
_clients: dict[int, dict] = {}
_by_digest: dict[str, int] = {}
_next_id = 1


class ClientError(ValueError):
    """A refused client operation whose message is safe to show the caller."""


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _save() -> None:
    """Persist every client. Callers hold the lock."""
    payload = {"version": 1, "next_id": _next_id,
               "clients": sorted(_clients.values(), key=lambda item: item["id"])}
    os.makedirs(cfg.DATA_DIR, exist_ok=True)
    temporary = CLIENTS_PATH + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.chmod(temporary, 0o600)
    os.replace(temporary, CLIENTS_PATH)


def _load() -> None:
    global _next_id
    try:
        with open(CLIENTS_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        data = {}
    stored = data.get("clients") if isinstance(data, dict) else None
    records: dict[int, dict] = {}
    for item in stored if isinstance(stored, list) else []:
        try:
            record = {"id": int(item["id"]), "name": str(item.get("name") or ""),
                      "platform": str(item.get("platform") or "other"),
                      "app_version": str(item.get("app_version") or ""),
                      "token_hash": str(item["token_hash"]),
                      "created_at": int(item.get("created_at") or 0),
                      "last_seen": int(item.get("last_seen") or 0),
                      "expires_at": int(item["expires_at"])}
        except (KeyError, TypeError, ValueError):
            continue
        records[record["id"]] = record
    with _lock:
        _clients.clear()
        _clients.update(records)
        _by_digest.clear()
        _by_digest.update({record["token_hash"]: cid for cid, record in records.items()})
        try:
            _next_id = max(int(data.get("next_id") or 1), max(records, default=0) + 1)
        except (AttributeError, TypeError, ValueError):
            _next_id = max(records, default=0) + 1


def public(record: dict) -> dict:
    """What the API shows about a client: never its token or the token's digest."""
    return {key: record[key] for key in ("id", "name", "platform", "app_version", "created_at",
                                         "last_seen", "expires_at")}


def register(name: str = "", platform: str = "other", app_version: str = "") -> tuple[dict, str]:
    """Record a newly signed-in client and return it with its token, shown this once only."""
    global _next_id
    platform = str(platform or "other").strip().lower()
    if platform not in PLATFORMS:
        raise ClientError("unknown platform")
    name = str(name or "").strip()[:NAME_MAX] or {"ios": "iPhone", "android": "Android"}.get(
        platform, "Client")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    now = int(time.time())
    with _lock:
        record = {"id": _next_id, "name": name, "platform": platform,
                  "app_version": str(app_version or "").strip()[:VERSION_MAX],
                  "token_hash": _digest(token), "created_at": now, "last_seen": now,
                  "expires_at": now + TOKEN_TTL}
        _clients[record["id"]] = record
        _by_digest[record["token_hash"]] = record["id"]
        _next_id += 1
        _save()
        return public(record), token


def resolve(token: str | None) -> dict | None:
    """The client this token belongs to, sliding its expiry forward; None if it is not valid."""
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    now = int(time.time())
    with _lock:
        # A dict lookup by digest: the digest of a guess reveals nothing about stored ones.
        client_id = _by_digest.get(_digest(token))
        record = _clients.get(client_id) if client_id is not None else None
        if record is None:
            return None
        if record["expires_at"] <= now:
            _forget(client_id)
            _save_quietly()
            return None
        if now - record["last_seen"] >= LAST_SEEN_SKEW:
            record["last_seen"] = now
            record["expires_at"] = now + TOKEN_TTL
            _save_quietly()
        return dict(record)


def _forget(client_id: int) -> dict | None:
    record = _clients.pop(int(client_id), None)
    if record is not None:
        _by_digest.pop(record["token_hash"], None)
    return record


def _save_quietly() -> None:
    try:
        _save()
    except OSError:
        # Losing a "last used" stamp is not a reason to refuse the request that is happening.
        pass


def _prune() -> list[int]:
    """Forget expired records. Callers hold the lock and save."""
    now = int(time.time())
    expired = [cid for cid, record in _clients.items() if record["expires_at"] <= now]
    for cid in expired:
        _forget(cid)
    return expired


def make_room() -> list[int]:
    """Before a sign-in: drop expired records, then the least recently used beyond the cap.

    Returns the ids removed, so the caller can close sockets a still-live one had open."""
    with _lock:
        removed = _prune()
        while len(_clients) >= MAX_CLIENTS:
            oldest = min(_clients.values(), key=lambda r: (r["last_seen"], r["id"]))
            _forget(oldest["id"])
            removed.append(oldest["id"])
        if removed:
            _save()
        return removed


def list_clients() -> list[dict]:
    with _lock:
        if _prune():
            _save_quietly()
        return [public(record) for record in sorted(_clients.values(), key=lambda r: r["id"])]


def revoke(client_id: int) -> bool:
    """Sign one client out. Its token stops working on the next request."""
    with _lock:
        if _forget(client_id) is None:
            return False
        _save()
        return True


def revoke_all() -> int:
    """Sign every client out: the administrator password changed."""
    with _lock:
        removed = len(_clients)
        if removed:
            _clients.clear()
            _by_digest.clear()
            _save()
        return removed


_load()
