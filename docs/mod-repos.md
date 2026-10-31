# Mod repositories (for modders)

Players of the BL4 Mod Menu can add your repository once (MODS > Repositories: type or paste
the link, or `modrepo add <link>` in the console). From then on your mods show on their
Discover page with your pictures, install with one click, and update from their Updates page,
all **without restarting the game**. Any SDK mod works: it doesn't need to know about the Mod Menu.

The code is `mods/bl4_mod_menu/repos.py` (repositories) and `hotload.py` (loading without a restart).

**Everything has to be on GitHub**: your repository or index file, every download, every picture
and every link (`homepage` too). The Mod Menu connects to `github.com` (and its subdomains, such as
`api.github.com`) and `*.githubusercontent.com` (where GitHub serves release files and raw files)
and nothing else, also after a redirect. A link anywhere else is dropped.

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

### 3. An index file in a GitHub repository

Commit the same JSON to a repository and have players add its raw link
(`https://raw.githubusercontent.com/<owner>/<name>/<branch>/index.json`). `asset` names resolve
relative to the index's link; `url` takes a full link (on GitHub). `make_repo.py --base-url
https://raw.githubusercontent.com/<owner>/<name>/<branch>/` writes full links. GitHub Pages
(`*.github.io`) and other sites aren't accepted.

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
      "homepage": "https://github.com/me/my-mods",
      "games": ["BL4"]
    }
  }
}
```

- The key (`my_mod`) is the module name: the `.sdkmod` must be a zip with exactly one folder of
  that name holding `__init__.py` (the SDK's own rule). The download is refused otherwise.
- `version` must match the `pyproject.toml` version inside the file (checked after download).
  Players get an update when the number goes up.
- **`sha256` is required** (since Mod Menu 0.2.3): a mod without a valid one (64 hex characters)
  is left out of your repository's list, and the player's log names it once per check
  ("left out N mods without a SHA-256 checksum"). `make_repo.py` writes it. In the no-manifest
  mode (1.) it's GitHub's own `digest` of each asset; an asset GitHub gives no digest for is left
  out too (older uploads may lack one: upload the file again). `size` is checked when present.
- `icon` and `images`: PNG or JPEG on GitHub (a release asset or a file in a repository), at most 3 MB and 4096 px a side, up to 6 images
  (16:9 fills the Discover page; square icons, 128-256 px). Relative links resolve against the
  index's link. Pictures download only when a player looks at your mod, and are cached.
- `games`: leave it out or include `"BL4"`; mods for other games are hidden.
- Everything is optional except `name`, `version`, `sha256` and `asset`/`url`. Unknown fields are ignored,
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
to a loose file you publish, a `Texture2D'...'` asset path, or a GitHub link the Mod Menu
downloads once and caches. `window.bl4ModMenu.picture(src, cls)` is the same outside a page.

## What the Mod Menu refuses in an index

Your index is data, never code or markup: the page shows every field as plain text. These are
dropped or refused, so check them if something of yours doesn't show up:

- Mod keys that aren't plain module names (`[A-Za-z_][A-Za-z0-9_]*`, 64 chars), or that are taken:
  the SDK's and the Mod Menu's own, BL4 Fix's mods (`bl4_reroll`, ...: pick your own prefix),
  Python's standard modules (`json`, `ctypes`, ...) and Windows device names (`con`, `nul`, ...).
  Compared without case.
- `asset` names with anything but letters, digits, `.`, `_`, `+` and `-`, or starting with `.`
  (no folders, no `..`). Use `url` for a file elsewhere.
- Links (`url`, `homepage`, `icon`, `images`) that aren't https on GitHub (above), or that hold a
  `user:password@` part, spaces or invisible characters. Redirects are held to the same rule.
- Control characters and invisible ones (right-to-left overrides, zero-width spaces) in any text;
  names, authors, versions and blurbs become one line.
- A repository `name` containing "BL4 Fix": the Mod Menu shows your link instead.
- An index that isn't strict JSON (`NaN`, `Infinity`, absurd nesting) or is over 2 MB.
- Downloads whose zip holds anything outside the one `<module>/` folder, `..` / absolute /
  backslash / drive paths, links, the same name twice (any case), or more than 1 GB or 20,000
  files unpacked.

## What players see and what's safe

- Adding a repository shows its name, link and mods first and asks for an OK, **with a warning
  every time**: mods are Python programs with the game's rights on their PC, BL4 Fix doesn't check
  other modders' mods, and whoever controls the repository decides what installs and updates bring.
- Each repository has its own **Check for updates at launch** switch, off unless the player turns
  it on when adding it (or later on its row). Off, your repository is read only when they press
  Refresh, so they don't see your new mods or updates until then.
- A Discover page of a mod from your repository says it isn't from BL4 Fix and wasn't checked.
- **Players see what your mod's code uses before it installs.** Install (and Update) first
  downloads the file, checks it, and reads its `.py` files with Python's parser (nothing runs),
  then asks "Install X? It uses: ..." with Install / Cancel; the confirmed install uses that same
  download. An update shows the version and size change and only what's **new** since the copy
  they have (one question for Update all). What's listed:
  - *network*: `socket`, `urllib.request`, `http.client`, `requests`, `ssl`, `ftplib`, `smtplib`,
    `websocket(s)`, `aiohttp`, `httpx`, `webbrowser`, ... (`urllib.parse` alone doesn't count);
  - *runs programs*: `subprocess`, `multiprocessing`, `os.system` / `startfile` / `popen` /
    `exec*` / `spawn*`;
  - *native code*: `ctypes`, `cffi`, or a `.pyd` / `.dll` / `.so` / `.exe` file in the zip;
  - *Windows registry*: `winreg`;
  - *dynamic code*: `exec`, `eval`, `compile`, `marshal`, `runpy`, `__import__` or
    `importlib.import_module` with a name built at run time (a literal name counts as that import);
  - *unreadable code*: a `.py` Python can't parse, `.pyc` files, or more than 8 MB of source.

  It's a hint, and the page says so; but each kind you use is one more thing a careful player asks
  about. Ship plain source (no `.pyc`), keep imports at the top and literal, and say in your
  `description` why you need the network or a native library.
- **Your repository is pinned to GitHub's number for it.** When a player adds `owner/name` (or
  a raw.githubusercontent.com index in it), the Mod Menu saves the repository's permanent `id`
  from GitHub's API and checks it again on later reads (at most once an hour). If the link ever
  leads to a repository with a different `id`, your repository is **blocked** for that player: its
  row says the link now points to a different repository, its mods leave Discover and Updates,
  and nothing installs from it until they press Trust again. That's what happens when someone
  takes over a deleted or renamed account's name, but also if you **delete and recreate** the
  repository or **move an index to another repository**. Renaming or transferring a repository keeps
  its `id` (GitHub redirects), so that's safe. Offline or rate-limited checks never block.
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
