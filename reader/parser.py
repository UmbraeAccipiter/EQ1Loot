"""Parsers for EQ log lines and raid dump files.

Only three things matter: loot lines, roll lines (0 to 1000 only) and the raid
dump. Every other log line is ignored and returns None.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

_STAMP = r"\[(?P<stamp>\w{3} \w{3} +\d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] "
_STAMP_FMT = "%a %b %d %H:%M:%S %Y"

_ROLL = re.compile(
    _STAMP + r"\*\*A Magic Die is rolled by (?P<name>\S+)\. "
    r"It could have been any number from (?P<lo>\d+) to (?P<hi>\d+), "
    r"but this time it turned up a (?P<value>\d+)\.\s*$")

_LOOT = re.compile(
    _STAMP + r"--(?:(?P<name>\S+) has|You have) looted (?P<what>.+?) "
    r"from (?P<corpse>.+?)\s*\.--\s*$")

_QTY = re.compile(r"^(?P<qty>\d+) (?P<item>.+)$")


@dataclass(frozen=True)
class Roll:
    character: str
    value: int
    time: datetime


@dataclass(frozen=True)
class Loot:
    looter: str | None      # None = the log's own character ("You have looted")
    item: str
    quantity: int
    corpse: str
    time: datetime          # kept for the reader's own use; never part of the identity

    @property
    def identity(self) -> tuple[str | None, str, str]:
        """(looter, item, corpse): no time, per DESIGN.md section 6."""
        return (self.looter, self.item, self.corpse)


@dataclass(frozen=True)
class DumpEntry:
    group: int              # 0 = in the raid but not in a group
    name: str
    level: int
    cls: str
    role: str               # "Raid Leader", "Group Leader" or ""


def _stamp(text: str) -> datetime:
    return datetime.strptime(" ".join(text.split()), _STAMP_FMT)


def parse_roll(line: str) -> Roll | None:
    """A roll counts only when its range is exactly 0 to 1000."""
    m = _ROLL.match(line)
    if not m or int(m["lo"]) != 0 or int(m["hi"]) != 1000:
        return None
    return Roll(m["name"], int(m["value"]), _stamp(m["stamp"]))


def parse_loot(line: str) -> Loot | None:
    m = _LOOT.match(line)
    if not m:
        return None
    what = m["what"]
    qty = 1
    q = _QTY.match(what)
    if q:
        qty, what = int(q["qty"]), q["item"]
    elif what.startswith("an "):
        what = what[3:]
    elif what.startswith("a "):
        what = what[2:]
    return Loot(m["name"], what, qty, m["corpse"].strip(), _stamp(m["stamp"]))


def parse_dump(text: str) -> list[DumpEntry]:
    """Tab-separated: group, name, level, class, role, then blank columns."""
    out: list[DumpEntry] = []
    for raw in text.splitlines():
        cols = raw.split("\t")
        if len(cols) < 4 or not cols[1].strip():
            continue
        try:
            group, level = int(cols[0]), int(cols[2])
        except ValueError:
            continue
        role = cols[4].strip() if len(cols) > 4 else ""
        out.append(DumpEntry(group, cols[1].strip(), level, cols[3].strip(), role))
    return out


def read_dump(path: str) -> list[DumpEntry]:
    with open(path, encoding="utf-8-sig", errors="replace", newline=None) as f:
        return parse_dump(f.read())
