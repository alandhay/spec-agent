"""Looking after the people who use the hosted workbench: who they are, what they have spent, and
inviting, crediting and suspending them. Used by the admin page (static/admin.html) through server.py.

Each person's folder (DATA/<their Cognito id>) holds:
    person.json    their address, when they were first and last seen, whether they are suspended
    account.json   what they have been granted and what they have spent (see server.Workspace)
    log.jsonl      every request, top-up and grant, with its cost
Invitations and the Cognito side of suspending go through the user pool; on your own machine there
is no pool, and the one person ("local") is shown with what can be known without it.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

PERSON_ID = re.compile(r"[0-9a-f-]{36}|local")
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
MAX_GRANT = 1000.0
SEEN_EVERY_S = 300                      # how often "last seen" is written while someone is active

_locks: dict[str, threading.RLock] = {}
_locks_lock = threading.Lock()


def lock_for(home: Path) -> threading.RLock:
    """One lock per person's folder, shared by everything that changes their account or record."""
    with _locks_lock:
        return _locks.setdefault(str(home), threading.RLock())


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def person(home: Path) -> dict:
    return _read(home / "person.json", {})


def update_person(home: Path, **changes) -> dict:
    with lock_for(home):
        record = {**person(home), **changes}
        (home / "person.json").write_text(json.dumps(record), encoding="utf-8")
        return record


def seen(home: Path, email: str) -> None:
    """Note that someone used the workbench (not more often than every few minutes)."""
    record, now = person(home), time.time()
    if record.get("email") != email or now - record.get("last", 0) > SEEN_EVERY_S:
        update_person(home, email=email, first=record.get("first", now), last=now)


def suspended(home: Path) -> bool:
    return bool(person(home).get("suspended"))


def log_tail(home: Path, count: int = 50) -> list[dict]:
    try:
        lines = (home / "log.jsonl").read_text(encoding="utf-8").splitlines()[-count:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out[::-1]


def summary(home: Path, account: dict | None) -> dict:
    """One person as the admin page lists them."""
    record = person(home)
    requests = [e for e in log_tail(home, 100000) if "request" in e]
    projects = home / "projects"
    return {"id": home.name, "email": record.get("email"), "first": record.get("first"), "last": record.get("last"),
            "suspended": bool(record.get("suspended")), "account": account,
            "requests": len(requests), "last_request": requests[0].get("at") if requests else None,
            "projects": sum(1 for p in projects.iterdir() if p.is_dir()) if projects.is_dir() else 0}


class Pool:
    """The Cognito user pool, for the parts of this that need it."""

    def __init__(self, pool_id: str, client=None) -> None:
        self.pool_id, self._client = pool_id, client

    def _cognito(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("cognito-idp", region_name=self.pool_id.split("_")[0])
        return self._client

    def users(self) -> dict[str, dict]:
        """Everyone in the pool by their id: address, whether they can sign in, their state, when invited."""
        out = {}
        for page in self._cognito().get_paginator("list_users").paginate(UserPoolId=self.pool_id):
            for user in page["Users"]:
                attributes = {a["Name"]: a["Value"] for a in user.get("Attributes", [])}
                created = user.get("UserCreateDate")
                out[attributes.get("sub", user["Username"])] = {
                    "email": attributes.get("email"), "enabled": user.get("Enabled", True),
                    "status": user.get("UserStatus"), "invited": created.timestamp() if hasattr(created, "timestamp") else None}
        return out

    def invite(self, email: str) -> None:
        self._cognito().admin_create_user(
            UserPoolId=self.pool_id, Username=email, DesiredDeliveryMediums=["EMAIL"],
            UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}])

    def enable(self, user_id: str, on: bool) -> None:
        call = self._cognito().admin_enable_user if on else self._cognito().admin_disable_user
        call(UserPoolId=self.pool_id, Username=user_id)


def people(data: Path, pool: Pool | None, account) -> list[dict]:
    """Everyone with a folder here, and everyone invited who has not signed in yet; most recently seen first.

    ``account(home)`` gives a person's budget (or None where there are none).
    """
    listed = {}
    if data.is_dir():
        for home in data.iterdir():
            if home.is_dir() and PERSON_ID.fullmatch(home.name):
                listed[home.name] = summary(home, account(home))
    if pool is not None:
        for user_id, user in pool.users().items():
            entry = listed.setdefault(user_id, {"id": user_id, "email": user["email"], "first": None, "last": None,
                                                "suspended": False, "account": None, "requests": 0,
                                                "last_request": None, "projects": 0})
            entry["email"] = entry["email"] or user["email"]
            entry.update(enabled=user["enabled"], status=user["status"], invited=user["invited"])
    return sorted(listed.values(), key=lambda p: p.get("last") or p.get("invited") or 0, reverse=True)
