"""Talks to the loot server (DESIGN.md section 19) and runs the reader's logic.

Client       - plain HTTPS/JSON calls and the clock offset.
ReaderSession - everything the reader does that is not drawing a window: it
                follows the ticked logs, collects rolls and loot, and sends them.

Wire format (the server must follow this; section 19 fixes the calls, this
file fixes the field names). Every request body also carries
"reader_id" and "guild_key"; the guild key is also sent as the X-Guild-Key header.

  POST /api/checkin      {version, characters, raid_code}
        -> {server_time, update_available, open_raid_code, raid}
           raid = null or {code, status: "active"|"closed", started_by,
                           started_at, last_dump: {by, time, names} | null}
  POST /api/raid/start   {starting_as, force}
        -> {status: "ok", raid} or {status: "warning", summary: {...}}
  POST /api/raid/join    {code}      -> {raid}
  POST /api/raid/leave   {code}      -> {}
  POST /api/raid/restart {code}      -> {raid}
  POST /api/rolls        {raid_code, rolls: [{character, value, time}]}
  POST /api/loot         {raid_code, events: [{looter, item, corpse, time, qty}]}
  POST /api/dump         {raid_code, filename, entries: [{group, name, level, cls, role}]}

All times are epoch seconds on the SERVER's clock. The reader measures its clock
offset at every check-in and applies it to log times before sending.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

from parser import parse_loot, parse_roll, read_dump
from settings import Settings, character_of
from state import State
from tailer import LogTailer

VERSION = "0.1.0"
ROLL_LOG_MAX = 200
SAVE_EVERY = 2.0


class ClientError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def log_epoch(dt: datetime) -> float:
    """A log time stamp is local time on this PC."""
    return time.mktime(dt.timetuple())


class Client:
    def __init__(self, base_url: str, guild_key: str, reader_id: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.guild_key = guild_key
        self.reader_id = reader_id
        self.timeout = timeout
        self.offset = 0.0           # server clock minus this PC's clock

    def _post(self, path: str, body: dict) -> dict:
        body = dict(body, reader_id=self.reader_id, guild_key=self.guild_key)
        req = urllib.request.Request(
            self.base_url + path, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "X-Guild-Key": self.guild_key})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            msg = ""
            try:
                msg = json.loads(e.read().decode("utf-8", "replace")).get("error", "")
            except Exception:
                pass
            raise ClientError(msg or f"Server answered {e.code}", e.code) from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ClientError(f"Can't reach {self.base_url}") from None
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            raise ClientError("The server sent something unreadable") from None

    def to_server(self, local_epoch: float) -> float:
        return local_epoch + self.offset

    # -- calls ------------------------------------------------------------
    def checkin(self, raid_code: str, characters: list[str]) -> dict:
        t0 = time.time()
        r = self._post("/api/checkin", {"version": VERSION, "characters": characters,
                                        "raid_code": raid_code})
        t1 = time.time()
        if "server_time" in r:
            self.offset = float(r["server_time"]) - (t0 + t1) / 2.0
        return r

    def raid_start(self, starting_as: str, force: bool = False) -> dict:
        return self._post("/api/raid/start", {"starting_as": starting_as, "force": force})

    def raid_join(self, code: str) -> dict:
        return self._post("/api/raid/join", {"code": code})

    def raid_leave(self, code: str) -> dict:
        return self._post("/api/raid/leave", {"code": code})

    def raid_restart(self, code: str) -> dict:
        return self._post("/api/raid/restart", {"code": code})

    def send_rolls(self, code: str, rolls: list[dict]) -> dict:
        return self._post("/api/rolls", {"raid_code": code, "rolls": rolls})

    def send_loot(self, code: str, events: list[dict]) -> dict:
        return self._post("/api/loot", {"raid_code": code, "events": events})

    def send_dump(self, code: str, filename: str, entries: list[dict]) -> dict:
        return self._post("/api/dump", {"raid_code": code, "filename": filename,
                                        "entries": entries})


class ReaderSession:
    def __init__(self, settings: Settings, state: State, client: Client):
        self.settings = settings
        self.state = state
        self.client = client
        self.lock = threading.RLock()
        self.connected = False
        self.last_error = ""
        self.update_available = False
        self.raid: dict | None = None
        self.open_raid_code = ""
        self.roll_log: list[str] = []
        self._checked_in = False
        self._seen: set = set()
        self._last_save = 0.0
        self.tailer = LogTailer(settings.logs_dir(), settings.ticked_logs, state.positions)
        for r in state.rolls:
            self._seen.add(("r", r["character"], r["value"], r["time"]))
        for x in state.loot:
            self._seen.add(("l", x["looter"], x["item"], x["corpse"], x["qty"], x["time"]))

    # -- settings changed -------------------------------------------------
    def reload_logs(self) -> None:
        self.tailer = LogTailer(self.settings.logs_dir(), self.settings.ticked_logs,
                                self.state.positions)

    # -- views for the window ---------------------------------------------
    def mode(self) -> str:
        """none | in_raid | closed | different"""
        code = self.state.raid_code
        if not code:
            return "none"
        if not self._checked_in or (self.raid or {}).get("status", "closed") == "active":
            return "in_raid"
        if self.open_raid_code and self.open_raid_code != code:
            return "different"
        return "closed"

    def collecting(self) -> bool:
        return self.mode() == "in_raid"

    def waiting(self) -> int:
        with self.lock:
            return sum(1 for x in self.state.loot if x.get("checked", True))

    def loot_items(self) -> list[dict]:
        with self.lock:
            return [dict(x) for x in self.state.loot]

    def set_checked(self, item_id: int, checked: bool) -> None:
        with self.lock:
            for x in self.state.loot:
                if x["id"] == item_id:
                    x["checked"] = bool(checked)
            self._save(force=True)

    # -- reading the logs -------------------------------------------------
    def _raid_start_local(self) -> float:
        if not self.state.raid_started:
            return 0.0
        return math.floor(self.state.raid_started - self.client.offset)

    def handle_line(self, char: str, line: str) -> None:
        if not self.collecting():
            return
        roll = parse_roll(line)
        if roll:
            ep = log_epoch(roll.time)
            if ep < self._raid_start_local():
                return
            key = ("r", roll.character, roll.value, ep)
            with self.lock:
                if key in self._seen:
                    return
                self._seen.add(key)
                self.state.rolls.append({"character": roll.character,
                                         "value": roll.value, "time": ep})
                self.roll_log.append(f"{roll.time:%H:%M:%S}  {roll.character} {roll.value}")
                del self.roll_log[:-ROLL_LOG_MAX]
            return
        loot = parse_loot(line)
        if loot:
            ep = log_epoch(loot.time)
            if ep < self._raid_start_local():
                return
            looter = loot.looter or char
            key = ("l", looter, loot.item, loot.corpse, loot.quantity, ep)
            with self.lock:
                if key in self._seen:                  # same event in two of our logs
                    return
                self._seen.add(key)
                self.state.loot.append({"id": self.state.next_id, "looter": looter,
                                        "item": loot.item, "corpse": loot.corpse,
                                        "qty": loot.quantity, "time": ep, "checked": True})
                self.state.next_id += 1

    def poll_logs(self) -> int:
        lines = self.tailer.poll()
        for char, line in lines:
            self.handle_line(char, line)
        if self.tailer.changed or lines:
            self._save()
        return len(lines)

    def _save(self, force: bool = False) -> None:
        now = time.time()
        if force or now - self._last_save >= SAVE_EVERY:
            with self.lock:
                self.state.save()
            self.tailer.changed = False
            self._last_save = now

    def save_now(self) -> None:
        self._save(force=True)

    # -- network ----------------------------------------------------------
    def checkin(self) -> bool:
        try:
            r = self.client.checkin(self.state.raid_code, self.settings.characters())
        except ClientError as e:
            self.connected, self.last_error = False, str(e)
            return False
        self.connected, self.last_error = True, ""
        self._checked_in = True
        self.update_available = bool(r.get("update_available"))
        self.open_raid_code = r.get("open_raid_code") or ""
        self.raid = r.get("raid")
        if self.raid and self.raid.get("started_at"):
            self.state.raid_started = float(self.raid["started_at"])
        self.flush_rolls()
        self._save(force=True)
        return True

    def flush_rolls(self) -> bool:
        with self.lock:
            batch = list(self.state.rolls)
        if not batch or not self.state.raid_code or not self.connected:
            return True
        try:
            self.client.send_rolls(self.state.raid_code, [
                {"character": r["character"], "value": r["value"],
                 "time": self.client.to_server(r["time"])} for r in batch])
        except ClientError as e:
            self.last_error = str(e)
            return False
        with self.lock:
            sent = {(r["character"], r["value"], r["time"]) for r in batch}
            self.state.rolls = [r for r in self.state.rolls
                                if (r["character"], r["value"], r["time"]) not in sent]
        self._save(force=True)
        return True

    def send_loot(self) -> tuple[bool, str]:
        """Send every ticked item, then clear the list (unticked items are dropped)."""
        if not self.state.raid_code:
            return False, "Not in a raid."
        with self.lock:
            snapshot = [dict(x) for x in self.state.loot]
        ticked = [x for x in snapshot if x.get("checked", True)]
        if ticked:
            try:
                self.client.send_loot(self.state.raid_code, [
                    {"looter": x["looter"], "item": x["item"], "corpse": x["corpse"],
                     "time": self.client.to_server(x["time"]), "qty": x["qty"]}
                    for x in ticked])
            except ClientError as e:
                self.last_error = str(e)
                return False, str(e)
        gone = {x["id"] for x in snapshot}
        with self.lock:
            self.state.loot = [x for x in self.state.loot if x["id"] not in gone]
        self._save(force=True)
        return True, f"Sent {len(ticked)} item(s)."

    def clear_loot(self) -> int:
        """Throw away the waiting loot without sending it (junk before a boss).
        Rolls, the raid and the log positions are kept. The "seen" memory is kept too, so a
        copy of a cleared line arriving later from another of our logs is still ignored."""
        with self.lock:
            n = len(self.state.loot)
            self.state.loot = []
        self._save(force=True)
        return n

    def send_dump(self) -> tuple[bool, str]:
        if not self.state.raid_code:
            return False, "Not in a raid."
        path = self.settings.newest_dump()
        if not path:
            return False, "No RaidRoster file found in the EverQuest folder."
        entries = read_dump(path)
        if not entries:
            return False, "The raid dump file has no names in it."
        try:
            self.client.send_dump(self.state.raid_code, os.path.basename(path), [
                {"group": e.group, "name": e.name, "level": e.level, "cls": e.cls,
                 "role": e.role} for e in entries])
        except ClientError as e:
            self.last_error = str(e)
            return False, str(e)
        return True, f"Sent {len(entries)} names."

    def _clear_held(self) -> None:
        with self.lock:
            self.state.loot = []
            self.state.rolls = []
            self._seen = set()

    def _enter_raid(self, raid: dict) -> None:
        with self.lock:
            if self.state.raid_code != raid["code"]:
                self._clear_held()
            self.state.raid_code = raid["code"]
            self.state.raid_started = float(raid.get("started_at") or 0.0)
            self.raid = raid
            self._checked_in = True
        self._save(force=True)

    def start_raid(self, starting_as: str, force: bool = False) -> dict:
        r = self.client.raid_start(starting_as, force)
        if r.get("status") == "warning":
            return r
        self._enter_raid(r["raid"])
        self.settings.starting_as = starting_as
        self.settings.save()
        self.checkin()              # measures the clock offset before any line is judged
        return r

    def join_raid(self, code: str) -> dict:
        r = self.client.raid_join(code.strip().upper())
        self._enter_raid(r["raid"])
        self.checkin()
        return r

    def restart_raid(self) -> dict:
        r = self.client.raid_restart(self.state.raid_code)
        self._enter_raid(r["raid"])
        self.checkin()
        return r

    def leave_raid(self) -> None:
        code = self.state.raid_code
        try:
            if code:
                self.client.raid_leave(code)
        except ClientError:
            pass                                   # leaving must always work locally
        self.discard_waiting()

    def discard_waiting(self) -> None:
        self._clear_held()
        with self.lock:
            self.state.raid_code = ""
            self.state.raid_started = 0.0
            self.raid = None
        self._save(force=True)

    def characters(self) -> list[str]:
        return self.settings.characters()
