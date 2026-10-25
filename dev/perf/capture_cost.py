# Through the bridge (game thread): measures what a Minimap-style scene capture costs at a given
# render-target size and show-flag set, with a SECOND capture of our own (the Minimap's live capture
# is never touched: swapping a live capture's target crashed the game once). Pair with
# dev/perf/gpu_time.py running outside.
#   python tools/bridge.py <id> -f dev/perf/capture_cost.py
#   python tools/bridge.py <id> "print(cc_start(512, 'same'))"    # films every frame from now on
#   python tools/bridge.py <id> "print(cc_stop())"                # destroys it
# Modes: 'same' = the Minimap capture's own show flags; 'lean' = those plus everything a flat unlit
# map doesn't need.
# ruff: noqa: F821
import sys

_scene = sys.modules["bl4_minimap.scene"]
_CC = globals().setdefault("_CC", {})

LEAN_OFF = (
    "Lumen", "LumenGlobalIllumination", "LumenReflections", "DynamicShadows", "ContactShadows",
    "CapsuleShadows", "VirtualShadowMapPersistentData", "Translucency", "Fog", "VolumetricFog",
    "Decals", "AmbientOcclusion", "ScreenSpaceAO", "Bloom", "EyeAdaptation", "DepthOfField",
    "LightShafts", "SkyLighting", "Atmosphere", "Cloud", "ReflectionEnvironment",
    "ScreenSpaceReflections", "DistanceFieldAO", "GlobalIllumination", "Particles",
    "MotionBlur", "PostProcessMaterial", "LocalExposure", "ToneCurve", "Refraction",
    "SeparateTranslucency", "HZB" , "Landscape", "Volumes", "InstancedFoliage", "InstancedGrass",
    "Paper2DSprites", "TextRender", "Wireframe",
)  # fmt: skip


def cc_start(size: int, mode: str = "same") -> str:
    cc_stop()
    src = _scene.Scene.capture()
    if src is None:
        return "no minimap capture to copy"
    s = src.CaptureComponent2D
    pc = get_pc()
    cap = _scene._spawn(pc, unrealsdk.find_class("SceneCapture2D"), _scene.ORIGIN)
    c = cap.CaptureComponent2D
    krl = unrealsdk.find_class("KismetRenderingLibrary").ClassDefaultObject
    rt = krl.CreateRenderTarget2D(pc, size, size, _scene.RTF_RGBA8,
                                  unrealsdk.make_struct("LinearColor", A=1), False, False)
    rt.TargetGamma = _scene.TARGET_GAMMA
    c.TextureTarget = rt
    c.ProjectionType = s.ProjectionType
    c.CaptureSource = s.CaptureSource
    c.OrthoWidth = s.OrthoWidth
    flags = [unrealsdk.make_struct("EngineShowFlagsSetting", ShowFlagName=f.ShowFlagName, Enabled=f.Enabled)
             for f in s.ShowFlagSettings]
    off = LEAN_OFF if mode == "lean" else (tuple(mode) if isinstance(mode, (list, tuple)) else ())
    if off:
        flags = [f for f in flags if str(f.ShowFlagName) not in off]
        flags += [unrealsdk.make_struct("EngineShowFlagsSetting", ShowFlagName=n, Enabled=False) for n in off]
    c.SetShowFlagSettings(flags)
    c.PostProcessSettings = s.PostProcessSettings
    c.PostProcessBlendWeight = s.PostProcessBlendWeight
    c.PrimitiveRenderMode = s.PrimitiveRenderMode
    shown = list(s.ShowOnlyActors)
    if _CC.get("no_fog"):
        fog = sys.modules["bl4_minimap.fog"].Fog.actor()
        shown = [x for x in shown if x is not None and (fog is None or x._get_address() != fog._get_address())]
    c.ShowOnlyActors = shown
    cap.K2_SetActorLocationAndRotation(src.K2_GetActorLocation(), src.K2_GetActorRotation(), False,
                                       unrealsdk.make_struct("HitResult"), True)
    c.bCaptureOnMovement = False
    c.bCaptureEveryFrame = True
    _CC["actor"] = unrealsdk.unreal.WeakPointer(cap)
    return f"capturing every frame: {size}^2 {len(off)} extra off, ortho {c.OrthoWidth:.0f}"


def cc_stop() -> str:
    ptr = _CC.pop("actor", None)
    a = ptr() if ptr is not None else None
    if a is None:
        return "nothing running"
    a.CaptureComponent2D.bCaptureEveryFrame = False
    a.K2_DestroyActor()
    return "stopped"


print("capture_cost ready")


def cc_shot_setup(size: int, mode: str) -> str:
    """A capture that films only on request (for look / settle comparisons)."""
    msg = cc_start(size, mode)
    a = _CC["actor"]()
    a.CaptureComponent2D.bCaptureEveryFrame = False
    return msg


def cc_film(x: float, y: float, half: float, path_dir: str, name: str) -> str:
    """Films world point (x, y), `half` world units to the edge, north up, and exports a PNG."""
    a = _CC["actor"]()
    c = a.CaptureComponent2D
    north = sys.modules["bl4_minimap"].State.north_yaw or 270.0
    where = unrealsdk.make_struct("Vector", X=_scene.ORIGIN[0] + x * _scene.MAP_SCALE,
                                  Y=_scene.ORIGIN[1] + y * _scene.MAP_SCALE,
                                  Z=_scene.ORIGIN[2] + _scene.CAMERA_HEIGHT)
    rot = unrealsdk.make_struct("Rotator", Pitch=-90.0, Yaw=north, Roll=0.0)
    a.K2_SetActorLocationAndRotation(where, rot, False, unrealsdk.make_struct("HitResult"), True)
    c.OrthoWidth = 2 * half * _scene.MAP_SCALE
    c.CaptureScene()
    if name:
        unrealsdk.find_class("KismetRenderingLibrary").ClassDefaultObject.ExportRenderTarget(
            get_pc(), c.TextureTarget, path_dir, name)
    return "filmed"


def cc_fog_compose(cand: str, path_dir: str, name: str) -> str:
    """Copies the probe capture's picture into a new target, draws the Minimap's fog material over
    it with UVs for orientation `cand` ('xy', '-xy', 'x-y', '-x-y'), exports it."""
    a = _CC["actor"]()
    c = a.CaptureComponent2D
    src = c.TextureTarget
    fogm = sys.modules["bl4_minimap.fog"].Fog
    mid = fogm.plane().StaticMeshComponent.GetMaterial(0)
    pl = fogm.placement
    pc = get_pc()
    krl = unrealsdk.find_class("KismetRenderingLibrary").ClassDefaultObject
    size = src.SizeX
    rt = _CC.get("compose_rt")
    rt = rt() if rt is not None else None
    if rt is None or rt.SizeX != size:
        rt = krl.CreateRenderTarget2D(pc, size, size, _scene.RTF_RGBA8, unrealsdk.make_struct("LinearColor", A=1), False, False)
        rt.TargetGamma = _scene.TARGET_GAMMA
        c.PostProcessSettings  # noqa: B018
        _CC["compose_rt_hold"] = rt  # dev only: python ref (short test)
        _CC["compose_rt"] = unrealsdk.unreal.WeakPointer(rt)
    loc = a.K2_GetActorLocation()
    cx, cy = loc.X - _scene.ORIGIN[0], loc.Y - _scene.ORIGIN[1]  # map units
    half = c.OrthoWidth / 2
    span = 100 * pl["scale"][0]
    lx0, lx1 = (cx - half - pl["loc"][0]) / span + 0.5, (cx + half - pl["loc"][0]) / span + 0.5
    ly0, ly1 = (cy - half - pl["loc"][1]) / span + 0.5, (cy + half - pl["loc"][1]) / span + 0.5
    u0, u1 = (lx0, lx1) if "-x" not in cand else (1 - lx0, 1 - lx1)
    v0, v1 = (ly0, ly1) if "-y" not in cand else (1 - ly0, 1 - ly1)
    v2 = lambda x, y: unrealsdk.make_struct("Vector2D", X=x, Y=y)  # noqa: E731
    _, canvas, _, ctx = krl.BeginDrawCanvasToRenderTarget(pc, rt, None, unrealsdk.unreal.IGNORE_STRUCT, unrealsdk.unreal.IGNORE_STRUCT)
    canvas.K2_DrawTexture(src, v2(0, 0), v2(size, size), v2(0, 0), v2(1, 1), unrealsdk.make_struct("LinearColor", R=1, G=1, B=1, A=1), 0, 0.0, v2(0.5, 0.5))
    canvas.K2_DrawMaterial(mid, v2(0, 0), v2(size, size), v2(u0, v0), v2(u1 - u0, v1 - v0), 0.0, v2(0.5, 0.5))
    krl.EndDrawCanvasToRenderTarget(pc, ctx)
    krl.ExportRenderTarget(pc, rt, path_dir, name)
    return f"composed {cand} uv ({u0:.3f},{v0:.3f})-({u1:.3f},{v1:.3f})"


def cc_fog_layer(cand: str, path_dir: str, name: str, blend: int = 2) -> str:
    """Fog material -> its own transparent target, then that over the probe's picture with `blend`."""
    a = _CC["actor"]()
    c = a.CaptureComponent2D
    src = c.TextureTarget
    fogm = sys.modules["bl4_minimap.fog"].Fog
    mid = fogm.plane().StaticMeshComponent.GetMaterial(0)
    pl = fogm.placement
    pc = get_pc()
    krl = unrealsdk.find_class("KismetRenderingLibrary").ClassDefaultObject
    size = src.SizeX
    clear0 = unrealsdk.make_struct("LinearColor", R=0, G=0, B=0, A=0)
    layer = krl.CreateRenderTarget2D(pc, size, size, _scene.RTF_RGBA8, clear0, False, False)
    out = krl.CreateRenderTarget2D(pc, size, size, _scene.RTF_RGBA8, unrealsdk.make_struct("LinearColor", A=1), False, False)
    _CC["hold"] = (layer, out)
    krl.ClearRenderTarget2D(pc, layer, clear0)
    loc = a.K2_GetActorLocation()
    cx, cy = loc.X - _scene.ORIGIN[0], loc.Y - _scene.ORIGIN[1]
    half = c.OrthoWidth / 2
    span = 100 * pl["scale"][0]
    lx0, lx1 = (cx - half - pl["loc"][0]) / span + 0.5, (cx + half - pl["loc"][0]) / span + 0.5
    ly0, ly1 = (cy - half - pl["loc"][1]) / span + 0.5, (cy + half - pl["loc"][1]) / span + 0.5
    u0, u1 = (lx0, lx1) if "-x" not in cand else (1 - lx0, 1 - lx1)
    v0, v1 = (ly0, ly1) if "-y" not in cand else (1 - ly0, 1 - ly1)
    v2 = lambda x, y: unrealsdk.make_struct("Vector2D", X=x, Y=y)  # noqa: E731
    white = unrealsdk.make_struct("LinearColor", R=1, G=1, B=1, A=1)
    IG = unrealsdk.unreal.IGNORE_STRUCT
    _, canvas, _, ctx = krl.BeginDrawCanvasToRenderTarget(pc, layer, None, IG, IG)
    canvas.K2_DrawMaterial(mid, v2(0, 0), v2(size, size), v2(u0, v0), v2(u1 - u0, v1 - v0), 0.0, v2(0.5, 0.5))
    krl.EndDrawCanvasToRenderTarget(pc, ctx)
    krl.ExportRenderTarget(pc, layer, path_dir, "layer_" + name)
    _, canvas, _, ctx = krl.BeginDrawCanvasToRenderTarget(pc, out, None, IG, IG)
    canvas.K2_DrawTexture(src, v2(0, 0), v2(size, size), v2(0, 0), v2(1, 1), white, 0, 0.0, v2(0.5, 0.5))
    canvas.K2_DrawTexture(layer, v2(0, 0), v2(size, size), v2(0, 0), v2(1, 1), white, blend, 0.0, v2(0.5, 0.5))
    krl.EndDrawCanvasToRenderTarget(pc, ctx)
    krl.ExportRenderTarget(pc, out, path_dir, name)
    return "ok"


def cc_roundtrip(path_dir: str, png: str, out_name: str, gamma: float) -> str:
    """Imports `png`, draws it into a new RT (TargetGamma `gamma`, 0 = default) and exports that."""
    pc = get_pc()
    krl = unrealsdk.find_class("KismetRenderingLibrary").ClassDefaultObject
    tex = krl.ImportFileAsTexture2D(pc, path_dir + "/" + png)
    size = tex.Blueprint_GetSizeX()
    rt = krl.CreateRenderTarget2D(pc, size, size, _scene.RTF_RGBA8, unrealsdk.make_struct("LinearColor", A=1), False, False)
    if gamma:
        rt.TargetGamma = gamma
    _CC["hold_rt"] = rt
    v2 = lambda x, y: unrealsdk.make_struct("Vector2D", X=x, Y=y)  # noqa: E731
    IG = unrealsdk.unreal.IGNORE_STRUCT
    _, canvas, _, ctx = krl.BeginDrawCanvasToRenderTarget(pc, rt, None, IG, IG)
    canvas.K2_DrawTexture(tex, v2(0, 0), v2(size, size), v2(0, 0), v2(1, 1),
                          unrealsdk.make_struct("LinearColor", R=1, G=1, B=1, A=1), 0, 0.0, v2(0.5, 0.5))
    krl.EndDrawCanvasToRenderTarget(pc, ctx)
    krl.ExportRenderTarget(pc, rt, path_dir, out_name)
    return f"tex srgb={tex.SRGB} size={size}"


def cc_rt_copy(path_dir: str, out_name: str, gamma: float) -> str:
    """Copies the probe capture's RT into a new RT (TargetGamma `gamma`, 0 = default), exports it."""
    a = _CC["actor"]()
    src = a.CaptureComponent2D.TextureTarget
    pc = get_pc()
    krl = unrealsdk.find_class("KismetRenderingLibrary").ClassDefaultObject
    size = src.SizeX
    rt = krl.CreateRenderTarget2D(pc, size, size, _scene.RTF_RGBA8, unrealsdk.make_struct("LinearColor", A=1), False, False)
    if gamma:
        rt.TargetGamma = gamma
    _CC["hold_rt"] = rt
    v2 = lambda x, y: unrealsdk.make_struct("Vector2D", X=x, Y=y)  # noqa: E731
    IG = unrealsdk.unreal.IGNORE_STRUCT
    _, canvas, _, ctx = krl.BeginDrawCanvasToRenderTarget(pc, rt, None, IG, IG)
    canvas.K2_DrawTexture(src, v2(0, 0), v2(size, size), v2(0, 0), v2(1, 1),
                          unrealsdk.make_struct("LinearColor", R=1, G=1, B=1, A=1), 0, 0.0, v2(0.5, 0.5))
    krl.EndDrawCanvasToRenderTarget(pc, ctx)
    krl.ExportRenderTarget(pc, rt, path_dir, out_name)
    krl.ExportRenderTarget(pc, src, path_dir, "rtsrc.png")
    return "ok"
