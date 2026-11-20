# EQ1 Loot Reader

A small Windows program that helps run loot distribution on guild off-night raids in
EverQuest. It reads the EverQuest log files on your own PC and sends the few lines that
matter (`/random 1000` rolls and loot lines such as "X has looted Y from Z") to the guild's
loot server, which shows them on a web page.

## What it reads and sends

- It only follows the log files you tick in Settings. It starts at the end of each file and
  never reads old history.
- It looks for two kinds of lines: roll results from `/random 1000` (range 0 to 1000) and
  loot lines. Everything else, including chat and tells, is thrown away on your PC and
  never leaves it.
- Loot is sent only when you press **Send loot**. Rolls are sent automatically during a raid.
- A raid dump (the `RaidRoster_*.txt` file EverQuest writes) is sent only when you press
  **Send raid dump**.
- There is no guild key or server address in the code. You type them into Settings.

## Download

Get the latest `EQLootReader.zip` from the
[Releases page](https://github.com/UmbraeAccipiter/EQ1Loot/releases/latest), unzip it and run
`EQLootReader.exe`. No installer or admin rights are needed.

The program is not code-signed, so Windows may say "Windows protected your PC". Click
**More info**, then **Run anyway**.

## Building it yourself

The reader is plain Python (standard library only) with a tkinter window. The workflow in
`.github/workflows/build-reader.yml` builds the exe with PyInstaller on a Windows machine at
GitHub whenever a version tag such as `v0.1.0` is pushed, and attaches it to a Release.

## License

Copyright (C) 2026 UmbraeAccipiter. Licensed under the
[GNU General Public License v3.0](LICENSE): you may use, study, change and share this program,
but anyone who shares a copy or a modified version must give recipients the full source under
the same license.
