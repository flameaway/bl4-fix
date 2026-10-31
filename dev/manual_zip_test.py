"""
Offline test of the release's manual install zip (tools/manual_zip.py) and its verifier
(tools/verify_release.py). Builds every .sdkmod from this tree into a scratch folder, then:

- the zip holds the SDK (no sdk_mods/settings/), every catalog .sdkmod, the README, and exactly the
  UI Link pak + startup page scripts the setup app's prepare_pages writes into a fresh game folder
  (run here with the link's game check stubbed to "ok": a fake game has no paks), and the same
  pictures the setup app's _install_assets writes, each small/ copy after its full-size picture
- two builds give the same bytes
- extracted over a game folder with the player's settings, nothing in settings/ changes
- verify_release passes on a matching fake release and fails on a changed .sdkmod / zip / sums
  line (its signature check stubbed: the real key only signs real releases)

    python dev/manual_zip_test.py
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "tools"), str(REPO / "installer"), str(REPO / "mods" / "bl4_direct"), str(REPO / "mods" / "bl4_skip_intro")]

import build  # noqa: E402
import catalog  # noqa: E402
import core  # noqa: E402
import manual_zip  # noqa: E402
import verify_release  # noqa: E402

results: list[bool] = []


def check(ok: bool, what: str) -> None:
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {what}")


def setup_app_pages(sdkmods: Path, game: Path) -> dict[str, bytes]:
    """What core.prepare_pages writes for every mod, with the release's .sdkmods as its payload."""
    load = core._load_page_module  # noqa: SLF001

    def loader(root: Path, mod_id: str, module: str):  # noqa: ANN202
        mod = load(root, mod_id, module)
        if module == "uilink":
            mod.check = lambda _game: ("ok", "", True)
        return mod

    old_payload, core.payload_mod = core.payload_mod, lambda mod_id: sdkmods / f"{mod_id}.sdkmod"
    core._load_page_module = loader  # noqa: SLF001
    try:
        core.prepare_pages(game, {m.id for m in catalog.MODS}, log=lambda _line: None)
    finally:
        core.payload_mod, core._load_page_module = old_payload, load  # noqa: SLF001
    files = {}
    for path in game.rglob("*"):
        if path.is_file():
            files[path.relative_to(game).as_posix()] = path.read_bytes()
    return files


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="bl4_manual_zip_"))
    rel = tmp / "release"
    rel.mkdir()
    for info in catalog.MODS:
        build.build_sdkmod(REPO / info.source / info.id, rel / f"{info.id}.sdkmod", catalog.license_files(info, REPO))
    sdk = build.fetch_sdk()
    tag = "v2099.01.01"
    out = manual_zip.build(rel / manual_zip.zip_name(tag), rel, sdk, tag)
    again = manual_zip.build(tmp / "again.zip", rel, sdk, tag)
    check(out.read_bytes() == again.read_bytes(), "two builds are byte-identical")

    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        contents = {n: zf.read(n) for n in names}
    with zipfile.ZipFile(sdk) as zf:
        sdk_files = {n for n in zf.namelist() if not n.endswith("/")}
    check(not any(n.startswith("sdk_mods/settings/") for n in names), "no sdk_mods/settings/ in the zip")
    check(sdk_files - {n for n in sdk_files if n.startswith("sdk_mods/settings/")} <= names, "every SDK file is in the zip")
    check(all(f"sdk_mods/{m.id}.sdkmod" in names for m in catalog.MODS), f"all {len(catalog.MODS)} catalog .sdkmods")
    check(all(contents[f"sdk_mods/{m.id}.sdkmod"] == (rel / f"{m.id}.sdkmod").read_bytes() for m in catalog.MODS),
          ".sdkmods are the release's bytes")
    check(manual_zip.README_NAME in names, "README in the zip")

    # Pictures (catalog `assets`): what the setup app's _install_assets writes into a fresh game
    # folder, PNGs byte for byte and index.json by content (the app writes it with its own JSON)
    picture_dirs = tuple(f"{manual_zip.UI_REL}/{rel_dir}/" for m in catalog.MODS for rel_dir in m.assets)
    zip_pictures = {n: d for n, d in contents.items() if n.startswith(picture_dirs)}
    pictures_game = tmp / "pictures_game"
    payload_assets = core.payload_assets
    core.payload_assets = lambda mod_id: tmp / f"{mod_id}.assets.zip"
    try:
        for info in catalog.MODS:
            if info.assets:
                build.build_assets(info, tmp / f"{info.id}.assets.zip")
                core._install_assets(pictures_game, info, log=lambda _line: None)  # noqa: SLF001
    finally:
        core.payload_assets = payload_assets
    app_pictures = {p.relative_to(pictures_game).as_posix(): p.read_bytes() for p in pictures_game.rglob("*") if p.is_file()}

    def same_picture(name: str) -> bool:
        if name.endswith(".json"):
            return json.loads(zip_pictures[name]) == json.loads(app_pictures[name])
        return zip_pictures[name] == app_pictures[name]

    pngs = sum(1 for n in zip_pictures if n.endswith(".png"))
    check(pngs > 0 and set(zip_pictures) == set(app_pictures) and all(same_picture(n) for n in zip_pictures),
          f"the pictures the setup app installs ({pngs} PNGs)")
    order = {n: i for i, n in enumerate(n for n in zipfile.ZipFile(out).namelist())}
    late = [n for n in zip_pictures if "/small/" in n and n.endswith(".png")
            and order[n] < order.get(n.replace("/small/", "/"), -1)]
    check(not late, f"every small/ copy after its picture ({late[:2]})")

    game = tmp / "fake_game"
    (game / core.GAME_EXE_REL).parent.mkdir(parents=True)
    expected = setup_app_pages(rel, game)
    zip_pages = {n: d for n, d in contents.items()
                 if n.startswith(("OakGame/Content/Paks/", "OakGame/Content/uiresources/")) and n not in zip_pictures}
    check(f"OakGame/Content/Paks/~mods/{catalog.LINK_PAK}" in zip_pages, "the UI Link pak is in the zip")
    check(any(n.endswith("bl4link/boot.js") for n in zip_pages), "bl4link/boot.js is in the zip")
    missing = sorted(set(expected) - set(zip_pages))
    extra = sorted(set(zip_pages) - set(expected))
    differ = sorted(n for n in set(expected) & set(zip_pages) if expected[n] != zip_pages[n])
    # The setup app also publishes loose files of mods whose page module runs at install (Item
    # Cards, Respec, ...); those mods publish them again at enable in game, before any page asks
    later = [n for n in missing if not n.startswith(("OakGame/Content/Paks/", *(f"OakGame/Content/uiresources/{d}/" for _, _, d in core.PAGE_SCRIPTS)))]
    check(not differ, f"page files equal to the setup app's ({differ[:3]})")
    check(not extra, f"nothing the setup app doesn't write ({extra[:3]})")
    check(missing == later, f"the setup app's startup page files are all there (missing: {sorted(set(missing) - set(later))[:3]})")
    print(f"      left to the mods in game: {len(later)} loose files ({', '.join(sorted({n.split('/')[3] for n in later}))})")

    # Extract over an install with the player's settings
    player = tmp / "player_game"
    settings = player / "sdk_mods" / "settings"
    settings.mkdir(parents=True)
    (settings / "bl4_minimap.json").write_text('{"enabled": false, "options": {"size": 3}}', encoding="utf-8")
    before = {p.name: p.read_bytes() for p in settings.iterdir()}
    with zipfile.ZipFile(out) as zf:
        zf.extractall(player)
    check({p.name: p.read_bytes() for p in settings.iterdir()} == before, "extracting keeps the player's settings")

    # verify_release on a fake release
    manifest = {"release": tag, "mods": {}, "app": {"asset": "BL4FixSetup.exe"}}
    for info in catalog.MODS:
        data = (rel / f"{info.id}.sdkmod").read_bytes()
        manifest["mods"][info.id] = {"asset": f"{info.id}.sdkmod", "sha256": hashlib.sha256(data).hexdigest()}
        if info.assets:
            build.build_assets(info, rel / f"{info.id}.assets.zip")
            blob = (rel / f"{info.id}.assets.zip").read_bytes()
            manifest["mods"][info.id]["pictures"] = {"asset": f"{info.id}.assets.zip", "sha256": hashlib.sha256(blob).hexdigest()}
    (rel / "BL4FixSetup.exe").write_bytes(b"MZ test")
    manifest["app"]["sha256"] = hashlib.sha256(b"MZ test").hexdigest()
    manifest["manual"] = {"asset": out.name, "sha256": hashlib.sha256(out.read_bytes()).hexdigest()}
    (rel / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (rel / "manifest.json.sig").write_text("test", encoding="utf-8")
    sums = [f"{hashlib.sha256(p.read_bytes()).hexdigest()} *{p.name}" for p in sorted(rel.iterdir()) if p.is_file()]
    (rel / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")
    verify_release.signing.verify = lambda *_a, **_k: None

    def run() -> int:
        sys.argv = ["verify_release.py", "--tag", tag, "--release-dir", str(rel), "--list", str(tmp / "verified.txt")]
        return verify_release.main()

    check(run() == 0, "verify_release passes on a matching release")
    listed = (tmp / "verified.txt").read_text(encoding="utf-8").split()
    check(len(listed) == len(catalog.MODS) + sum(1 for m in catalog.MODS if m.assets) + 1, f"{len(listed)} files listed for attestation")
    victim = rel / "bl4_less_fog.sdkmod"
    good = victim.read_bytes()
    victim.write_bytes(good + b"x")
    check(run() == 1, "a changed .sdkmod fails")
    victim.write_bytes(good)
    (rel / "SHA256SUMS.txt").write_text("0" * 64 + " *BL4FixSetup.exe\n", encoding="utf-8")
    check(run() == 1, "a wrong SHA256SUMS line fails")

    if all(results):
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"\n{sum(results)}/{len(results)} passed")
        return 0
    # Building from the live tree twice: another session editing a mod in between fails the verify checks
    print(f"\n{sum(results)}/{len(results)} passed  (kept for a look: {tmp})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
