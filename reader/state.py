"""Crash-safe reader state: log positions, unsent loot, unsent rolls, raid code.

The file is written to a temporary name and then renamed over the real one, so a
crash in the middle of a write can never leave a half-written state file.
"""
from __future__ import annotations

import json
import os


class State:
    def __init__(self, path: str):
        self.path = path
        self.positions: dict[str, int] = {}
        self.loot: list[dict] = []
        self.rolls: list[dict] = []
        self.raid_code: str = ""
        self.raid_started: float = 0.0     # server clock
        self.next_id: int = 1

    def load(self) -> "State":
        try:
            os.remove(self.path + ".tmp")          # leftover from a crash mid-write
        except OSError:
            pass
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.positions = {str(k): int(v) for k, v in d.get("positions", {}).items()}
            self.loot = [x for x in d.get("loot", []) if isinstance(x, dict)]
            self.rolls = [x for x in d.get("rolls", []) if isinstance(x, dict)]
            self.raid_code = str(d.get("raid_code", ""))
            self.raid_started = float(d.get("raid_started", 0.0))
            self.next_id = int(d.get("next_id", 1))
            ids = [int(x.get("id", 0)) for x in self.loot]
            if ids:
                self.next_id = max(self.next_id, max(ids) + 1)
        except (OSError, ValueError, TypeError, AttributeError):
            self.__init__(self.path)                # unreadable: start fresh
        return self

    def save(self) -> None:
        data = {"positions": self.positions, "loot": self.loot, "rolls": self.rolls,
                "raid_code": self.raid_code, "raid_started": self.raid_started,
                "next_id": self.next_id}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
