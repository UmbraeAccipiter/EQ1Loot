"""Reader settings, kept in a small JSON file next to the program."""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, field

_LOG_NAME = re.compile(r"^eqlog_([^_]+)_(.+)\.txt$", re.IGNORECASE)


def default_path() -> str:
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "reader_settings.json")


def character_of(log_name: str) -> str:
    """eqlog_<Character>_<server>.txt -> Character ('' if the name doesn't fit)."""
    m = _LOG_NAME.match(os.path.basename(log_name))
    return m.group(1) if m else ""


@dataclass
class Settings:
    server: str = ""
    guild_key: str = ""
    eq_folder: str = ""
    ticked_logs: list = field(default_factory=list)
    starting_as: str = ""
    reader_id: str = ""
    path: str = ""

    # -- load / save ------------------------------------------------------
    @classmethod
    def load(cls, path: str | None = None) -> "Settings":
        path = path or default_path()
        s = cls(path=path)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            for k in ("server", "guild_key", "eq_folder", "starting_as", "reader_id"):
                if isinstance(data.get(k), str):
                    setattr(s, k, data[k])
            if isinstance(data.get("ticked_logs"), list):
                s.ticked_logs = [x for x in data["ticked_logs"] if isinstance(x, str)]
        except (OSError, ValueError):
            pass
        if not s.reader_id:
            s.reader_id = uuid.uuid4().hex
            s.save()
        return s

    def save(self) -> None:
        data = {k: getattr(self, k) for k in
                ("server", "guild_key", "eq_folder", "ticked_logs", "starting_as", "reader_id")}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    # -- derived ----------------------------------------------------------
    def is_complete(self) -> bool:
        return bool(self.server.strip() and self.guild_key and self.eq_folder)

    def base_url(self) -> str:
        s = self.server.strip().rstrip("/")
        if s.lower().startswith(("http://", "https://")):
            return s
        return "https://" + s

    def logs_dir(self) -> str:
        return os.path.join(self.eq_folder, "Logs")

    def list_logs(self) -> list[tuple[str, int, float]]:
        """(file name, size in bytes, modified time) for every eqlog_*.txt in Logs."""
        out = []
        for p in glob.glob(os.path.join(self.logs_dir(), "eqlog_*.txt")):
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append((os.path.basename(p), st.st_size, st.st_mtime))
        out.sort(key=lambda t: t[0].lower())
        return out

    def characters(self) -> list[str]:
        seen, out = set(), []
        for name in self.ticked_logs:
            c = character_of(name)
            if c and c not in seen:
                seen.add(c)
                out.append(c)
        return out

    def newest_dump(self) -> str | None:
        files = glob.glob(os.path.join(self.eq_folder, "RaidRoster_*.txt"))
        if not files:
            return None
        return max(files, key=lambda p: (os.path.getmtime(p), p))
