"""
Boss pictures without spawning the boss (2026-11-02, in progress).

The old bake (bake.py) spawns each boss for real and photographs it: AI, scripts and arena logic
come with it, it crashed the game during development, and players can't be asked to run it. Here
the boss is rebuilt as plain meshes on a helper actor that nothing in the game knows about:

- the game merges a boss's modular parts itself: a GbxSkeletalMeshComponent given the boss's
  GestaltPartList (and seed) builds the same `Transient_Gestalt_*` mesh the boss gets (verified on
  Idolator Sol: same skeleton, bounds and 16 material slots);
- the pose is a recorded one: component-space bone transforms applied to a hidden
  PoseableMeshComponent that the visible mesh follows (SetLeaderPoseComponent). A boss's animation
  blueprint can't pose a bare component (its animations come from sets its character provides:
  it stays in the bind pose), and a pose needs no animation code at all;
- materials: each slot's material, plus the parameters the boss's dynamic copies override
  (colorization from its NCS material parts).

A recipe is recorded once per game version on the dev machine, from a boss the old bake spawned
(`record`), and holds only asset paths and numbers. tools/gen_booth_recipes.py turns a recording
into booth.jsonl (shipped with the mod, pinned to the game build it was recorded on). Players'
games read it (`read_recipes`), build the booth (`build`) and photograph it with bake.py's
camera, light and passes.

Game thread only, but for `read_recipes` / `game_build` (plain file reads, any thread). Plain
unrealsdk; no hooks.
"""

from __future__ import annotations

import ctypes
import json
import math
import time
from importlib import resources
from typing import Any

import unrealsdk
from unrealsdk.unreal import BoundFunction, UObject, WeakPointer

RECIPE_VERSION = 1
# Attached actors left out of the picture: the breakable crystals of Ordinite-infused and corrupted
# bosses (IO_OrdiniteCrystal*, IO_CorruptionCrystal_*) are real and that big, but they bury the boss
# (Sturm, Drang: verified against the real boss photographed with its attachments, 2026-11-03)
# Thrown things a boss holds when recorded leave too (Gilgathresh's fishing-net hook, OakProjectile)
SKIP_ACTORS = frozenset({"OakInteractiveObject", "OakProjectile"})
# ... but a melee weapon held in the hand is a projectile actor as well, its mesh component named
# for the hand's weapon slot (Bone Boogie's machetes, Mimicron's / Vulkorr's spears, CL34N-R's club,
# Crackclaw's claw, Arjay's dagger), unlike the bombs, molotovs, nets, hooks and effect meshes
# (StaticMeshComponentN): kept (the user's choice, 2026-11-02). The shipped pictures (real spawns)
# showed neither: KEEP_HELD = False drops both, as they did
HELD_WEAPONS = frozenset({"BuzzAxe", "DualBlade", "HeldClaw", "PrisonBuddyDagger"})
KEEP_HELD = True
SPACE_COMPONENT = 2  # ERelativeTransformSpace::RTS_Component
# Skinned booth meshes' LOD (SetForcedLOD: 1 = LOD 0, 0 = the engine picks): LODs follow the
# player's camera 200 m below, and every booth mesh sat at LOD 3 (Bio-Bulkhead had black patches)
FORCED_LOD = 1
BONE_SPACE_COMPONENT = 1  # EBoneSpaces::ComponentSpace
RECIPES_FILE = "booth.jsonl"  # tools/gen_booth_recipes.py
RECIPES_FORMAT = 1  # its header's "format"


def skipped(piece: dict[str, Any]) -> bool:
    """Whether a recorded piece stays out of the booth (SKIP_ACTORS, HELD_WEAPONS)."""
    actor = piece.get("actor")
    if actor == "OakProjectile" and KEEP_HELD and piece.get("name") in HELD_WEAPONS:
        return False
    return actor in SKIP_ACTORS


# region Helpers


def _path(obj: UObject | None) -> str | None:
    return obj._path_name() if obj is not None else None  # noqa: SLF001


def _is_asset(path: str | None) -> bool:
    """A saved asset (loadable by path), not something made at runtime (Transient_Gestalt_*, MIDs)."""
    return bool(path) and path.startswith(("/Game/", "/Engine/")) and ":" not in path and "Transient" not in path  # type: ignore[union-attr,operator]


def load(path: str) -> UObject | None:
    """Loads an asset by object path (blocking), e.g. `/Game/X/Y.Y`."""
    lib = unrealsdk.find_class("AssetRegistryHelpers").ClassDefaultObject
    registry = lib.GetAssetRegistry()
    get = BoundFunction(unrealsdk.find_class("AssetRegistry")._find("GetAssetByObjectPath"), registry)  # noqa: SLF001
    result = lib.GetAsset(get(path, False))
    return result[0] if isinstance(result, tuple) else result


def _ret(value: Any) -> Any:
    """A UFunction's return value (calls with out params return a tuple, the return value first)."""
    return value[0] if isinstance(value, tuple) else value


def _xf_list(xf: Any) -> list[float]:
    xf = _ret(xf)
    t, r, s = xf.Translation, xf.Rotation, xf.Scale3D
    out = [round(t.X, 2), round(t.Y, 2), round(t.Z, 2), round(r.X, 5), round(r.Y, 5), round(r.Z, 5), round(r.W, 5)]
    if max(abs(s.X - 1), abs(s.Y - 1), abs(s.Z - 1)) > 1e-4:
        out += [round(s.X, 4), round(s.Y, 4), round(s.Z, 4)]
    return out


def _xf(v: list[float]) -> Any:
    sx, sy, sz = (v[7], v[8], v[9]) if len(v) >= 10 else (1.0, 1.0, 1.0)
    return unrealsdk.make_struct(
        "Transform",
        Translation=unrealsdk.make_struct("Vector", X=v[0], Y=v[1], Z=v[2]),
        Rotation=unrealsdk.make_struct("Quat", X=v[3], Y=v[4], Z=v[5], W=v[6]),
        Scale3D=unrealsdk.make_struct("Vector", X=sx, Y=sy, Z=sz),
    )


def game_build() -> str:
    """
    The running game's build: its exe's PE timestamp and image size (the same as gamekeys' and
    tools/game_pins.json's `pe_stamp`). Reads the loaded exe's own header (any thread).
    """
    k32 = ctypes.WinDLL("kernel32")  # our own object: shared windll prototypes stay untouched
    k32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    k32.GetModuleHandleW.restype = ctypes.c_void_p
    base = k32.GetModuleHandleW(None)
    pe = base + ctypes.c_uint32.from_address(base + 0x3C).value
    stamp = ctypes.c_uint32.from_address(pe + 8).value  # FileHeader.TimeDateStamp
    size = ctypes.c_uint32.from_address(pe + 0x50).value  # OptionalHeader.SizeOfImage
    return f"{stamp:08x}-{size:x}"


def read_recipes(boss_ids: list[str] | None = None, *, text: str | None = None,
                 build: str | None = None) -> tuple[dict[str, Any], str]:  # fmt: skip
    """
    The shipped recipes (booth.jsonl: a header line, then one boss a line), only these bosses' parsed
    (each line ~1 ms; the whole file is ~5 MB). Returns ({boss id: recipe}, "") or ({}, why not):
    recipes recorded on another game build aren't used (a patch can move or change any asset they
    name: the booth steps aside until they're recorded again). Worker thread: no unreal objects.
    `text` / `build`: tests.
    """
    if text is None:
        try:
            text = resources.files(__package__).joinpath(RECIPES_FILE).read_text(encoding="utf-8")
        except (OSError, ValueError, TypeError):
            return {}, "no booth recipes in this copy of the mod"
    lines = text.splitlines()
    try:
        header = json.loads(lines[0]) if lines else {}
    except ValueError:
        header = {}
    if not isinstance(header, dict) or header.get("format") != RECIPES_FORMAT:
        return {}, "the booth recipes are in a format this version doesn't read"
    here = build if build is not None else game_build()
    if header.get("build") != here:
        return {}, f"the booth recipes are for game build {header.get('build')}, this is {here}: they need recording again"
    wanted = set(boss_ids) if boss_ids is not None else None
    out: dict[str, Any] = {}
    for line in lines[1:]:
        # Each line starts {"id":"<boss id>", (the generator writes it first): read the id before parsing
        if not line.startswith('{"id":"'):
            continue
        boss_id = line[7 : line.find('"', 7)]
        if wanted is not None and boss_id not in wanted:
            continue
        try:
            recipe = json.loads(line)
        except ValueError:
            continue
        if isinstance(recipe, dict) and recipe.get("v") == RECIPE_VERSION:
            out[boss_id] = recipe
    return out, ""


# endregion
# region Recording (dev machine, from a boss the old bake spawned)


def _materials(comp: UObject) -> list[dict[str, Any]]:
    out = []
    for mat in comp.GetMaterials():
        if mat is None:
            out.append({})
            continue
        entry: dict[str, Any] = {}
        if mat.Class.Name == "MaterialInstanceDynamic":
            entry["mat"] = _path(mat.Parent)
            # Each parameter as [name, association, layer index, value]: the enemy colorization
            # colours are layer parameters (association 1, index 2 on MI_Psycho_Outfit), and set by
            # name alone they land on an unused global parameter (Hank's pants stayed teal)
            vec = []
            for p in mat.VectorParameterValues:
                c, info = p.ParameterValue, p.ParameterInfo
                vec.append([str(info.Name), int(info.Association), int(info.Index), [round(c.R, 5), round(c.G, 5), round(c.B, 5), round(c.A, 5)]])
            sca = [[str(p.ParameterInfo.Name), int(p.ParameterInfo.Association), int(p.ParameterInfo.Index), round(p.ParameterValue, 5)] for p in mat.ScalarParameterValues]
            tex = [[str(p.ParameterInfo.Name), int(p.ParameterInfo.Association), int(p.ParameterInfo.Index), _path(p.ParameterValue)]
                   for p in mat.TextureParameterValues if _is_asset(_path(p.ParameterValue))]  # fmt: skip
            if vec:
                entry["vec"] = vec
            if sca:
                entry["sca"] = sca
            if tex:
                entry["tex"] = tex
        else:
            entry["mat"] = _path(mat)
        if not _is_asset(entry.get("mat")):
            entry = {}
        out.append(entry)
    return out


def _pose(comp: UObject) -> list[list[Any]]:
    out = []
    for i in range(comp.GetNumBones()):
        name = comp.GetBoneName(i)
        out.append([str(name), *_xf_list(comp.GetSocketTransform(name, SPACE_COMPONENT))])
    return out


DEBUG = False  # dev: each recorded piece also gets "dbg", the flags that can hide it in game


def _debug(actor: UObject, comp: UObject) -> dict[str, Any]:
    out: dict[str, Any] = {"actor": actor.Class.Name, "actor_name": actor.Name}
    for obj, names in ((actor, ("bHidden",)), (comp, (
        "bRenderInMainPass", "bRenderInDepthPass", "bVisibleInSceneCaptureOnly", "bHiddenInSceneCapture",
        "bOwnerNoSee", "bOnlyOwnerSee", "LDMaxDrawDistance", "CachedMaxDrawDistance", "BoundsScale",
        "bUseAsOccluder", "bVisibleInReflectionCaptures", "bVisibleInRayTracing"))):  # fmt: skip
        for n in names:
            try:
                v = getattr(obj, n)
                out[n] = v if isinstance(v, (bool, int, float, str)) else str(v)
            except Exception:  # noqa: BLE001
                pass
    for call in ("IsVisible", "IsRegistered"):
        try:
            out[call] = bool(_ret(getattr(comp, call)()))
        except Exception:  # noqa: BLE001
            pass
    try:
        data = comp.CustomPrimitiveData.Data
        out["cpd"] = [round(data[i], 4) for i in range(len(data))]
    except Exception:  # noqa: BLE001
        pass
    try:
        parent = comp.AttachParent
        out["parent"] = parent.Name if parent is not None else None
        out["socket"] = str(comp.AttachSocketName)
        rs = comp.RelativeScale3D
        out["rel_scale"] = [round(rs.X, 3), round(rs.Y, 3), round(rs.Z, 3)]
        out["abs_scale"] = bool(comp.bAbsoluteScale)
    except Exception:  # noqa: BLE001
        pass
    try:
        out["mat_classes"] = [m.Class.Name if m is not None else None for m in comp.GetMaterials()]
    except Exception:  # noqa: BLE001
        pass
    return out


def record(actors: list[UObject]) -> dict[str, Any]:
    """
    The recipe for a boss standing posed (the old bake's actor and its riders): every visible mesh
    component with its asset or gestalt inputs, its place relative to the first actor, its materials
    and (skeletal meshes) its pose.
    """
    kml = unrealsdk.find_class("KismetMathLibrary").ClassDefaultObject
    mesh_cls = unrealsdk.find_class("MeshComponent")
    gestalt_cls = unrealsdk.find_class("GbxSkeletalMeshComponent")
    skinned_cls = unrealsdk.find_class("SkinnedMeshComponent")
    static_cls = unrealsdk.find_class("StaticMeshComponent")
    root_xf = _ret(actors[0].GetTransform())
    pieces = []
    skipped = []
    # Plus what's attached to them (Saddleback's rider is a child actor on the boss; the scene
    # capture draws child actors with their parent, so they're in the old pictures)
    everything: list[UObject] = []
    for actor in actors:
        attached = actor.GetAttachedActors([], True, True)  # (Ellipsis, [actors]): no return value
        for one in [actor, *(attached[1] if isinstance(attached, tuple) else attached)]:
            if one not in everything:
                everything.append(one)
    for actor in everything:
        for comp in actor.K2_GetComponentsByClass(mesh_cls):
            if not comp.bVisible or comp.bHiddenInGame:
                continue
            piece: dict[str, Any] = {"name": comp.Name, "actor": actor.Class.Name}
            if comp.Class._inherits(skinned_cls):  # noqa: SLF001
                asset = _path(comp.SkinnedAsset)
                if comp.Class._inherits(gestalt_cls) and not _is_asset(asset):  # noqa: SLF001
                    parts = comp.GestaltMeshParts
                    from_list = comp.ResolvedPartListMeshParts
                    piece["kind"] = "gestalt"
                    piece["partlist"] = _path(comp.GestaltPartList)
                    piece["parts"] = [_path(parts[i]) for i in range(len(parts))]
                    # The ones the part list gave (the rest came from the character's actor parts)
                    piece["from_list"] = [_path(from_list[i]) for i in range(len(from_list))]
                    piece["seed"] = int(comp.GestaltSeed)
                    piece["slots"] = len(comp.GetMaterials())
                elif _is_asset(asset):
                    piece["kind"] = "skel"
                    piece["mesh"] = asset
                else:
                    skipped.append(f"{actor.Name}.{comp.Name}: mesh {asset}")
                    continue
                piece["pose"] = _pose(comp)
            elif comp.Class._inherits(static_cls):  # noqa: SLF001
                mesh = _path(comp.StaticMesh)
                if not _is_asset(mesh):
                    skipped.append(f"{actor.Name}.{comp.Name}: static {mesh}")
                    continue
                piece["kind"] = "static"
                piece["mesh"] = mesh
            else:
                skipped.append(f"{actor.Name}.{comp.Name}: {comp.Class.Name}")
                continue
            piece["xf"] = _xf_list(kml.MakeRelativeTransform(_ret(comp.K2_GetComponentToWorld()), root_xf))
            piece["mats"] = _materials(comp)
            if DEBUG:
                piece["dbg"] = _debug(actor, comp)
            pieces.append(piece)
    return {"v": RECIPE_VERSION, "pieces": pieces, "skipped": skipped}


# endregion
# region Building (players' games)


class Booth:
    """A rebuilt boss: one plain actor holding a component per recorded piece."""

    def __init__(self) -> None:
        self.actor = WeakPointer()
        self.missing: list[str] = []
        self.cost_ms = 0.0
        self.posed: list[tuple[WeakPointer, list[list[Any]]]] = []  # each posed component and its pose
        self.reposed = False

    def repose(self) -> None:
        """
        The pose again, once the forced LOD 0 is in use (see _apply_pose): at least a few frames
        after build, and a frame or more before the first capture (bones refresh at frame end).
        """
        self.reposed = True
        for comp, pose in self.posed:
            c = comp()
            if c is not None:
                _apply_pose(c, pose)

    def destroy(self) -> None:
        actor = self.actor()
        self.actor = WeakPointer()
        if actor is not None:
            actor.K2_DestroyActor()


def _add(actor: UObject, cls_name: str, xf: Any, setup: Any = None) -> UObject:
    res = actor.AddComponentByClass(unrealsdk.find_class(cls_name), False, xf, True)
    comp = res[0] if isinstance(res, tuple) else res
    if setup is not None:
        setup(comp)
    actor.FinishAddComponent(comp, False, xf)
    # Ticks with the game paused too (solo pause menu, title screen): a posed mesh's bones and its
    # forced LOD only come in on its tick
    comp.SetTickableWhenPaused(True)
    return comp


def _apply_pose(poseable: UObject, pose: list[list[Any]]) -> None:
    """
    The recorded pose, bone by bone in component space. A bone set in component space is made
    relative to its parent's pose as computed from the bones the component's current LOD uses: the
    hidden leader (tiny bind-pose bounds, 200 m from the camera) sat at a low LOD and every bone
    that LOD leaves out came out wrong (Kraggula's bat collapsed to 3 cm, too small to draw). So
    build() forces LOD 0, but when the forced LOD takes effect (a frame or so later) the bones go
    back to the reference pose (Idolator Sol and King Crosis stood in an A-pose, 2026-11-02): the
    pose is set again once it has (Booth.repose, from the bake's settle wait).
    """
    for bone in pose:
        poseable.SetBoneTransformByName(bone[0], _xf(bone[1:]), BONE_SPACE_COMPONENT)


def _fit_bounds(comp: UObject, pose: list[list[Any]]) -> None:
    """
    A posed booth mesh still reports the bounds of its bind pose (it has no physics asset), and the
    renderer culls by them and picks its LOD by them. Kraggula's bat (a 0.01-scale mesh whose posed
    bones reach ~200 units out) reported a 3-unit box and wasn't drawn. Scales the bounds up to
    cover the recorded bones.
    """
    reach = max((math.sqrt(b[1] * b[1] + b[2] * b[2] + b[3] * b[3]) for b in pose), default=0.0)
    s = comp.K2_GetComponentScale()
    reach *= max(abs(s.X), abs(s.Y), abs(s.Z))
    ksl = unrealsdk.find_class("KismetSystemLibrary").ClassDefaultObject
    found = ksl.GetComponentBounds(comp, unrealsdk.make_struct("Vector"), unrealsdk.make_struct("Vector"), 0.0)
    origin, radius = found[1], found[3]
    here = comp.K2_GetComponentLocation()
    off = math.dist((origin.X, origin.Y, origin.Z), (here.X, here.Y, here.Z))
    if radius > 0 and reach + off > radius:
        comp.SetBoundsScale(min((reach + off) * 1.1 / radius, 1000.0))


def _apply_materials(comp: UObject, mats: list[dict[str, Any]], booth: Booth) -> None:
    for i, entry in enumerate(mats):
        path = entry.get("mat")
        if not path:
            continue
        mat = load(path)
        if mat is None:
            booth.missing.append(path)
            continue
        if not (entry.get("vec") or entry.get("sca") or entry.get("tex")):
            comp.SetMaterial(i, mat)
            continue
        mid = comp.CreateDynamicMaterialInstance(i, mat, "None")

        def info(name: str, association: int, index: int) -> Any:
            return unrealsdk.make_struct("MaterialParameterInfo", Name=name, Association=association, Index=index)

        for name, association, index, c in entry.get("vec", []):
            color = unrealsdk.make_struct("LinearColor", R=c[0], G=c[1], B=c[2], A=c[3])
            mid.SetVectorParameterValueByInfo(info(name, association, index), color)
        for name, association, index, v in entry.get("sca", []):
            mid.SetScalarParameterValueByInfo(info(name, association, index), v)
        for name, association, index, tex_path in entry.get("tex", []):
            tex = load(tex_path)
            if tex is not None:
                mid.SetTextureParameterValueByInfo(info(name, association, index), tex)
            else:
                booth.missing.append(tex_path)


def build(recipe: dict[str, Any], context: UObject, where: tuple[float, float, float], yaw: float = 0.0) -> Booth:
    """
    Builds the booth for a recipe at `where` (turned by `yaw` degrees). Everything is created and
    referenced in this call (component properties hold every loaded asset), so nothing loaded
    can be collected before it's used.
    """
    started = time.perf_counter()
    booth = Booth()
    gs = unrealsdk.find_class("GameplayStatics").ClassDefaultObject
    half = math.radians(yaw) / 2
    xf = unrealsdk.make_struct(
        "Transform",
        Translation=unrealsdk.make_struct("Vector", X=where[0], Y=where[1], Z=where[2]),
        Rotation=unrealsdk.make_struct("Quat", Z=math.sin(half), W=math.cos(half)),
        Scale3D=unrealsdk.make_struct("Vector", X=1, Y=1, Z=1),
    )
    spawned = gs.BeginDeferredActorSpawnFromClass(context, unrealsdk.find_class("Actor"), xf, 1, None, 0)
    actor = spawned[0] if isinstance(spawned, tuple) else spawned
    gs.FinishSpawningActor(actor, xf, 0)
    actor.SetTickableWhenPaused(True)
    booth.actor = WeakPointer(actor)
    # The root carries the booth's place: a plain Actor has no root when spawned, so the spawn
    # transform is dropped (the first booths stood at the world origin)
    _add(actor, "SceneComponent", xf)
    for piece in recipe["pieces"]:
        if skipped(piece):
            continue
        rel = _xf(piece["xf"])
        kind = piece["kind"]
        if kind == "static":
            mesh = load(piece["mesh"])
            if mesh is None:
                booth.missing.append(piece["mesh"])
                continue
            comp = _add(actor, "StaticMeshComponent", rel, lambda c, m=mesh: c.SetStaticMesh(m))
            comp.SetForcedLodModel(1)
        elif kind == "skel":
            mesh = load(piece["mesh"])
            if mesh is None:
                booth.missing.append(piece["mesh"])
                continue
            comp = _add(actor, "PoseableMeshComponent", rel)
            comp.SetSkinnedAssetAndUpdate(mesh, True)
            comp.SetForcedLOD(FORCED_LOD)  # (see _apply_pose)
            _apply_pose(comp, piece["pose"])
            booth.posed.append((WeakPointer(comp), piece["pose"]))
            _fit_bounds(comp, piece["pose"])
        else:  # gestalt
            plist = load(piece["partlist"]) if piece.get("partlist") else None
            if piece.get("partlist") and plist is None:
                booth.missing.append(piece["partlist"])
                continue
            # Parts the part list doesn't give (actor parts; all of them for most human bosses, which
            # have no part list) go in as external gestalt meshes, merged at once
            from_list = set(piece.get("from_list", [])) if plist is not None else set()
            extra = [p for p in piece.get("parts", []) if p not in from_list]
            parts = [load(p) for p in extra]
            if None in parts:
                booth.missing += [p for p, m in zip(extra, parts, strict=True) if m is None]
                parts = [m for m in parts if m is not None]

            def setup(c: UObject, plist: UObject | None = plist, seed: int = piece.get("seed", 0)) -> None:
                c.GestaltPartList = plist
                c.GestaltSeed = seed
                c.VisibilityBasedAnimTickOption = 0

            comp = _add(actor, "GbxSkeletalMeshComponent", rel, setup)
            if parts:
                # Merged at once (Hank the Welder: 12 parts -> 275 bones, 15 slots, as on the boss)
                comp.AddExternalGestaltMeshes_BP(parts)
            if comp.SkinnedAsset is None:
                booth.missing.append(f"gestalt {piece['name']}")
                continue
            slots = len(comp.GetMaterials())
            if piece.get("slots") and slots != piece["slots"]:
                # A different merge: recorded materials would land on the wrong sections
                booth.missing.append(f"gestalt {piece['name']}: {slots} material slots, recorded {piece['slots']}")
            leader = _add(actor, "PoseableMeshComponent", rel)
            leader.SetSkinnedAssetAndUpdate(comp.SkinnedAsset, True)
            leader.SetHiddenInGame(True, False)
            leader.SetForcedLOD(FORCED_LOD)  # the pose is the leader's: its LOD too (see _apply_pose)
            comp.SetLeaderPoseComponent(leader, True, False)
            _apply_pose(leader, piece["pose"])
            booth.posed.append((WeakPointer(leader), piece["pose"]))
            _fit_bounds(comp, piece["pose"])
            comp.SetForcedLOD(FORCED_LOD)
        _apply_materials(comp, piece.get("mats", []), booth)
    booth.cost_ms = (time.perf_counter() - started) * 1000
    return booth


# endregion
