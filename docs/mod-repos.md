# Mod repositories (for modders)

Players of the BL4 Mod Menu can add your repository once (MODS > Repositories: type or paste
the link, or `modrepo add <link>` in the console). From then on your mods show on their
Discover page with your pictures, install with one click, and update from their Updates page,
all **without restarting the game**. Any SDK mod works: it doesn't need to know about the Mod Menu.

The code is `mods/bl4_mod_menu/repos.py` (repositories) and `hotload.py` (loading without a restart).

## Three ways to publish

### 1. GitHub releases, nothing else (zero effort)

Attach your `<module>.sdkmod` files to a GitHub release. Players add `owner/name` (or the
repository's github.com link). The Mod Menu lists every `.sdkmod` on your **newest** release, uses
the release tag as the version (`v1.2.0`) and GitHub's SHA-256 digest of each file. No pictures or
descriptions in this mode, and it uses GitHub's API (60 requests an hour per player, plenty for a
check at launch).

### 2. GitHub releases with a manifest (recommended)

Add a `manifest.json` to the same release. It gives each mod a name, description, pictures and a
real version, and needs no API calls:

```
python tools/make_repo.py dist/*.sdkmod --tag v1.2.0 --name "My Mods" --out dist/manifest.json
gh release create v1.2.0 dist/*.sdkmod dist/manifest.json
```

The Mod Menu reads `https://github.com/<owner>/<name>/releases/latest/download/manifest.json`, so
the manifest must be on the newest (non-prerelease) release, and so must every file it names as an
`asset`. A mod you didn't rebuild can point at an older release with a full `url` instead.

### 3. An index anywhere on https

Host the same JSON anywhere (GitHub Pages, `raw.githubusercontent.com`, your site) and have players
add its https link. `asset` names resolve relative to the index's link; `url` takes a full link.
`make_repo.py --base-url https://example.com/mods/` writes full links.

## The format

```json
{
  "format": 1,
  "release": "v1.2.0",
  "name": "My Mods",
  "description": "One or two sentences on the Repositories page.",
  "homepage": "https://github.com/me/my-mods",
  "mods": {
    "my_mod": {
      "name": "My Mod",
      "version": "1.2.0",
      "asset": "my_mod.sdkmod",
      "sha256": "<64 hex chars>",
      "size": 12345,
      "author": "me",
      "blurb": "One line for the Discover list.",
      "description": "The full text on the mod's Discover page.",
      "notes": "What's new in this version.",
      "icon": "icon.png",
      "images": ["shots/1.jpg", "shots/2.jpg"],
      "homepage": "https://www.nexusmods.com/borderlands4/mods/123",
      "games": ["BL4"]
    }
  }
}
```

- The key (`my_mod`) is the module name: the `.sdkmod` must be a zip with exactly one folder of
  that name holding `__init__.py` (the SDK's own rule). The download is refused otherwise.
- `version` must match the `pyproject.toml` version inside the file (checked after download).
  Players get an update when the number goes up.
- `sha256` and `size` are checked when present. Use them: they turn a broken or swapped
  download into a clear error instead of a broken mod.
- `icon` and `images`: PNG or JPEG, https, at most 3 MB and 4096 px a side, up to 6 images
  (16:9 fills the Discover page; square icons, 128-256 px). Relative links resolve against the
  index's link. Pictures download only when a player looks at your mod, and are cached.
- `games`: leave it out or include `"BL4"`; mods for other games are hidden.
- Everything is optional except `name`, `version` and `asset`/`url`. Unknown fields are ignored,
  so the format can grow.

`make_repo.py` reads `name`, `version`, `description` and `authors` from each mod's
`pyproject.toml`, and `blurb`, `icon`, `images`, `homepage`, `notes` from a `[tool.bl4_mod_menu]`
table in the same file.

## Your mod's icon in the mod list

Put a PNG named `icon.png` (square, 128-256 px, at most 1 MB) in your mod's folder, next to
`__init__.py`. The Mod Menu shows it next to your mod's name, installed from a repository or not.

## Pictures on your mod's own page

If your mod has a page in the Mod Menu (an extension: see
`mods/bl4_mod_menu/extensions.py`), `api.image(src, cssClass)` returns a div showing a picture scaled to fit: a `coui://` link
to a loose file you publish, a `Texture2D'...'` asset path, or an https link the Mod Menu
downloads once and caches. `window.bl4ModMenu.picture(src, cls)` is the same outside a page.

## What players see and what's safe

- Adding a repository shows its name and mods first and asks for an OK. The page says that mods
  run code on their PC like any mod.
- A mod belongs to the repository it was installed from: another repository listing the same
  module name can't replace it, and nobody's repository can replace BL4 Fix's mods. Removing a
  repository keeps its mods (without updates).
- Install writes `sdk_mods/<module>.sdkmod` and loads it at once, turned on. Update stages the new
  file in `sdk_mods/.bl4_updates/` and reloads the mod from there (the loaded file is never written
  over while the game runs); the file moves into place when the game closes. Remove turns the mod
  off at once and deletes its file when the game closes.
- A reload turns your mod off (`on_disable`), forgets its modules and imports the new copy. Clean
  up in `on_disable` (threads, hooks you added yourself, objects you spawned) and reloads just
  work. If the new copy fails to import, the old one is put back and keeps running. Mods other
  mods import from, and libraries, update at the next start instead.
