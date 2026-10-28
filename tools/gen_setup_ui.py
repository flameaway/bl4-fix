"""
Copies the game UI files the setup app's page uses into installer/ui/game/ (gitignored: they're
the game's files, taken from the builder's own install).

    python tools/gen_setup_ui.py [--game <Borderlands 4 folder>] [--oodle <oo2core_*_win64.dll>]
    python tools/gen_setup_ui.py --dump dev/re/ui_dump

The page (installer/ui/) is built from the game's own menus: its stylesheets, fonts, component
templates (Options rows, toggle, list button, main menu button) and art (title screen, ECHO
portraits, Claptrap). This reads them straight out of the game's paks (the highest patch wins,
like the game; or out of a dev/re/ui_extract.py dump with --dump), follows every url() the
stylesheets reference, and rewrites the game's coui://uiresources/ links into relative paths so
the page loads them from game/. installer/build.py runs it when the folder is missing and bundles
the folder into the exe.

Pak data is Oodle-compressed: without --oodle it decompresses with pyooz (`pip install pyooz`),
so nothing but the game is needed. Also needs Pillow and numpy (the icon and Claptrap).
"""

from __future__ import annotations

import argparse
import io
import os
import posixpath
import re
import shutil
import sys
from collections.abc import Callable
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "installer" / "ui" / "game"
DEFAULT_GAME = Path(os.environ.get("BL4_GAME_DIR", r"C:\Program Files (x86)\Steam\steamapps\common\Borderlands 4"))

# Stylesheets the page links (game/<path>), in this order
CSS = [
    "_shared/css/gbx.css",
    "_shared/css/oakgame.css",
    "_shared/css/oak_markup.css",
    "_shared/css/oakscroll.css",
    "_shared/css/color_tint_filters.css",
    "_shared/css/shared_animations.css",
    "components/gbx_button/css/gbx_button.css",
    "components/list_button/css/list_button.css",
    "components/main_menu_button/css/main_menu_button.css",
    "components/pip/css/pip.css",
    "components/toggle/css/toggle.css",
    "components/stepper/css/stepper.css",
    "components/text_input/css/text_input.css",
    "components/category_divider/css/category_divider.css",
    "menu_main/css/menu_main.css",
    "menu_options/css/menu_options.css",
    "title_screen/css/title_screen.css",
    "echo_portrait/css/echo_portrait.css",
    "dialog_popup/css/dialog_popup.css",
    "celebratory_notifications/css/celebratory_notifications.css",
]
# Templates the page clones (fetched as text, links rewritten relative to index.html)
HTML = [
    "components/list_button/list_button.html",
    "components/main_menu_button/main_menu_button.html",
    "components/toggle/toggle.html",
    "components/stepper/stepper.html",
    "components/text_input/text_input.html",
    "components/category_divider/category_divider.html",
    "menu_options/menu_options.html",
    "menu_main/menu_main.html",
    "title_screen/title_screen.html",
    "echo_portrait/echo_portrait.html",
    "dialog_popup/dialog_popup.html",
]
# Files used directly by the page
FILES = [
    "_shared/fonts/Industry-Book.ttf",
    "_shared/fonts/Industry-Medium.ttf",
    "_shared/fonts/Industry-MediumItalic.ttf",
    "_shared/fonts/Industry-Demi.ttf",
    "_shared/fonts/Industry-Bold.ttf",
    "_shared/fonts/IndustryMono-Book.ttf",
    "_shared/fonts/IndustryMono-Medium.ttf",
    "_shared/fonts/IndustryMono-Demi.ttf",
    "echo_portrait/assets/UI_Art_Assets/echo_portrait_art/art_echo_portrait_claptrap.png",
    "menu_news/assets/image_large/BL4_News_Claptrap_Full_1920x1080.png",
    "menu_news/assets/image_large/OAK2_TGASCREEN_1920.png",
    "menu_equip/assets/BG.png",
    "loading_spinner/assets/VaultSpinner.png",  # 8x8 frames of 256 px
    "components/menu_dropdown/assets/claptrap.png",
    "components/menu_dropdown/assets/vault_symbol.png",
    *(f"title_screen/assets/ui_art_assets/ts_elec_{i:02}.png" for i in range(1, 16)),
    *(f"title_screen/assets/ui_art_assets/ts_logo_elec_{i:02}.png" for i in range(1, 8)),
    *(f"title_screen/assets/ui_art_assets/ts_logo_elec_idle_{i:02}.png" for i in range(1, 9)),
]

# Not needed by an English page: localized fonts (~30 MB), DLC (common:// etc.) links
SKIP = re.compile(r"^_shared/fonts/[^/]+/|:/")
# Coherent-only CSS: its colour matrix filter (how the game tints its white textures) becomes an
# SVG feColorMatrix the page puts in its document (game/_setup/filters.svg), its text-stroke the
# -webkit- one
COH_MATRIX = re.compile(r"coh-color-matrix\(([^)]*)\)")
TEXT_STROKE = re.compile(r"(?<![-\w])text-stroke\s*:")
COUI = re.compile(r"coui://uiresources/([^\"')\s]+)", re.I)
URL = re.compile(r"url\(\s*[\"']?([^\"')]+)[\"']?\s*\)", re.I)


Reader = Callable[[str], "bytes | None"]  # path under uiresources -> the file, None if absent


def pak_reader(game: Path, oodle: Path | None) -> Reader:
    """The game's own uiresources files, out of its paks (our ~mods paks and DLC folders skipped)."""
    sys.path.insert(0, str(REPO / "mods" / "bl4_item_cards"))
    sys.path.insert(0, str(REPO / "tools"))
    sys.path.insert(0, str(REPO / "dev" / "re"))
    import pak  # noqa: PLC0415
    from gen_item_cards import extract  # noqa: PLC0415
    from ui_extract import list_entries  # noqa: PLC0415

    paks = [p for p in (game / "OakGame/Content/Paks").rglob("*.pak") if "~mods" not in p.parts]
    if not paks:
        raise SystemExit(f"no game paks under {game}: pass --game <Borderlands 4 folder> (or set BL4_GAME_DIR)")
    files: dict[str, tuple[Path, int]] = {}
    for pak_path in sorted(paks, key=pak.patch_number):  # later (higher patch) paks override earlier ones
        for path, offset in list_entries(pak_path).items():
            low = path.lower()
            if "/uiresources/" in low and "/dlc/" not in low:
                files[low.split("/uiresources/", 1)[1]] = (pak_path, offset)

    def read(rel: str) -> bytes | None:
        hit = files.get(rel.lower())  # paths in the game's CSS don't always match the files' case
        if hit is None or hit[1] < 0:  # -1: deleted by a later patch
            return None
        return extract(pak.PakEntryInfo(hit[0], False, "", hit[1]), oodle)

    return read


def dump_reader(dump: Path) -> Reader:
    """Files from a dev/re/ui_extract.py dump."""

    def read(rel: str) -> bytes | None:
        path = dump / rel
        if path.is_file():
            return path.read_bytes()
        parent = dump
        for part in PurePosixPath(rel).parts:  # paths in the game's CSS don't always match the files' case
            try:
                match = next((c for c in parent.iterdir() if c.name.lower() == part.lower()), None)
            except OSError:
                return None
            if match is None:
                return None
            parent = match
        return parent.read_bytes() if parent.is_file() else None

    return read


def make_claptrap(src: bytes, dst: Path) -> None:
    """
    Claptrap cut out of the news art (he's drawn on flat red): the red reached from the picture's
    edges goes, then enclosed pockets of the same pure red (between his wheel struts) unless
    small. His own reds (warning plate, paint) are duller, so they stay.
    """
    from collections import deque  # noqa: PLC0415

    import numpy as np  # noqa: PLC0415
    from PIL import Image, ImageFilter  # noqa: PLC0415

    im = Image.open(io.BytesIO(src)).convert("RGB")
    a = np.asarray(im).astype(int)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    red = (r > 140) & (r - g > 70) & (r - b > 90)  # the background and its orange glow
    pure = (r > 200) & (g < 35) & (b < 45)  # the background's own red
    h, w = red.shape
    gone = np.zeros_like(red)
    labels = np.zeros((h, w), dtype=np.int32)

    def fill(y0: int, x0: int, mask: np.ndarray, mark: np.ndarray, value: int = 1) -> list[tuple[int, int]]:
        pts = []
        q = deque([(y0, x0)])
        mark[y0, x0] = value
        while q:
            y, x = q.popleft()
            pts.append((y, x))
            for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not mark[ny, nx]:
                    mark[ny, nx] = value
                    q.append((ny, nx))
        return pts

    for x in range(w):
        for y in (0, h - 1):
            if red[y, x] and not gone[y, x]:
                fill(y, x, red, gone)
    for y in range(h):
        for x in (0, w - 1):
            if red[y, x] and not gone[y, x]:
                fill(y, x, red, gone)
    pocket = pure & ~gone
    n = 0
    for y, x in zip(*np.nonzero(pocket)):
        if not labels[y, x]:
            n += 1
            pts = fill(y, x, pocket, labels, n)
            if len(pts) > 150:
                ys, xs = zip(*pts)
                gone[list(ys), list(xs)] = True
    alpha = Image.fromarray(np.where(gone, 0, 255).astype("uint8"))
    alpha = alpha.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(1.2))
    out = im.convert("RGBA")
    out.putalpha(alpha)
    out = out.crop(alpha.getbbox())
    dst.parent.mkdir(parents=True, exist_ok=True)
    out.save(dst, optimize=True)


_matrices: dict[str, str] = {}  # values -> filter id


def _matrix_id(values: str) -> str:
    key = " ".join(v.strip() for v in values.split(","))
    return _matrices.setdefault(key, f"cm{len(_matrices)}")


def _write_filters(path: Path) -> None:
    filters = "".join(
        f'<filter id="{fid}" color-interpolation-filters="sRGB"><feColorMatrix type="matrix" values="{values}"/></filter>'
        for values, fid in _matrices.items()
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="0" height="0" style="position:absolute">{filters}</svg>',
                    encoding="utf-8")  # fmt: skip


def make_icon(src: bytes, out_dir: Path) -> None:
    """The app's icon: the lit vault symbol from the title screen's menu panel."""
    from PIL import Image  # noqa: PLC0415

    im = Image.open(io.BytesIO(src)).convert("RGBA")
    im = im.crop(im.getchannel("A").getbbox())
    side = max(im.size)
    square = Image.new("RGBA", (side, side))
    square.alpha_composite(im, ((side - im.width) // 2, (side - im.height) // 2))
    big = square.resize((256, 256), Image.LANCZOS)
    out_dir.mkdir(parents=True, exist_ok=True)
    big.save(out_dir / "icon.png", optimize=True)
    big.save(out_dir / "icon.ico", sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)])


def _text(data: bytes) -> str:
    """As read_text() reads it (universal newlines): write_text() puts the line ends back."""
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


def _relative(target: str, from_file: str) -> str:
    return os.path.relpath(target, os.path.dirname(from_file) or ".").replace(os.sep, "/")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--game", type=Path, default=DEFAULT_GAME, help="the Borderlands 4 folder")
    parser.add_argument("--oodle", type=Path, help="an oo2core_*_win64.dll (default: pyooz)")
    parser.add_argument("--dump", type=Path, help="read a dev/re/ui_extract.py dump instead of the paks")
    args = parser.parse_args()
    read = dump_reader(args.dump) if args.dump else pak_reader(args.game, args.oodle)

    def need(rel: str) -> bytes:
        if (data := read(rel)) is None:
            raise SystemExit(f"{rel} isn't in the game's files")
        return data

    shutil.rmtree(OUT, ignore_errors=True)
    done: set[str] = set()
    missing: list[str] = []
    queue = [*CSS, *HTML, *FILES]
    while queue:
        rel = queue.pop()
        key = rel.lower()
        if key in done or SKIP.search(key):
            continue
        done.add(key)
        data = read(rel)
        if data is None:
            missing.append(rel)
            continue
        dst = OUT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        suffix = PurePosixPath(rel).suffix.lower()
        if suffix == ".css":
            text = _text(data)
            refs = [m.group(1) for m in COUI.finditer(text)]
            # Relative url()s inside the game's CSS, resolved against the stylesheet
            for m in URL.finditer(text):
                ref = m.group(1).strip()
                if not ref.lower().startswith(("coui:", "data:", "http", "#", "texture2d", "/")):
                    refs.append(posixpath.normpath(str(PurePosixPath(rel).parent / ref)))
            queue.extend(r.split("?")[0] for r in refs)
            text = COUI.sub(lambda m: _relative(m.group(1), rel), text)
            text = COH_MATRIX.sub(lambda m: f"url(#{_matrix_id(m.group(1))})", text)
            text = TEXT_STROKE.sub("-webkit-text-stroke:", text)
            dst.write_text(text, encoding="utf-8")
        elif suffix == ".html":
            text = _text(data)
            queue.extend(r for r in (m.group(1) for m in COUI.finditer(text)) if not r.endswith((".js", ".data", ".css")))
            dst.write_text(COUI.sub(lambda m: "game/" + m.group(1), text), encoding="utf-8")
        else:
            dst.write_bytes(data)
    _write_filters(OUT / "_setup/filters.svg")
    make_icon(need("menu_main/assets/ui_art_asset/menu_panel_logo.png"), OUT / "_setup")
    make_claptrap(need("menu_news/assets/image_large/BL4_News_Claptrap_Full_1920x1080.png"), OUT / "_setup/claptrap.png")
    size = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"{len(done) - len(missing)} files, {size / 1e6:.1f} MB -> {OUT}")
    if missing:
        print(f"{len(missing)} referenced files not in the game's files (fine for unused states): {', '.join(sorted(missing)[:8])}...")


if __name__ == "__main__":
    main()
