"""EQ Loot Reader: the tkinter windows. All the logic is in client.py (ReaderSession)."""
from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
import webbrowser
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

from client import VERSION, Client, ClientError, ReaderSession
from settings import Settings, default_path
from state import State

TICK_MS = 1000
CHECKIN_SECS = 30


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return ""


class ScrollFrame(ttk.Frame):
    def __init__(self, parent, height=110):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, height=height, highlightthickness=0)
        bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.inner.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")

    def clear(self):
        for w in self.inner.winfo_children():
            w.destroy()


class Collapsible(ttk.Frame):
    """A heading that opens and closes its body. Closed by default."""

    def __init__(self, parent, title: str):
        super().__init__(parent)
        self.title = title
        self.open = False
        self.button = ttk.Button(self, text="", command=self.toggle)
        self.button.pack(fill="x")
        self.body = ttk.Frame(self)
        self.set_title(title)

    def set_title(self, title: str):
        self.title = title
        self.button.configure(text=("v  " if self.open else ">  ") + title)

    def toggle(self):
        self.open = not self.open
        if self.open:
            self.body.pack(fill="both", expand=True)
        else:
            self.body.pack_forget()
        self.set_title(self.title)


class App:
    def __init__(self, root, settings_path: str | None = None):
        self.root = root
        self.settings = Settings.load(settings_path or default_path())
        state_path = os.path.join(os.path.dirname(self.settings.path) or ".",
                                  "reader_state.json")
        self.state = State(state_path).load()
        self.results: queue.Queue = queue.Queue()
        self.busy: set[str] = set()
        self.last_checkin = 0.0
        self._raid_sig = None
        self._loot_sig = None
        self._roll_count = -1
        self.session: ReaderSession | None = None
        self.rebuild_session()

        root.title(f"EQ Loot Reader  {VERSION}")
        root.minsize(420, 0)
        outer = ttk.Frame(root, padding=10)
        outer.pack(fill="both", expand=True)

        top = ttk.Frame(outer)
        top.pack(fill="x")
        self.status = ttk.Label(top, text="Connecting...")
        self.status.pack(side="left")
        self.watching = ttk.Label(top, text="")
        self.watching.pack(side="right")

        self.notice = ttk.Label(outer, text="", foreground="#b45309", wraplength=400)
        self.notice.pack(fill="x", pady=(6, 0))

        self.raid_frame = ttk.Frame(outer)
        self.raid_frame.pack(fill="x", pady=8)

        self.counters = ttk.Label(outer, text="")
        self.counters.pack(fill="x")

        self.loot_box = Collapsible(outer, "Loot seen (0)")
        self.loot_box.pack(fill="x", pady=(6, 0))
        self.loot_scroll = ScrollFrame(self.loot_box.body)
        self.loot_scroll.pack(fill="both", expand=True)

        self.roll_box = Collapsible(outer, "Roll log (0)")
        self.roll_box.pack(fill="x", pady=(4, 0))
        self.roll_text = ttk.Label(self.roll_box.body, text="", justify="left")
        self.roll_text.pack(fill="x")

        foot = ttk.Frame(outer)
        foot.pack(fill="x", pady=(8, 0))
        ttk.Button(foot, text="Settings", command=self.open_settings).pack(side="left")
        self.foot_new = ttk.Button(foot, text="Start a new raid instead",
                                   command=self.start_raid_dialog)
        self.foot_new.pack(side="left", padx=8)

        root.after(200, self.tick)
        if not self.settings.is_complete():
            root.after(300, self.open_settings)

    # ------------------------------------------------------------------
    def rebuild_session(self):
        client = Client(self.settings.base_url() if self.settings.server else "http://invalid",
                        self.settings.guild_key, self.settings.reader_id)
        self.session = ReaderSession(self.settings, self.state, client)
        self.last_checkin = 0.0
        self._raid_sig = None

    def bg(self, name: str, fn, done=None):
        """Run fn on a worker thread; done(result, error) runs on the window thread."""
        if name in self.busy:
            return
        self.busy.add(name)

        def work():
            try:
                res, err = fn(), None
            except Exception as e:                      # shown to the user, never lost
                res, err = None, e
            self.results.put((name, done, res, err))
        threading.Thread(target=work, daemon=True).start()

    def drain(self):
        while True:
            try:
                name, done, res, err = self.results.get_nowait()
            except queue.Empty:
                return
            self.busy.discard(name)
            if done:
                done(res, err)

    def tick(self):
        try:
            self.drain()
            s = self.session
            if self.settings.is_complete():
                s.poll_logs()
                now = time.time()
                if now - self.last_checkin >= CHECKIN_SECS:
                    self.last_checkin = now
                    self.bg("checkin", s.checkin)
                elif s.state.rolls and s.connected:
                    self.bg("rolls", s.flush_rolls)
            self.refresh()
        finally:
            self.root.after(TICK_MS, self.tick)

    # ------------------------------------------------------------------
    def refresh(self):
        s = self.session
        if not self.settings.is_complete():
            self.status.configure(text="Set up the reader in Settings", foreground="#b45309")
        elif s.connected:
            self.status.configure(text="● Connected", foreground="#15803d")
        else:
            self.status.configure(
                text=f"● {s.last_error or 'Connecting...'}, retrying", foreground="#b91c1c")
        self.watching.configure(text=f"Watching {len(self.settings.ticked_logs)} logs")

        notice = ""
        if s.update_available:
            notice = "An update to the reader is available."
        self.notice.configure(text=notice)

        mode = s.mode()
        raid = s.raid or {}
        dump = raid.get("last_dump") or {}
        sig = (mode, s.connected, s.state.raid_code, raid.get("started_by"),
               dump.get("by"), dump.get("time"), dump.get("names"), s.waiting(),
               len(s.loot_items()))
        if sig != self._raid_sig:
            self._raid_sig = sig
            self.render_raid(mode, raid, dump)

        self.counters.configure(
            text=f"Rolls queued: {len(s.state.rolls)}    Loot waiting: {s.waiting()}")

        items = s.loot_items()
        lsig = tuple((x["id"], x["checked"]) for x in items)
        if lsig != self._loot_sig:
            self._loot_sig = lsig
            self.render_loot(items)
        if len(s.roll_log) != self._roll_count:
            self._roll_count = len(s.roll_log)
            self.roll_box.set_title(f"Roll log ({len(s.roll_log)})")
            self.roll_text.configure(text="\n".join(s.roll_log[-30:]))

    def render_loot(self, items):
        self.loot_box.set_title(f"Loot seen ({len(items)})")
        self.loot_scroll.clear()
        for x in items:
            var = tk.BooleanVar(value=x["checked"])
            when = datetime.fromtimestamp(x["time"]).strftime("%H:%M:%S")
            qty = f"{x['qty']} " if x["qty"] > 1 else ""
            text = f"{when}  {x['looter']}: {qty}{x['item']}"
            ttk.Checkbutton(self.loot_scroll.inner, text=text, variable=var,
                            command=lambda i=x["id"], v=var: self.session.set_checked(
                                i, v.get())).pack(anchor="w")

    def render_raid(self, mode, raid, dump):
        for w in self.raid_frame.winfo_children():
            w.destroy()
        s = self.session
        f = self.raid_frame
        can_act = s.connected
        if mode == "in_raid":
            ttk.Label(f, text=f"Raid code  ·  Started by {raid.get('started_by', '?')}"
                      ).pack(anchor="w")
            crow = ttk.Frame(f)
            crow.pack(anchor="w")
            code_lbl = ttk.Label(crow, text=s.state.raid_code, font=("Consolas", 22, "bold"),
                                 cursor="hand2")
            code_lbl.pack(side="left")
            copied = ttk.Label(crow, text="", foreground="#15803d")
            copied.pack(side="left", padx=8)
            code_lbl.bind("<Button-1>", lambda e: self.copy_code(copied))
            row = ttk.Frame(f)
            row.pack(fill="x", pady=4)
            ttk.Button(row, text="Open website", command=self.open_website).pack(side="left")
            ttk.Button(row, text="Send raid dump", command=self.send_dump,
                       state="normal" if can_act else "disabled").pack(side="left", padx=4)
            ttk.Button(row, text="Leave raid", command=self.leave).pack(side="left")
            if dump:
                ttk.Label(f, text=f"Last dump by {dump.get('by', '?')}: "
                                  f"{dump.get('time', '?')} ({dump.get('names', 0)} names)"
                          ).pack(anchor="w")
            lrow = ttk.Frame(f)
            lrow.pack(anchor="w", pady=(6, 0))
            ttk.Button(lrow, text=f"Send loot ({s.waiting()} waiting)", command=self.send_loot,
                       state="normal" if can_act else "disabled").pack(side="left")
            ttk.Button(lrow, text="Clear loot", command=self.clear_loot_click,
                       state="normal" if s.loot_items() else "disabled").pack(side="left", padx=6)
        elif mode in ("closed", "different"):
            held = s.waiting()
            msg = "No raid is currently running."
            if held:
                msg += f" {held} items are waiting to upload."
            ttk.Label(f, text=msg, foreground="#b45309", wraplength=400).pack(anchor="w")
            if mode == "closed":
                ttk.Button(f, text="Restart raid", command=self.restart,
                           state="normal" if can_act else "disabled").pack(anchor="w", pady=4)
                ttk.Label(f, text=f"Restart reopens raid {s.state.raid_code} with its loot "
                                  "and rolls.", wraplength=400).pack(anchor="w")
            else:
                ttk.Label(f, text="A different raid is running.").pack(anchor="w")
                row = ttk.Frame(f)
                row.pack(anchor="w", pady=4)
                ttk.Button(row, text="Join raid", command=self.join_dialog,
                           state="normal" if can_act else "disabled").pack(side="left")
                ttk.Button(row, text="Discard waiting items",
                           command=self.discard).pack(side="left", padx=4)
        else:
            row = ttk.Frame(f)
            row.pack(anchor="w")
            ttk.Button(row, text="Start raid", command=self.start_raid_dialog,
                       state="normal" if can_act else "disabled").pack(side="left")
            ttk.Button(row, text="Join raid", command=self.join_dialog,
                       state="normal" if can_act else "disabled").pack(side="left", padx=4)

    # ------------------------------------------------------------------ actions
    def info(self, title, text):
        messagebox.showinfo(title, text, parent=self.root)

    def fail(self, err):
        messagebox.showerror("EQ Loot Reader", str(err), parent=self.root)

    def ask(self, title, message, ok_text="OK", cancel_text="Cancel") -> bool:
        win = tk.Toplevel(self.root)
        win.title(title)
        win.transient(self.root)
        win.resizable(False, False)
        answer = {"v": False}
        ttk.Label(win, text=message, wraplength=380, justify="left",
                  padding=12).pack(fill="x")
        row = ttk.Frame(win, padding=(12, 0, 12, 12))
        row.pack(fill="x")

        def done(v):
            answer["v"] = v
            win.destroy()
        ttk.Button(row, text=cancel_text, command=lambda: done(False)).pack(side="right")
        ttk.Button(row, text=ok_text, command=lambda: done(True)).pack(side="right", padx=6)
        win.grab_set()
        self.root.wait_window(win)
        return answer["v"]

    def open_website(self):
        code = self.session.state.raid_code
        webbrowser.open(f"{self.settings.base_url()}/?code={code}")

    def send_dump(self):
        self.bg("dump", self.session.send_dump,
                lambda r, e: self.fail(e) if e else self.info("Raid dump", r[1]) if r[0]
                else self.fail(r[1]))

    def send_loot(self):
        def done(r, e):
            self._loot_sig = None
            if e:
                self.fail(e)
            elif not r[0]:
                self.fail(r[1])
        self.bg("loot", self.session.send_loot, done)

    def copy_code(self, copied_label):
        code = self.session.state.raid_code
        if not code:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(code)
        copied_label.configure(text="Copied")

        def hide():
            try:
                copied_label.configure(text="")
            except Exception:                      # the label was redrawn meanwhile
                pass
        self.root.after(1200, hide)

    def clear_loot_click(self):
        s = self.session
        n = len(s.loot_items())
        if n and self.ask("Clear loot?", f"Discard {n} loot items that have not been sent? "
                          "Nothing is sent to the server.", "Clear loot"):
            s.clear_loot()
            self.invalidate()

    def leave(self):
        s = self.session
        if s.waiting() and not self.ask(
                "Leave raid?", f"{s.waiting()} loot items have not been sent and will be "
                "lost. Leave anyway?", "Leave raid"):
            return
        self.bg("leave", s.leave_raid, lambda r, e: self.invalidate())

    def discard(self):
        if self.ask("Discard waiting items?", "The items waiting to upload will be lost.",
                    "Discard"):
            self.session.discard_waiting()
            self.invalidate()

    def invalidate(self):
        self._raid_sig = None
        self._loot_sig = None

    def restart(self):
        def done(r, e):
            if e:
                self.fail(e)
            self.invalidate()
        self.bg("restart", self.session.restart_raid, done)

    def join_dialog(self):
        win = tk.Toplevel(self.root)
        win.title("Join raid")
        win.transient(self.root)
        ttk.Label(win, text="Raid code", padding=(12, 12, 12, 0)).pack(anchor="w")
        var = tk.StringVar()
        ent = ttk.Entry(win, textvariable=var, width=14)
        ent.pack(padx=12, pady=6)

        def go():
            code = var.get().strip()
            if not code:
                return
            win.destroy()

            def done(r, e):
                if e:
                    self.fail(e)
                self.invalidate()
            self.bg("join", lambda: self.session.join_raid(code), done)
        row = ttk.Frame(win, padding=12)
        row.pack(fill="x")
        ttk.Button(row, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(row, text="Join", command=go).pack(side="right", padx=6)
        win.grab_set()

    def start_raid_dialog(self):
        s = self.session
        chars = self.settings.characters()
        if not chars:
            self.info("Start raid", "Tick at least one log in Settings first.")
            return
        win = tk.Toplevel(self.root)
        win.title("Start raid")
        win.transient(self.root)
        ttk.Label(win, text="Starting as", padding=(12, 12, 12, 0)).pack(anchor="w")
        var = tk.StringVar(value=self.settings.starting_as if self.settings.starting_as
                           in chars else chars[0])
        box = ttk.Combobox(win, textvariable=var, values=chars, state="readonly")
        box.pack(padx=12, pady=6, fill="x")

        def go():
            who = var.get()
            win.destroy()
            held = s.waiting()
            if held and s.state.raid_code and not self.ask(
                    "Start a new raid?", f"{held} loot items from the current raid have not "
                    "been sent and will be lost.", "Start raid"):
                return
            self.bg("start", lambda: s.start_raid(who), lambda r, e: self.started(r, e, who))
        row = ttk.Frame(win, padding=12)
        row.pack(fill="x")
        ttk.Button(row, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(row, text="Start", command=go).pack(side="right", padx=6)
        win.grab_set()

    def started(self, r, e, who):
        self.invalidate()
        if e:
            self.fail(e)
            return
        if r.get("status") == "warning":
            sm = r.get("summary", {})
            if self.ask("Start a new raid?",
                        "Guild raid listed, verify nothing is being overwritten.\n\n"
                        f"Started by {sm.get('started_by', '?')} · {sm.get('loot', 0)} loot · "
                        f"{sm.get('players', 0)} players. Starting a new raid closes it and "
                        "keeps everything in it.", "Start new raid anyway"):
                self.bg("start", lambda: self.session.start_raid(who, force=True),
                        lambda r2, e2: self.started(r2, e2, who))

    # ------------------------------------------------------------------ settings
    def open_settings(self):
        win = tk.Toplevel(self.root)
        win.title("Settings")
        win.transient(self.root)
        pad = {"padx": 12, "pady": (8, 0), "anchor": "w"}
        server = tk.StringVar(value=self.settings.server)
        key = tk.StringVar(value=self.settings.guild_key)
        folder = tk.StringVar(value=self.settings.eq_folder)
        ticks: dict[str, tk.BooleanVar] = {}

        ttk.Label(win, text="Server address (domain or IP and port)").pack(**pad)
        ttk.Entry(win, textvariable=server, width=44).pack(padx=12, fill="x")
        ttk.Label(win, text="Guild key").pack(**pad)
        ttk.Entry(win, textvariable=key, width=44, show="*").pack(padx=12, fill="x")
        ttk.Label(win, text="EverQuest folder (logs are read from Logs inside it)").pack(**pad)
        frow = ttk.Frame(win)
        frow.pack(padx=12, fill="x")
        ttk.Entry(frow, textvariable=folder, width=36).pack(side="left", fill="x", expand=True)
        listing = ScrollFrame(win, height=150)

        def scan():
            keep = {n for n, v in ticks.items() if v.get()} | set(self.settings.ticked_logs)
            tmp = Settings(eq_folder=folder.get())
            listing.clear()
            ticks.clear()
            for name, size, mtime in tmp.list_logs():
                v = tk.BooleanVar(value=name in keep)
                ticks[name] = v
                when = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
                ttk.Checkbutton(listing.inner, variable=v,
                                text=f"{name}   {human_size(size)}   {when}").pack(anchor="w")

        def browse():
            d = filedialog.askdirectory(parent=win, initialdir=folder.get() or None)
            if d:
                folder.set(d)
                scan()
        ttk.Button(frow, text="Browse...", command=browse).pack(side="left", padx=(6, 0))
        ttk.Label(win, text="Logs to watch (none are ticked until you choose)").pack(**pad)
        listing.pack(padx=12, fill="both", expand=True)
        ttk.Button(win, text="Refresh", command=scan).pack(padx=12, pady=(4, 0), anchor="w")

        def save():
            self.settings.server = server.get().strip()
            self.settings.guild_key = key.get()
            self.settings.eq_folder = folder.get().strip()
            self.settings.ticked_logs = [n for n, v in ticks.items() if v.get()]
            self.settings.save()
            self.session.reload_logs()
            self.rebuild_session()
            win.destroy()
        row = ttk.Frame(win, padding=12)
        row.pack(fill="x")
        ttk.Button(row, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(row, text="Save", command=save).pack(side="right", padx=6)
        scan()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
