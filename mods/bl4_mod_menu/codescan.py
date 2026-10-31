"""
What a mod's code uses, read before it's installed or updated from another modder's repository:
the internet, other programs, native code, the Windows registry, code built at run time. The page
shows it on the install / update confirm step (repos.py). It's a hint, not a guarantee: code can
hide what it does (getattr with a built string, a downloaded script), and this never runs any of it.

Python's own parser (ast) reads each .py file of the zip. Plain Python, worker threads only, and
light on the game thread: ast.parse holds the GIL for its whole call (~0.3 ms per KB of source), so
big files are parsed a few KB of top-level statements at a time, with 1 ms rests in between (root
CLAUDE.md, "A worker running Python still holds the GIL").
"""

from __future__ import annotations

import ast
import io
import time
import zipfile
from pathlib import Path
from typing import Any

# What the page shows, in this order
USES = ("network", "runs programs", "native code", "Windows registry", "dynamic code", "unreadable code")
MAX_SOURCE = 8 << 20  # .py bytes read per mod; past it, the rest counts as unreadable
CHUNK = 2 << 10  # source parsed per ast.parse call (~1 ms)
SLICE_S = 0.001  # work this long, then rest 1 ms

NETWORK = frozenset({
    "socket", "_socket", "requests", "ssl", "_ssl", "ftplib", "smtplib", "poplib", "imaplib", "telnetlib", "websocket",
    "websockets", "aiohttp", "httpx", "urllib3", "xmlrpc", "socketserver", "asyncio.streams", "webbrowser",
})  # fmt: skip
# Packages only some of whose modules connect (urllib.parse and http.HTTPStatus don't)
NETWORK_PARTS = ("urllib.request", "urllib.error", "urllib.response", "urllib.robotparser", "http.client",
                 "http.server", "http.cookiejar")  # fmt: skip
PROGRAMS = frozenset({"subprocess", "_posixsubprocess", "_winapi", "multiprocessing", "pty"})
NATIVE = frozenset({"ctypes", "_ctypes", "cffi", "_cffi_backend"})
REGISTRY = frozenset({"winreg", "_winreg"})
DYNAMIC = frozenset({"marshal", "runpy"})
# os.<name> that starts a program
OS_PROGRAMS = ("system", "startfile", "popen", "exec", "spawn", "posix_spawn")
DYNAMIC_CALLS = frozenset({"exec", "eval", "compile"})
NATIVE_FILES = (".pyd", ".dll", ".so", ".exe", ".dylib")
COMPILED_FILES = (".pyc", ".pyo")


def _module_use(name: str) -> str | None:
    top = name.split(".")[0]
    if any(name == part or name.startswith(f"{part}.") for part in NETWORK_PARTS):
        return "network"
    for kind, names in (("network", NETWORK), ("runs programs", PROGRAMS), ("native code", NATIVE),
                        ("Windows registry", REGISTRY), ("dynamic code", DYNAMIC)):  # fmt: skip
        if top in names or name in names:
            return kind
    return None


def _os_program(attr: str) -> bool:
    return any(attr == p or (p in ("exec", "spawn") and attr.startswith(p)) for p in OS_PROGRAMS)


class Scan:
    """What one mod's files use: uses = {kind: where it was first seen}."""

    def __init__(self) -> None:
        self.uses: dict[str, str] = {}
        self.source = 0
        self.files = 0
        self._spent = time.perf_counter()

    def add(self, kind: str, where: str) -> None:
        self.uses.setdefault(kind, where)

    def rest(self) -> None:
        """Rests 1 ms once this slice has worked SLICE_S (hands the GIL to the game thread)."""
        if time.perf_counter() - self._spent >= SLICE_S:
            time.sleep(0.001)
            self._spent = time.perf_counter()

    def result(self) -> dict[str, Any]:
        kinds = [k for k in USES if k in self.uses]
        return {"uses": kinds, "where": {k: self.uses[k] for k in kinds}, "source": self.source, "files": self.files}

    # region One file

    def python(self, name: str, data: bytes) -> None:
        self.files += 1
        if self.source + len(data) > MAX_SOURCE:
            self.add("unreadable code", f"{name}: more code than the scan reads (8 MB)")
            return
        self.source += len(data)
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            self.add("unreadable code", f"{name}: not UTF-8 text")
            return
        trees = self._parse(text)
        if trees is None:
            self.add("unreadable code", f"{name}: Python can't read it")
            return
        aliases: dict[str, str] = {}  # local name -> module it stands for
        for tree in trees:
            self._walk(name, tree, aliases)

    def _parse(self, text: str) -> list[ast.AST] | None:
        """The file's statements in parts of about CHUNK bytes, or the whole file at once if a cut
        doesn't parse on its own. None: Python can't read it. Cuts go before a top-level statement
        or a method (parsed under "if 1:"), so one call never holds the GIL long."""
        if len(text) <= CHUNK:
            try:
                return [ast.parse(text)]
            except (SyntaxError, ValueError, RecursionError, MemoryError):
                return None
        lines = text.splitlines(keepends=True)
        # Where a statement may start: column 0 (not a comment, a closing bracket or a clause of the
        # statement before: else, except...), or a def / class / decorator indented once
        cuts = [0]
        size = 0
        for i, line in enumerate(lines):
            size += len(line)
            if size >= CHUNK and i + 1 < len(lines):
                nxt = lines[i + 1]
                top = nxt[:1] not in ("", " ", "\t", "\n", "\r", "#", ")", "]", "}") and not nxt.startswith(("else", "elif", "except", "finally"))
                method = nxt.startswith("    ") and nxt[4:5] != " " and nxt[4:].startswith(("def ", "async def ", "class ", "@"))
                if top or method:
                    cuts.append(i + 1)
                    size = 0
        cuts.append(len(lines))
        trees: list[ast.AST] = []
        i, j = 0, 1  # the part is lines[cuts[i]:cuts[j]]
        while i < len(cuts) - 1:
            part = "".join(lines[cuts[i] : cuts[j]])
            try:
                trees.append(ast.parse(("if 1:\n" + part) if part[:1] == " " else part))
            except (SyntaxError, ValueError, RecursionError, MemoryError):
                # Cut inside a string, a bracket or between a decorator and its def: take more
                if j + 1 < len(cuts) and len(part) < 16 * CHUNK:
                    j += 1
                    self.rest()
                    continue
                break
            i, j = j, j + 1
            self.rest()
        else:
            return trees
        # A part didn't parse on its own: the whole file in one call decides
        try:
            return [ast.parse(text)]
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            return None

    def _walk(self, name: str, tree: ast.AST, aliases: dict[str, str]) -> None:
        for count, node in enumerate(ast.walk(tree)):
            if count % 256 == 255:  # noqa: PLR2004
                self.rest()
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self._module(name, node, alias.name)
                    aliases[(alias.asname or alias.name).split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level == 0 and module:
                    self._module(name, node, module)
                    for alias in node.names:
                        full = f"{module}.{alias.name}"
                        self._module(name, node, full)
                        if module == "os" and _os_program(alias.name):
                            self.add("runs programs", f"{name}:{node.lineno}: from os import {alias.name}")
                        if module in ("builtins", "importlib") and alias.name in (*DYNAMIC_CALLS, "__import__"):
                            self.add("dynamic code", f"{name}:{node.lineno}: from {module} import {alias.name}")
                        aliases[alias.asname or alias.name] = full
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                module = aliases.get(node.value.id, "")
                if module == "os" and _os_program(node.attr):
                    self.add("runs programs", f"{name}:{node.lineno}: os.{node.attr}")
                elif module == "builtins" and node.attr in DYNAMIC_CALLS:
                    self.add("dynamic code", f"{name}:{node.lineno}: builtins.{node.attr}")
                elif module:
                    self._module(name, node, f"{module}.{node.attr}")
            elif isinstance(node, ast.Call):
                self._call(name, node, aliases)

    def _module(self, name: str, node: ast.AST, module: str) -> None:
        if (kind := _module_use(module)) is not None:
            self.add(kind, f"{name}:{getattr(node, 'lineno', 0)}: {module}")

    def _call(self, name: str, node: ast.Call, aliases: dict[str, str]) -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id in DYNAMIC_CALLS:
            self.add("dynamic code", f"{name}:{node.lineno}: {func.id}()")
            return
        imports = (
            (isinstance(func, ast.Name) and (func.id == "__import__" or aliases.get(func.id) == "importlib.import_module"))
            or (isinstance(func, ast.Attribute) and func.attr == "import_module" and isinstance(func.value, ast.Name)
                and aliases.get(func.value.id) == "importlib")
        )  # fmt: skip
        if not imports:
            return
        first = node.args[0] if node.args else None
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            self._module(name, node, first.value)  # importlib.import_module("socket") is import socket
        else:
            self.add("dynamic code", f"{name}:{node.lineno}: an import by a computed name")

    # endregion


def scan_zip(data: bytes) -> dict[str, Any]:
    """What an .sdkmod (the zip's bytes, already checked by repos._verify) uses. Worker threads."""
    scan = Scan()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                low = info.filename.lower()
                if low.endswith(NATIVE_FILES):
                    scan.add("native code", f"{info.filename}: a native file")
                elif low.endswith(COMPILED_FILES):
                    scan.add("unreadable code", f"{info.filename}: compiled Python")
                elif low.endswith((".py", ".pyw")):
                    if scan.source + info.file_size > MAX_SOURCE:
                        scan.add("unreadable code", f"{info.filename}: more code than the scan reads (8 MB)")
                        continue
                    scan.python(info.filename, zf.read(info))
                scan.rest()
                time.sleep(0.001)  # between files
    except (zipfile.BadZipFile, OSError, ValueError, RuntimeError) as ex:
        scan.add("unreadable code", f"the zip couldn't be read ({ex})")
    return scan.result()


def scan_folder(folder: Path) -> dict[str, Any]:
    """The same for a mod installed as a folder (a dev copy)."""
    scan = Scan()
    try:
        for path in sorted(folder.rglob("*")):
            if "__pycache__" in path.parts or not path.is_file():
                continue
            low = path.name.lower()
            rel = path.relative_to(folder.parent).as_posix()
            if low.endswith(NATIVE_FILES):
                scan.add("native code", f"{rel}: a native file")
            elif low.endswith((".py", ".pyw")):
                if scan.source + path.stat().st_size > MAX_SOURCE:
                    scan.add("unreadable code", f"{rel}: more code than the scan reads (8 MB)")
                    continue
                scan.python(rel, path.read_bytes())
            scan.rest()
            time.sleep(0.001)
    except OSError as ex:
        scan.add("unreadable code", f"the folder couldn't be read ({ex})")
    return scan.result()
