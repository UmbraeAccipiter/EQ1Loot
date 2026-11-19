"""Follows the ticked EverQuest logs from their end, one new line at a time.

A log is never read from the top: the first time a file is watched the position
is set to its current end. Positions live in the State so a restart or crash
carries on from where it left off. A file that is now smaller than its saved
position was replaced, so it also starts again at its end.
"""
from __future__ import annotations

import os

from settings import character_of

CHUNK = 4 * 1024 * 1024        # most bytes read from one log per poll


class LogTailer:
    def __init__(self, logs_dir: str, names: list[str], positions: dict[str, int]):
        self.logs_dir = logs_dir
        self.names = list(names)
        self.positions = positions
        self.changed = False          # positions moved since the caller last saved

    def poll(self) -> list[tuple[str, str]]:
        """New complete lines as (character, line)."""
        out: list[tuple[str, str]] = []
        for name in self.names:
            char = character_of(name)
            if not char:
                continue
            path = os.path.join(self.logs_dir, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            pos = self.positions.get(name)
            if pos is None or size < pos:
                self.positions[name] = size           # start (or restart) at the end
                self.changed = True
                continue
            if size == pos:
                continue
            try:
                with open(path, "rb") as f:
                    f.seek(pos)
                    data = f.read(min(size - pos, CHUNK))
            except OSError:
                continue
            end = data.rfind(b"\n")
            if end < 0:
                if len(data) >= CHUNK:                # a huge unterminated run: skip it
                    self.positions[name] = pos + len(data)
                    self.changed = True
                continue                              # partial line: wait for the rest
            self.positions[name] = pos + end + 1
            self.changed = True
            text = data[:end + 1].decode("cp1252", errors="replace")
            for line in text.splitlines():
                if line:
                    out.append((char, line))
        return out
