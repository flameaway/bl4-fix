"""Tkinter GUI for BL4 Fix: setup and mod choice on one tab, friends + firewall on the other."""

from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import catalog
import core
import selfupdate
import shared

PAD = {"padx": 8, "pady": 4}


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"BL4 Fix {shared.MOD_VERSION}")
        icon = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "icon.ico"
        if icon.exists():
            self.iconbitmap(default=str(icon))
        self.geometry("820x780")
        self.minsize(720, 700)

        found = core.find_game_dirs()
        self.game_dir = tk.StringVar(value=str(found[0]) if found else "")
        self.busy = False
        # Tk isn't thread safe: worker threads queue callables, and the main loop runs them
        self.ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._drain_ui_queue()

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=6, pady=6)
        notebook.add(self._build_setup(notebook), text="Setup")
        notebook.add(self._build_friends(notebook), text="Friends & hosting")

        self.refresh_status()
        self.load_friends()
        self.lookup_public_ip()
        if selfupdate.finish_update():
            self.log(f"Updated to BL4 Fix {shared.MOD_VERSION}.")
        threading.Thread(target=self._check_update, daemon=True).start()

    def _check_update(self) -> None:
        result, manifest = selfupdate.check()
        if result and result["stale"]:
            self.on_ui(lambda: self._offer_update(result, manifest))

    def _offer_update(self, result: dict, manifest: dict) -> None:
        lines = [f"BL4 Fix Setup {result['current']} -> {result['version']} ({result['release']})"]
        if result["new_mods"]:
            lines.append("New mods: " + ", ".join(result["new_mods"]))
        if result["newer_mods"]:
            lines.append("Updated: " + ", ".join(result["newer_mods"]))
        if selfupdate.this_exe() is None:
            self.log("A newer release is out: " + "; ".join(lines))
            return
        text = "A newer BL4 Fix Setup is out.\n\n" + "\n".join(lines) + "\n\nUpdate and restart now?"
        if not messagebox.askyesno("BL4 Fix", text):
            return

        def run() -> None:
            exe = selfupdate.update(manifest, self.log)
            selfupdate.restart(exe)
            self.on_ui(self.destroy)

        self._run_task("Update", run)

    def on_ui(self, func: Callable[[], None]) -> None:
        """Run func on the Tk thread. Safe to call from any thread."""
        self.ui_queue.put(func)

    def _drain_ui_queue(self) -> None:
        while True:
            try:
                func = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            func()
        self.after(50, self._drain_ui_queue)

    # region Setup tab

    def _build_setup(self, parent: ttk.Notebook) -> ttk.Frame:
        frame = ttk.Frame(parent)

        row = ttk.Frame(frame)
        row.pack(fill="x", **PAD)
        ttk.Label(row, text="Borderlands 4 folder:").pack(side="left")
        ttk.Entry(row, textvariable=self.game_dir).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(row, text="Browse...", command=self.browse).pack(side="left")

        self.status_text = tk.Text(frame, height=9, wrap="word", state="disabled", font=("Consolas", 9))
        self.status_text.pack(fill="x", **PAD)

        self._build_mods(frame)

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", **PAD)
        ttk.Button(buttons, text="Install / Apply", command=self.install).pack(side="left")
        ttk.Button(buttons, text="Refresh", command=self.refresh_status).pack(side="left", padx=4)
        ttk.Button(buttons, text="Uninstall...", command=self.uninstall).pack(side="right")
        ttk.Button(buttons, text="Report a problem", command=self.bug_report).pack(side="right", padx=4)
        ttk.Button(buttons, text="Safe mode...", command=self.safe_mode).pack(side="right")

        ttk.Label(frame, text="Log:").pack(anchor="w", padx=8)
        self.log_text = tk.Text(frame, height=8, wrap="word", state="disabled", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True, **PAD)
        return frame

    def _build_mods(self, parent: ttk.Frame) -> None:
        """A checkbox per mod. Ticks take effect with Install / Apply (add and remove)."""
        box = ttk.LabelFrame(parent, text="Mods (tick what you want, then Install / Apply; run this app again any time to change it)")
        box.pack(fill="x", **PAD)
        grid = ttk.Frame(box)
        grid.pack(fill="x", padx=4, pady=2)
        self.mod_vars: dict[str, tk.BooleanVar] = {}
        self.mod_desc = tk.StringVar(value="Point at a mod to see what it does.")
        columns = 2
        for i, info in enumerate(catalog.MODS):
            var = tk.BooleanVar(value=info.required or info.default)
            self.mod_vars[info.id] = var
            text = info.name + (" (always)" if info.required else "") + (" *" if info.source == "ported" else "")
            check = ttk.Checkbutton(grid, text=text, variable=var)
            if info.required:
                check.state(["disabled"])
            check.grid(row=i // columns, column=i % columns, sticky="w", padx=6, pady=1)
            check.bind("<Enter>", lambda _e, info=info: self.mod_desc.set(f"{info.name}: {info.blurb}"))
        for col in range(columns):
            grid.columnconfigure(col, weight=1)

        ttk.Label(box, textvariable=self.mod_desc, foreground="#333").pack(anchor="w", padx=8)
        row = ttk.Frame(box)
        row.pack(fill="x", padx=4, pady=(2, 4))
        ttk.Button(row, text="Select all", command=lambda: self._select_mods(set(catalog.BY_ID))).pack(side="left")
        ttk.Button(row, text="Select none", command=lambda: self._select_mods(set())).pack(side="left", padx=4)
        ttk.Button(row, text="Defaults", command=lambda: self._select_mods(set(catalog.DEFAULTS))).pack(side="left")
        ttk.Label(
            row,
            text="* ports of Yeti's mods (GPL-3.0). Settings and keys for every mod: F7 in game.",
            foreground="#555",
        ).pack(side="right", padx=4)

    def _select_mods(self, ids: set[str]) -> None:
        for mod_id, var in self.mod_vars.items():
            var.set(mod_id in ids or catalog.BY_ID[mod_id].required)

    def selected_mods(self) -> set[str]:
        return {mod_id for mod_id, var in self.mod_vars.items() if var.get()}

    def browse(self) -> None:
        path = filedialog.askdirectory(title="Select the Borderlands 4 install folder")
        if path:
            self.game_dir.set(path)
            self.refresh_status()
            self.load_friends()

    def game(self) -> Path | None:
        path = Path(self.game_dir.get())
        if not core.is_game_dir(path):
            messagebox.showerror("BL4 Fix", "Pick your Borderlands 4 install folder first.")
            return None
        return path

    def _set_text(self, widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.configure(state="disabled")

    def log(self, msg: str) -> None:
        def append() -> None:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        self.on_ui(append)

    def refresh_status(self) -> None:
        path = Path(self.game_dir.get())
        if not core.is_game_dir(path):
            self._set_text(self.status_text, "Borderlands 4 not found - use Browse to pick its folder.")
            return
        self._set_text(self.status_text, "\n".join(core.status(path).lines()))
        self._select_mods(core.choose_mods(path))

    def _run_task(self, name: str, func) -> None:  # noqa: ANN001
        if self.busy:
            return
        self.busy = True
        self.log(f"--- {name} ---")

        def worker() -> None:
            try:
                func()
            except Exception as ex:  # noqa: BLE001
                self.log(f"Error: {ex}")
                self.on_ui(lambda ex=ex: messagebox.showerror("BL4 Fix", str(ex)))
            finally:
                self.busy = False
                self.on_ui(self.refresh_status)

        threading.Thread(target=worker, daemon=True).start()

    def install(self) -> None:
        game = self.game()
        if not game or self.busy:
            return
        chosen = self.selected_mods()
        dropping = [catalog.BY_ID[m].name for m in catalog.OPTIONAL if m in core.installed_mods(game) and m not in chosen]
        if dropping and not messagebox.askyesno(
            "BL4 Fix",
            "These mods will be removed (their settings are kept):\n\n" + "\n".join(dropping) + "\n\nContinue?",
        ):
            return
        use_firewall = bool(self.use_firewall.get())
        self._run_task("Install", lambda: core.install(game, self.log, mods=chosen, use_firewall=use_firewall))

    def uninstall(self) -> None:
        game = self.game()
        if not game:
            return
        remove_sdk = messagebox.askyesnocancel(
            "Uninstall BL4 Fix",
            "This removes every mod BL4 Fix installed.\n\n"
            "Also remove the Python SDK?\n\nChoose No if you use other SDK mods.",
        )
        if remove_sdk is None:
            return
        self._run_task("Uninstall", lambda: core.uninstall(game, bool(remove_sdk), self.log))

    def safe_mode(self) -> None:
        """Safe mode on/off (core.set_safe_mode): other modders' mods set aside, not deleted."""
        game = self.game()
        if not game or self.busy:
            return
        st = core.safe_mode_status(game)
        why = (
            "For when a mod breaks the game: sets every mod that isn't BL4 Fix's aside (not deleted)"
            " until you turn this off. BL4 Fix's mods keep working."
        )
        if st["on"]:
            names = ", ".join(st["aside"]) or "none"
            question = f"Safe mode is on: {len(st['aside'])} set aside ({names}).\n\nTurn it off and put them back?"
        else:
            names = ", ".join(st["others"]) or "none"
            question = f"{why}\n\nOther modders' mods installed: {len(st['others'])} ({names}).\n\nTurn safe mode on?"
        if not messagebox.askyesno("BL4 Fix: Safe mode", question):
            return
        on = not st["on"]
        self._run_task("Safe mode " + ("on" if on else "off"), lambda: core.set_safe_mode(game, on, self.log))

    def bug_report(self) -> None:
        """A bug report zip (the Mod Menu's report.py), shown in Explorer, then the GitHub issue."""
        game = self.game()
        if not game or self.busy:
            return

        def offer(path: Path, url: str) -> None:
            core.show_report(path)
            if messagebox.askyesno(
                "BL4 Fix",
                f"Saved as {path.name} (Explorer shows it now). Your user name, IPs and account ids are masked;"
                " nothing was sent.\n\nOpen a new GitHub issue to attach it?",
            ):
                import os  # noqa: PLC0415

                os.startfile(url)  # noqa: S606

        def run() -> None:
            path, url = core.bug_report(game, log=self.log)
            self.on_ui(lambda: offer(path, url))

        self._run_task("Bug report", run)

    # endregion
    # region Friends tab

    def _build_friends(self, parent: ttk.Notebook) -> ttk.Frame:
        frame = ttk.Frame(parent)

        you = ttk.LabelFrame(frame, text="You")
        you.pack(fill="x", **PAD)
        self.public_ip = tk.StringVar(value="looking up...")
        row = ttk.Frame(you)
        row.pack(fill="x", **PAD)
        ttk.Label(row, text="Your public IP (send this to your friend):").pack(side="left")
        ttk.Entry(row, textvariable=self.public_ip, width=18, state="readonly").pack(side="left", padx=4)
        ttk.Button(row, text="Copy", command=lambda: self._copy(self.public_ip.get())).pack(side="left")

        self.host_port = tk.IntVar(value=shared.DEFAULT_PORT)
        self.allow_lan = tk.BooleanVar(value=True)
        self.use_firewall = tk.BooleanVar(value=False)
        row = ttk.Frame(you)
        row.pack(fill="x", **PAD)
        ttk.Label(row, text="Port when you host:").pack(side="left")
        ttk.Spinbox(row, from_=1024, to=65535, textvariable=self.host_port, width=7).pack(side="left", padx=4)
        ttk.Checkbutton(row, text="Also allow my local network / Tailscale", variable=self.allow_lan).pack(
            side="left", padx=12,
        )
        row = ttk.Frame(you)
        row.pack(fill="x", **PAD)
        ttk.Checkbutton(
            row, text="Firewall rule: block my host port to everyone but these friends (strongly recommended if you host)",
            variable=self.use_firewall,
        ).pack(side="left")
        lan = core.lan_ip() or "this PC's local IP"
        ttk.Label(
            you,
            text=(
                "Hosting over the internet: forward UDP to this PC on your router "
                f"({lan}). Joining needs no setup."
            ),
            foreground="#555",
            wraplength=660,
        ).pack(anchor="w", **PAD)

        friends = ttk.LabelFrame(frame, text="Friends (they can join you; you can join them)")
        friends.pack(fill="both", expand=True, **PAD)
        self.tree = ttk.Treeview(friends, columns=("name", "ip", "port"), show="headings", height=6)
        for col, label, width in (("name", "Name", 200), ("ip", "Public IP", 200), ("port", "Their host port", 120)):
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width)
        self.tree.pack(fill="both", expand=True, **PAD)

        form = ttk.Frame(friends)
        form.pack(fill="x", **PAD)
        self.f_name = tk.StringVar()
        self.f_ip = tk.StringVar()
        self.f_port = tk.IntVar(value=shared.DEFAULT_PORT)
        ttk.Label(form, text="Name").pack(side="left")
        ttk.Entry(form, textvariable=self.f_name, width=14).pack(side="left", padx=4)
        ttk.Label(form, text="IP").pack(side="left")
        ttk.Entry(form, textvariable=self.f_ip, width=16).pack(side="left", padx=4)
        ttk.Label(form, text="Port").pack(side="left")
        ttk.Spinbox(form, from_=1024, to=65535, textvariable=self.f_port, width=7).pack(side="left", padx=4)
        ttk.Button(form, text="Add / update", command=self.add_friend).pack(side="left", padx=4)
        ttk.Button(form, text="Remove selected", command=self.remove_friend).pack(side="left")

        ttk.Button(frame, text="Save (and update the firewall rule)", command=self.save_friends).pack(anchor="e", **PAD)
        ttk.Label(
            frame,
            text="In game: press F6 for the co-op panel (BL4 Direct Connect) - host, join, and add friends there too.",
            foreground="#555",
        ).pack(anchor="w", **PAD)
        return frame

    def _copy(self, text: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)

    def lookup_public_ip(self) -> None:
        def worker() -> None:
            try:
                ip = core.public_ip()
            except RuntimeError as ex:
                ip = f"({ex})"
            self.on_ui(lambda: self.public_ip.set(ip))

        threading.Thread(target=worker, daemon=True).start()

    def load_friends(self) -> None:
        path = Path(self.game_dir.get())
        if not core.is_game_dir(path):
            return
        config = core.load_config(path)
        self.host_port.set(config.host_port)
        self.allow_lan.set(config.allow_lan)
        self.use_firewall.set(config.firewall)
        self.tree.delete(*self.tree.get_children())
        for f in config.friends:
            self.tree.insert("", "end", values=(f.name, f.address, f.port))

    def add_friend(self) -> None:
        name = self.f_name.get().strip()
        ip = shared.normalize_ipv4(self.f_ip.get()) or ""
        if not name or not ip:
            messagebox.showerror("BL4 Fix", "Enter a name and an IPv4 address like 203.0.113.5")
            return
        for item in self.tree.get_children():
            if self.tree.item(item, "values")[0].lower() == name.lower():
                self.tree.delete(item)
        self.tree.insert("", "end", values=(name, ip, self.f_port.get()))
        self.f_name.set("")
        self.f_ip.set("")

    def remove_friend(self) -> None:
        for item in self.tree.selection():
            self.tree.delete(item)

    def save_friends(self) -> None:
        game = self.game()
        if not game:
            return
        config = shared.Config(
            host_port=int(self.host_port.get()),
            allow_lan=bool(self.allow_lan.get()),
            firewall=bool(self.use_firewall.get()),
            friends=[
                shared.Friend(str(v[0]), str(v[1]), int(v[2]))
                for v in (self.tree.item(i, "values") for i in self.tree.get_children())
            ],
        )
        config.save(core.friends_file(game))
        self.log("Saved friends list.")
        self._run_task("Firewall", lambda: core.apply_firewall(game, self.log))
        messagebox.showinfo(
            "BL4 Fix",
            "Saved. If the game is running, press 'Reload friends' in the BL4 Direct Connect panel (F6).",
        )

    # endregion


def run() -> None:
    App().mainloop()
