"""
The logo movies, moved aside so the startup movie player has nothing to play for them.

The game's DefaultGame.ini (inside its paks) lists the movies played at launch:

    [/Script/MoviePlayer.MoviePlayerSettings]
    +StartupMovies=2KLOGO
    +StartupMovies=GBXLOGO
    +StartupMovies=Loading

Changing that list doesn't work: this shipping build regenerates Saved/Config/Windows/Game.ini at
startup without reading it (the file is deleted, and the loaded settings keep the default list), and
mods load after the logos have played. So we rename the two logo files (loose .bk2 files under
Content/Movies, not paks) to "<name>.bk2.skipped", the same thing PCGamingWiki suggests by hand.
Loading.bk2 stays: it loops until the engine is ready, so it covers the load rather than delaying it.

A Steam file check or game update puts the originals back; skipping again moves the new copies
aside (keeping the newest). restore() puts everything back as it was.

Plain Python (no SDK imports): the installer bundles this file too.
"""

from __future__ import annotations

import os
from pathlib import Path

LOGOS = ("2KLOGO", "GBXLOGO")
SUFFIX = ".skipped"


def _paths(game_dir: Path, name: str) -> tuple[Path, Path]:
    movie = game_dir / "OakGame" / "Content" / "Movies" / f"{name}.bk2"
    return movie, movie.with_name(movie.name + SUFFIX)


def skip(game_dir: Path) -> list[str]:
    """Move the logo movies aside. Returns the names moved now."""
    moved = []
    for name in LOGOS:
        movie, parked = _paths(game_dir, name)
        if movie.exists():
            os.replace(movie, parked)  # over an older parked copy: keep the game's newest
            moved.append(name)
    return moved


def restore(game_dir: Path) -> list[str]:
    """Put the logo movies back. Returns the names restored now."""
    restored = []
    for name in LOGOS:
        movie, parked = _paths(game_dir, name)
        if not parked.exists():
            continue
        if movie.exists():
            parked.unlink()  # the game already has its own copy back (file check, update)
        else:
            os.replace(parked, movie)
            restored.append(name)
    return restored


def skipped(game_dir: Path) -> bool:
    """True if no logo movie is left for the game to play."""
    return all(not _paths(game_dir, name)[0].exists() for name in LOGOS)
