import json
import socket
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

AODT_HOST = "localhost"
AODT_PORT = 9876

mcp = FastMCP("Nvidia AODT MCP Server")


# ─── Transport helpers ────────────────────────────────────────────────────────

def _send(command_type: str, params: dict = None) -> dict:
    """Send a JSON command to the AODT socket server and return the response dict."""
    payload = {"type": command_type, "params": params or {}}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(10.0)
            s.connect((AODT_HOST, AODT_PORT))
            s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            data = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"\n" in chunk:
                    break
            if not data:
                return {"status": "error", "message": "Empty response from AODT"}
            return json.loads(data.decode("utf-8").strip())
    except ConnectionRefusedError:
        return {
            "status": "error",
            "message": (
                f"Connection refused. Ensure AODT is running and the MCP extension "
                f"is enabled (port {AODT_PORT})."
            ),
        }
    except Exception as e:
        return {"status": "error", "message": f"Communication error: {e}"}


def _run(code: str, truncate: int = 50_000) -> str:
    """Execute Python code inside AODT and return the stdout as a string."""
    response = _send("execute", {"code": code})
    if response.get("status") == "success":
        result = response.get("result", "")
        if not result:
            return "Done (no output)."
        if len(result) > truncate:
            result = result[:truncate] + "\n... (output truncated)"
        return result
    return f"Error: {response.get('message', 'Unknown error')}"


# ─── 1. Connectivity ──────────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True))
def ping_aodt() -> str:
    """
    Checks whether the AODT socket server is reachable.
    Call this first to verify connectivity before using other tools.
    Returns JSON with 'connected' (bool) and a status message.
    """
    response = _send("ping")
    if response.get("status") == "success":
        return json.dumps({"connected": True, "message": "AODT socket server is active."})
    return json.dumps({"connected": False, "error": response.get("message")})


# ─── 2. Stage management ──────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False))
def new_stage() -> str:
    """
    Creates a blank new USD stage in AODT, discarding the current stage and any unsaved changes.
    Equivalent to File > New Stage.
    """
    return _run("""
import omni.usd
omni.usd.get_context().new_stage()
stage = omni.usd.get_context().get_stage()
print("New stage created." if stage else "Error: new stage not available.")
""")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False))
def load_stage(path: str) -> str:
    """
    Opens a USD stage file in AODT, replacing the currently loaded stage.

    Args:
        path: Local path (e.g. /home/user/scene.usd) or Nucleus URL
              (e.g. omniverse://server/Projects/scene.usd).
    """
    code = f"""
import omni.usd
_path = {json.dumps(path)}
ctx = omni.usd.get_context()
success = ctx.open_stage(_path)
stage = ctx.get_stage()
if stage:
    root = stage.GetRootLayer()
    total = sum(1 for _ in stage.Traverse())
    print(f"Stage loaded: {{root.realPath or root.identifier}}")
    print(f"Total prims: {{total}}")
else:
    print(f"Error: failed to open '{{_path}}'")
"""
    return _run(code)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
def save_stage() -> str:
    """
    Saves the currently loaded USD stage to disk. Equivalent to File > Save.
    """
    return _run("""
import omni.usd
ctx = omni.usd.get_context()
stage = ctx.get_stage()
if not stage:
    print("Error: no stage is currently loaded.")
else:
    ctx.save_stage()
    print(f"Stage saved: {stage.GetRootLayer().realPath}")
""")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True))
def get_stage_info() -> str:
    """
    Returns metadata about the currently loaded USD stage:
    file path, up-axis, meters-per-unit, total prim count, and sublayers.
    """
    return _run("""
import omni.usd
from pxr import UsdGeom
ctx = omni.usd.get_context()
stage = ctx.get_stage()
if not stage:
    print("No stage is currently loaded.")
else:
    root = stage.GetRootLayer()
    print(f"File:            {root.realPath or root.identifier}")
    print(f"Up Axis:         {UsdGeom.GetStageUpAxis(stage)}")
    print(f"Meters Per Unit: {UsdGeom.GetStageMetersPerUnit(stage)}")
    print(f"Total Prims:     {sum(1 for _ in stage.Traverse())}")
    subs = root.subLayerPaths
    if subs:
        print(f"Sublayers ({len(subs)}):")
        for s in subs:
            print(f"  - {s}")
    else:
        print("Sublayers: none")
""")


# ─── 3. Stage traversal ───────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True))
def get_aodt_stage_hierarchy(max_depth: int = 3) -> str:
    """
    Returns a text tree of the current USD stage prim hierarchy.
    Read-only — does not modify the stage.

    Args:
        max_depth: Depth to traverse (default 3). Increase to see deeper nesting.
    """
    code = f"""
import omni.usd
stage = omni.usd.get_context().get_stage()

def traverse(prim, depth, max_depth):
    if depth > max_depth:
        return []
    indent = "  " * depth
    lines = [indent + str(prim.GetPath()) + " [" + prim.GetTypeName() + "]"]
    children = prim.GetChildren()
    if children:
        if depth == max_depth:
            lines.append("  " * (depth + 1) + "... (truncated, increase max_depth)")
        else:
            for child in children:
                lines.extend(traverse(child, depth + 1, max_depth))
    return lines

if stage:
    hierarchy = traverse(stage.GetPseudoRoot(), 0, {max_depth})
    print("\\n".join(hierarchy))
    total = sum(1 for _ in stage.Traverse())
    print(f"\\n--- Total prims in stage: {{total}} ---")
else:
    print("No active stage.")
"""
    response = _send("execute", {"code": code})
    if response.get("status") == "success":
        result = response.get("result", "No hierarchy found.")
        if len(result) > 50_000:
            result = result[:50_000] + "\n... (truncated)"
        return result
    return f"Failed to get hierarchy: {response.get('message')}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True))
def find_prims(
    type_filter: str = "",
    name_filter: str = "",
    parent_path: str = "/",
    max_results: int = 50,
) -> str:
    """
    Searches the USD stage for prims matching optional type and/or name filters.

    Args:
        type_filter: Exact USD type name (e.g. 'Mesh', 'SphereLight', 'Camera', 'Xform'). Empty = any.
        name_filter: Case-insensitive substring to match against prim names. Empty = any.
        parent_path: Limit search to descendants of this path (default '/').
        max_results: Maximum results to return (default 50).
    """
    code = f"""
import omni.usd
from pxr import Usd
_type  = {json.dumps(type_filter)}
_name  = {json.dumps(name_filter)}
_root  = {json.dumps(parent_path)}
_max   = {max_results}
stage  = omni.usd.get_context().get_stage()
if not stage:
    print("No stage loaded.")
else:
    base = stage.GetPrimAtPath(_root)
    if not base.IsValid() and _root != "/":
        print(f"Error: no prim at parent_path '{{_root}}'")
    else:
        it = Usd.PrimRange(stage.GetPseudoRoot()) if _root == "/" else Usd.PrimRange(base)
        results = []
        for prim in it:
            if _type and prim.GetTypeName() != _type:
                continue
            if _name and _name.lower() not in prim.GetName().lower():
                continue
            results.append(f"{{prim.GetPath()}} [{{prim.GetTypeName()}}]")
            if len(results) >= _max:
                break
        print(f"Found {{len(results)}} prim(s) (type='{{_type}}', name~'{{_name}}', under '{{_root}}'):")
        for r in results:
            print(f"  {{r}}")
        if len(results) == _max:
            print("  ... (limit reached, increase max_results)")
"""
    return _run(code)


# ─── 4. Prim inspection ───────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True))
def get_prim_info(prim_path: str) -> str:
    """
    Returns detailed information about a specific USD prim: type, active state,
    visibility, all non-null attributes with values, and its direct children.

    Args:
        prim_path: Full USD path (e.g. '/World/MyMesh' or '/RUs/ru_0001').
    """
    code = f"""
import omni.usd
from pxr import UsdGeom, Usd
_path = {json.dumps(prim_path)}
stage = omni.usd.get_context().get_stage()
prim  = stage.GetPrimAtPath(_path)
if not prim.IsValid():
    print(f"Error: no prim found at '{{_path}}'")
else:
    print(f"Path:   {{prim.GetPath()}}")
    print(f"Type:   {{prim.GetTypeName() or '(none)'}}")
    print(f"Active: {{prim.IsActive()}}")
    try:
        vis = UsdGeom.Imageable(prim).ComputeVisibility()
        print(f"Visible: {{vis != UsdGeom.Tokens.invisible}}")
    except Exception:
        pass
    attrs = [(a.GetName(), a.GetTypeName(), a.Get())
             for a in prim.GetAttributes() if a.Get() is not None]
    if attrs:
        print(f"\\nAttributes ({{len(attrs)}}):")
        for name, tname, val in attrs:
            print(f"  {{name}} ({{tname}}): {{val}}")
    children = list(prim.GetChildren())
    if children:
        print(f"\\nChildren ({{len(children)}}):")
        for c in children:
            print(f"  {{c.GetPath()}} [{{c.GetTypeName()}}]")
    else:
        print("\\nChildren: none")
"""
    return _run(code)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True))
def get_prim_attribute(prim_path: str, attribute_name: str) -> str:
    """
    Gets the current value of a specific attribute on a USD prim.
    If the attribute is not found, lists all available attributes on that prim.

    Args:
        prim_path:      Full USD path (e.g. '/World/MyLight').
        attribute_name: Attribute name (e.g. 'intensity', 'xformOp:translate',
                        'aerial:ru:frequency_band').
    """
    code = f"""
import omni.usd
_path = {json.dumps(prim_path)}
_attr = {json.dumps(attribute_name)}
stage = omni.usd.get_context().get_stage()
prim  = stage.GetPrimAtPath(_path)
if not prim.IsValid():
    print(f"Error: no prim at '{{_path}}'")
else:
    attr = prim.GetAttribute(_attr)
    if not attr.IsValid():
        print(f"Attribute '{{_attr}}' not found on '{{_path}}'")
        print("Available attributes:")
        for a in prim.GetAttributes():
            v = a.Get()
            if v is not None:
                print(f"  {{a.GetName()}} ({{a.GetTypeName()}}): {{v}}")
    else:
        print(f"{{_attr}} = {{attr.Get()}}")
        print(f"Type: {{attr.GetTypeName()}}")
"""
    return _run(code)


# ─── 5. Prim manipulation ─────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def create_prim(prim_path: str, prim_type: str = "Xform") -> str:
    """
    Creates a new USD prim in the stage. Uses omni.kit.commands so the action is undoable.

    Common types: Xform, Cube, Sphere, Cylinder, Cone, Capsule, Plane, Mesh,
                  SphereLight, DiskLight, RectLight, DistantLight, Camera.

    Args:
        prim_path: Full USD path for the new prim (e.g. '/World/MyCube').
        prim_type: USD schema type (default 'Xform').
    """
    code = f"""
import omni.usd, omni.kit.commands
_path = {json.dumps(prim_path)}
_type = {json.dumps(prim_type)}
stage = omni.usd.get_context().get_stage()
if stage.GetPrimAtPath(_path).IsValid():
    print(f"Error: prim already exists at '{{_path}}'")
else:
    omni.kit.commands.execute('CreatePrimWithDefaultXform', prim_path=_path, prim_type=_type)
    prim = stage.GetPrimAtPath(_path)
    print(f"Created {{_type}} at '{{_path}}'" if prim.IsValid() else f"Error: prim not created at '{{_path}}'")
"""
    return _run(code)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False))
def delete_prim(prim_path: str) -> str:
    """
    Deletes a USD prim and all its children from the stage. Action is undoable via undo().

    Args:
        prim_path: Full USD path of the prim to delete (e.g. '/World/MyMesh').
    """
    code = f"""
import omni.usd, omni.kit.commands
_path = {json.dumps(prim_path)}
stage = omni.usd.get_context().get_stage()
if not stage.GetPrimAtPath(_path).IsValid():
    print(f"Error: no prim at '{{_path}}'")
else:
    omni.kit.commands.execute('DeletePrims', paths=[_path])
    gone = not stage.GetPrimAtPath(_path).IsValid()
    print(f"Deleted '{{_path}}'" if gone else f"Error: '{{_path}}' still exists after delete")
"""
    return _run(code)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def duplicate_prim(source_path: str, dest_path: str) -> str:
    """
    Duplicates a USD prim (and its children) to a new path. Action is undoable.

    Args:
        source_path: Path of the prim to copy (e.g. '/RUs/ru_0001').
        dest_path:   Path for the duplicate (e.g. '/RUs/ru_0002').
    """
    code = f"""
import omni.usd, omni.kit.commands
_src = {json.dumps(source_path)}
_dst = {json.dumps(dest_path)}
stage = omni.usd.get_context().get_stage()
if not stage.GetPrimAtPath(_src).IsValid():
    print(f"Error: source prim not found at '{{_src}}'")
elif stage.GetPrimAtPath(_dst).IsValid():
    print(f"Error: prim already exists at '{{_dst}}'")
else:
    omni.kit.commands.execute('CopyPrim', path_from=_src, path_to=_dst,
                              duplicate_layers=False, combine_layers=False)
    ok = stage.GetPrimAtPath(_dst).IsValid()
    print(f"Duplicated '{{_src}}' -> '{{_dst}}'" if ok else f"Error: duplicate not created at '{{_dst}}'")
"""
    return _run(code)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def set_prim_visibility(prim_path: str, visible: bool) -> str:
    """
    Shows or hides a prim in the viewport.

    Args:
        prim_path: Full USD path of the prim.
        visible:   True to show, False to hide.
    """
    code = f"""
import omni.usd
from pxr import UsdGeom
_path    = {json.dumps(prim_path)}
_visible = {str(visible)}
stage = omni.usd.get_context().get_stage()
prim  = stage.GetPrimAtPath(_path)
if not prim.IsValid():
    print(f"Error: no prim at '{{_path}}'")
else:
    img = UsdGeom.Imageable(prim)
    img.MakeVisible() if _visible else img.MakeInvisible()
    print(f"'{{_path}}' is now {{'visible' if _visible else 'hidden'}}.")
"""
    return _run(code)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def set_prim_attribute(prim_path: str, attribute_name: str, value: str) -> str:
    """
    Sets an attribute on a USD prim. Automatically infers the correct USD type
    from the existing attribute type or the value format.

    Value format rules:
      - Number:     '1000.0' or '42'
      - Bool:       'true' / 'false'
      - Int array:  '[1, 2, 3]'
      - String:     any other value

    Args:
        prim_path:      Full USD path (e.g. '/World/MyLight' or '/Scenario').
        attribute_name: Attribute name (e.g. 'intensity', 'sim:num_procedural_ues').
        value:          Value string (e.g. '1000.0', 'true', '[1,2,3]').
    """
    # Embed the type-inference logic directly — adapted from AODT's aodt_api.py
    code = f"""
import omni.usd
import re, ast
from pxr import Sdf, Vt

_path  = {json.dumps(prim_path)}
_attr  = {json.dumps(attribute_name)}
_value = {json.dumps(value)}

def _infer_and_cast(string_value, prim, attname):
    '''Infer Sdf type from existing attribute or value shape.'''
    if prim.IsValid() and prim.HasAttribute(attname):
        existing = prim.GetAttribute(attname)
        vt = existing.GetTypeName()
        if vt == Sdf.ValueTypeNames.Bool:
            return vt, string_value.lower() in ('true', 'yes', 'y')
        if vt == Sdf.ValueTypeNames.UInt:
            return vt, int(string_value)
        if vt == Sdf.ValueTypeNames.Int:
            return vt, int(string_value)
        if vt in (Sdf.ValueTypeNames.Float, Sdf.ValueTypeNames.Double):
            return vt, float(string_value)
        if vt == Sdf.ValueTypeNames.IntArray:
            parsed = ast.literal_eval(string_value)
            return vt, Vt.IntArray([int(v) for v in parsed])
        return Sdf.ValueTypeNames.String, string_value
    # No existing attribute — infer from value
    if string_value.lower() in ('true', 'false', 'yes', 'no'):
        return Sdf.ValueTypeNames.Bool, string_value.lower() in ('true', 'yes')
    try:
        return Sdf.ValueTypeNames.Int, int(string_value)
    except ValueError:
        pass
    try:
        return Sdf.ValueTypeNames.Float, float(string_value)
    except ValueError:
        pass
    if string_value.startswith('[') and string_value.endswith(']'):
        parsed = ast.literal_eval(string_value)
        return Sdf.ValueTypeNames.IntArray, Vt.IntArray([int(v) for v in parsed])
    return Sdf.ValueTypeNames.String, string_value

stage = omni.usd.get_context().get_stage()
prim  = stage.GetPrimAtPath(_path)
if not prim.IsValid():
    print(f"Error: no prim at '{{_path}}'")
else:
    try:
        vtype, casted = _infer_and_cast(_value, prim, _attr)
        attr = prim.GetAttribute(_attr)
        if not attr.IsValid():
            attr = prim.CreateAttribute(_attr, vtype)
        attr.Set(casted)
        print(f"Set '{{_path}}'.{{_attr}} = {{attr.Get()}} ({{attr.GetTypeName()}})")
    except Exception as e:
        print(f"Error: {{e}}")
"""
    return _run(code)


# ─── 6. Transform operations ──────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True))
def get_prim_transform(prim_path: str) -> str:
    """
    Returns the local position, rotation (XYZ Euler degrees), and scale of a prim,
    along with its computed world-space translation.

    Args:
        prim_path: Full USD path (e.g. '/World/MyCube').
    """
    code = f"""
import omni.usd
from pxr import UsdGeom, Usd
_path = {json.dumps(prim_path)}
stage = omni.usd.get_context().get_stage()
prim  = stage.GetPrimAtPath(_path)
if not prim.IsValid():
    print(f"Error: no prim at '{{_path}}'")
else:
    xf = UsdGeom.Xformable(prim)
    if not xf:
        print(f"Prim '{{_path}}' is not xformable")
    else:
        translate = rotate = scale = None
        for op in xf.GetOrderedXformOps():
            t = op.GetOpType()
            if t == UsdGeom.XformOp.TypeTranslate:
                translate = op.Get()
            elif t in (UsdGeom.XformOp.TypeRotateXYZ, UsdGeom.XformOp.TypeRotateZYX,
                       UsdGeom.XformOp.TypeRotateX, UsdGeom.XformOp.TypeRotateY,
                       UsdGeom.XformOp.TypeRotateZ):
                rotate = op.Get()
            elif t == UsdGeom.XformOp.TypeScale:
                scale = op.Get()
        world_xform = xf.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        world_t = world_xform.ExtractTranslation()
        print(f"Prim: {{_path}}")
        print(f"  Local Translation: {{translate}}")
        print(f"  Local Rotation:    {{rotate}}")
        print(f"  Local Scale:       {{scale}}")
        print(f"  World Translation: {{world_t}}")
"""
    return _run(code)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def set_prim_transform(
    prim_path: str,
    position: list[float] | None = None,
    rotation: list[float] | None = None,
    scale: list[float] | None = None,
) -> str:
    """
    Sets the local transform of a prim. Only provided components are updated.

    Args:
        prim_path: Full USD path (e.g. '/World/MyCube').
        position:  [x, y, z] translation in stage units. Example: [0.0, 100.0, 50.0]
        rotation:  [rx, ry, rz] XYZ Euler degrees.       Example: [0.0, 0.0, 45.0]
        scale:     [sx, sy, sz] scale factors.            Example: [2.0, 2.0, 2.0]
    """
    code = f"""
import omni.usd
from pxr import Gf, UsdGeom, Usd
_path = {json.dumps(prim_path)}
_pos  = {json.dumps(position)}
_rot  = {json.dumps(rotation)}
_scl  = {json.dumps(scale)}
stage = omni.usd.get_context().get_stage()
prim  = stage.GetPrimAtPath(_path)
if not prim.IsValid():
    print(f"Error: no prim at '{{_path}}'")
else:
    xf = UsdGeom.Xformable(prim)
    existing = {{op.GetOpType(): op for op in xf.GetOrderedXformOps()}}

    def _set_or_add(op_type, add_fn, value):
        op = existing.get(op_type)
        if op:
            op.Set(value)
        else:
            add_fn().Set(value)

    if _pos is not None:
        _set_or_add(UsdGeom.XformOp.TypeTranslate, xf.AddTranslateOp, Gf.Vec3d(*_pos))
    if _rot is not None:
        _set_or_add(UsdGeom.XformOp.TypeRotateXYZ, xf.AddRotateXYZOp, Gf.Vec3f(*_rot))
    if _scl is not None:
        _set_or_add(UsdGeom.XformOp.TypeScale, xf.AddScaleOp, Gf.Vec3f(*_scl))

    print(f"Transform updated for '{{_path}}':")
    if _pos is not None: print(f"  Position: {{_pos}}")
    if _rot is not None: print(f"  Rotation: {{_rot}}")
    if _scl is not None: print(f"  Scale:    {{_scl}}")
"""
    return _run(code)


# ─── 7. Viewport & selection ──────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def select_and_focus_prims(prim_paths: list[str]) -> str:
    """
    Selects the given prims in the stage and moves the viewport camera to frame them.
    Use this to quickly navigate to any object by path.

    Args:
        prim_paths: List of USD paths to select and frame
                    (e.g. ['/RUs/ru_0001'] or ['/World/Cube', '/World/Sphere']).
    """
    code = f"""
import omni.usd, omni.kit.commands
from omni.kit.viewport.utility import get_active_viewport
from pxr import Usd, UsdGeom, Sdf

_paths = {json.dumps(prim_paths)}
ctx    = omni.usd.get_context()
stage  = ctx.get_stage()

valid   = [p for p in _paths if stage.GetPrimAtPath(p).IsValid()]
invalid = [p for p in _paths if p not in valid]
if invalid:
    print(f"Warning: paths not found in stage: {{invalid}}")
if not valid:
    print("Error: no valid paths to select.")
else:
    ctx.get_selection().set_selected_prim_paths(valid, False)
    # Frame the selection using the active viewport camera
    viewport = get_active_viewport()
    if viewport:
        camera_path = viewport.camera_path
        time_code   = Usd.TimeCode.Default()
        resolution  = viewport.resolution
        aspect      = resolution[0] / resolution[1] if resolution[1] else 1.0
        omni.kit.commands.execute(
            'FramePrimsCommand',
            prim_to_move=camera_path,
            prims_to_frame=valid,
            time_code=time_code,
            aspect_ratio=aspect,
            zoom=0.6,
        )
        print(f"Selected and framed: {{valid}}")
    else:
        print(f"Selected: {{valid}} (no active viewport to frame)")
"""
    return _run(code)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False))
def take_screenshot(output_path: str = "/tmp/aodt_screenshot.png") -> str:
    """
    Captures the current AODT viewport to an image file.
    The file is written after the next rendered frame (usually <1 second).

    Args:
        output_path: Absolute path for the output image
                     (default '/tmp/aodt_screenshot.png'). PNG format.
    """
    code = f"""
import omni.renderer_capture
_path = {json.dumps(output_path)}
try:
    iface = omni.renderer_capture.acquire_renderer_capture_interface()
    iface.capture_next_frame_swapchain(_path)
    print(f"Screenshot queued: {{_path}}")
    print("The file is written after the next rendered frame.")
except Exception as e:
    print(f"Error: {{e}}")
"""
    return _run(code)


# ─── 8. Timeline / simulation ─────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True))
def get_simulation_status() -> str:
    """
    Returns the current simulation state (playing / paused / stopped)
    and the current timeline position.
    """
    return _run("""
import omni.timeline
tl = omni.timeline.get_timeline_interface()
state = "playing" if tl.is_playing() else ("stopped" if tl.is_stopped() else "paused")
print(f"State:       {state}")
print(f"Current time: {tl.get_current_time():.3f}s")
print(f"Time range:   {tl.get_start_time():.3f}s - {tl.get_end_time():.3f}s")
""")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def start_simulation() -> str:
    """Starts (plays) the simulation from its current time position."""
    return _run("""
import omni.timeline
tl = omni.timeline.get_timeline_interface()
tl.play()
print(f"Simulation started at {tl.get_current_time():.3f}s")
""")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def stop_simulation() -> str:
    """Pauses the simulation at its current time position."""
    return _run("""
import omni.timeline
tl = omni.timeline.get_timeline_interface()
tl.pause()
print(f"Simulation paused at {tl.get_current_time():.3f}s")
""")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
def reset_simulation() -> str:
    """Stops the simulation and resets the timeline to t=0."""
    return _run("""
import omni.timeline
omni.timeline.get_timeline_interface().stop()
print("Simulation reset to t=0.")
""")


# ─── 9. AODT domain — network entities ───────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True))
def get_scenario_info() -> str:
    """
    Returns the AODT scenario configuration: simulation parameters, procedural UE count,
    scatterer count, and any other attributes on the /Scenario prim.
    """
    return _run("""
import omni.usd
stage = omni.usd.get_context().get_stage()
prim  = stage.GetPrimAtPath("/Scenario")
if not prim.IsValid():
    print("No /Scenario prim found. Is an AODT scene loaded?")
else:
    print("AODT Scenario Configuration:")
    attrs = [(a.GetName(), a.GetTypeName(), a.Get())
             for a in prim.GetAttributes() if a.Get() is not None]
    for name, tname, val in sorted(attrs):
        print(f"  {name} ({tname}): {val}")
""")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True))
def list_network_entities() -> str:
    """
    Lists all Radio Units (RUs), Distributed Units (DUs), and User Equipment (UEs)
    currently placed in the scene, with their positions.
    """
    return _run("""
import omni.usd
from pxr import UsdGeom, Usd
stage = omni.usd.get_context().get_stage()

def list_entity_group(path_str, label):
    parent = stage.GetPrimAtPath(path_str)
    if not parent.IsValid():
        print(f"  (/{label}s path not found)")
        return
    children = list(parent.GetChildren())
    print(f"{label}s ({len(children)}):")
    for prim in children:
        xf = UsdGeom.Xformable(prim)
        pos = None
        if xf:
            for op in xf.GetOrderedXformOps():
                if op.GetOpType() == UsdGeom.XformOp.TypeTranslate:
                    pos = op.Get()
                    break
        pos_str = f"  pos={pos}" if pos else ""
        print(f"  {prim.GetPath()}{pos_str}")

list_entity_group("/RUs", "RU")
print()
list_entity_group("/DUs", "DU")
print()
list_entity_group("/UEs", "UE")
""")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def create_ue(position: list[float]) -> str:
    """
    Places a new User Equipment (UE / mobile device) in the AODT scene.
    Uses the AODT prim factory for correct asset referencing and ID assignment.

    Args:
        position: [x, y, z] world coordinates in stage units (e.g. [100.0, 200.0, 0.0]).
    """
    code = f"""
from pxr import Gf
from aodt.common.prims import create_ue_prim
_pos = {json.dumps(position)}
prim = create_ue_prim(position=Gf.Vec3d(*_pos))
if prim and prim.IsValid():
    print(f"UE created at {{prim.GetPath()}}  position={{_pos}}")
else:
    print("Error: UE prim was not created. Check that an AODT scene is loaded and UE asset is configured.")
"""
    return _run(code)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def create_ru(position: list[float]) -> str:
    """
    Places a new Radio Unit (RU / base station antenna) in the AODT scene.
    Uses the AODT deployer model for correct placement and configuration.

    Args:
        position: [x, y, z] world coordinates in stage units (e.g. [0.0, 0.0, 30.0]).
    """
    code = f"""
from aodt.deployer.model import get_ru_model_instance
_pos = {json.dumps(position)}
prim_path = get_ru_model_instance().deploy_ru(_pos)
if prim_path:
    print(f"RU created at {{prim_path}}  position={{_pos}}")
else:
    print("Error: RU not created. Check that an AODT scene is loaded and RU asset is configured.")
"""
    return _run(code)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def create_du(position: list[float]) -> str:
    """
    Places a new Distributed Unit (DU / baseband processing unit) in the AODT scene.
    DUs are typically placed above buildings (min ~300 m altitude) and connect to RUs.

    Args:
        position: [x, y, z] world coordinates in stage units (e.g. [0.0, 0.0, 350.0]).
    """
    code = f"""
from aodt.deployer.model import get_du_model_instance
_pos = {json.dumps(position)}
get_du_model_instance().deploy_du(_pos)
print(f"DU deployment triggered at position {{_pos}}")
"""
    return _run(code)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True))
def get_ue_performance(prim_path: str) -> str:
    """
    Retrieves live throughput telemetry for a specific UE prim.
    Returns DL/UL average throughput, slot count, and zero-throughput counts.
    Only available while a simulation is running or has recently run.

    Args:
        prim_path: Full USD path of the UE prim (e.g. '/UEs/ue_0001').
    """
    code = f"""
import omni.usd
from aodt.telemetry import TelemetryExt
_path = {json.dumps(prim_path)}
stage = omni.usd.get_context().get_stage()
prim  = stage.GetPrimAtPath(_path)
if not prim.IsValid():
    print(f"Error: no prim at '{{_path}}'")
else:
    uid_attr = prim.GetAttribute("aerial:ue:user_id")
    if not uid_attr or uid_attr.Get() is None:
        print(f"Prim '{{_path}}' has no 'aerial:ue:user_id' attribute — is it a UE?")
    else:
        ue_id = uid_attr.Get()
        data  = TelemetryExt.get_ue_telemetry_data(ue_id)
        if not data:
            print(f"No telemetry data for UE {{ue_id}} at '{{_path}}'.")
            print("Ensure the simulation has run and telemetry is enabled.")
        else:
            import numpy as np
            tput   = np.array(data.get("t_put", []))
            links  = np.array(data.get("link",  []))
            slots  = data.get("slot", [])
            dl_vals = tput[links == "DL"]
            ul_vals = tput[links == "UL"]
            dl_avg  = float(dl_vals[dl_vals > 0].mean()) if any(dl_vals > 0) else 0.0
            ul_avg  = float(ul_vals[ul_vals > 0].mean()) if any(ul_vals > 0) else 0.0
            print(f"UE performance: '{{_path}}' (id={{ue_id}})")
            print(f"  Total slots:   {{len(slots)}}")
            print(f"  DL avg tput:   {{dl_avg:.1f}} (zeros: {{int(np.sum(dl_vals == 0))}})")
            print(f"  UL avg tput:   {{ul_avg:.1f}} (zeros: {{int(np.sum(ul_vals == 0))}})")
"""
    return _run(code)


# ─── 10. AODT configuration settings ─────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True))
def get_aodt_setting(setting_path: str) -> str:
    """
    Reads a Carbonite (carb.settings) configuration value from AODT.
    Use this to inspect DB connection info, asset paths, session names, etc.

    Common paths:
      /persistent/exts/aodt.configuration/db_host
      /persistent/exts/aodt.configuration/db_name
      /persistent/exts/aodt.configuration/session_name
      /persistent/exts/aodt.configuration/ru_asset_path
      /persistent/exts/aodt.configuration/ue_asset_path

    Args:
        setting_path: Carb settings path (e.g. '/persistent/exts/aodt.configuration/db_host').
    """
    code = f"""
import carb.settings
_path = {json.dumps(setting_path)}
val = carb.settings.get_settings().get(_path)
if val is None:
    print(f"Setting '{{_path}}' is not set (None).")
else:
    print(f"{{_path}} = {{val!r}}")
"""
    return _run(code)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def set_aodt_setting(setting_path: str, value: str) -> str:
    """
    Writes a Carbonite (carb.settings) configuration value in AODT.
    Type is inferred automatically (bool, int, float, or string).

    Args:
        setting_path: Carb settings path (e.g. '/persistent/exts/aodt.configuration/db_host').
        value:        New value as a string (e.g. 'localhost', '5432', 'true').
    """
    code = f"""
import carb.settings
_path  = {json.dumps(setting_path)}
_value = {json.dumps(value)}
settings = carb.settings.get_settings()
current  = settings.get(_path)

if isinstance(current, bool):
    settings.set(_path, _value.lower() in ('true', 'yes', '1'))
elif isinstance(current, int):
    settings.set(_path, int(_value))
elif isinstance(current, float):
    settings.set(_path, float(_value))
else:
    # Try numeric inference if no existing value
    try:
        settings.set(_path, int(_value)); print(f"Set (int) {{_path}} = {{settings.get(_path)!r}}"); raise SystemExit
    except (ValueError, SystemExit) as e:
        if isinstance(e, SystemExit): raise
    try:
        settings.set(_path, float(_value)); print(f"Set (float) {{_path}} = {{settings.get(_path)!r}}"); raise SystemExit
    except (ValueError, SystemExit) as e:
        if isinstance(e, SystemExit): raise
    settings.set(_path, _value)

print(f"Set {{_path}} = {{settings.get(_path)!r}}")
"""
    return _run(code)


# ─── 11. History ──────────────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def undo() -> str:
    """
    Undoes the last stage modification (equivalent to Ctrl+Z).
    Works for create_prim, delete_prim, set_prim_transform, set_prim_attribute, etc.
    """
    return _run("""
import omni.kit.undo
omni.kit.undo.undo()
print("Undo performed.")
""")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def redo() -> str:
    """Redoes the last undone stage modification (equivalent to Ctrl+Shift+Z)."""
    return _run("""
import omni.kit.undo
omni.kit.undo.redo()
print("Redo performed.")
""")


# ─── 12. Asset discovery ──────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True))
def search_aodt_assets(query: str) -> str:
    """
    Searches for USD assets matching a keyword across local AODT install paths
    and Omniverse Nucleus server directories.

    Args:
        query: Search term (e.g. 'tokyo', 'berlin', 'antenna').
    """
    # json.dumps safely escapes query — prevents injection via special characters
    safe_query = json.dumps(query)
    code = f"""
import os
import omni.client

_query = {safe_query}

search_paths = [
    "/home/sal-garfield/.local/share/ov/pkg/aodt-1.4.1/assets",
    "/home/sal-garfield/aodt_1.4.1/assets",
    "omniverse://omniverse-server/Users/aerial",
    "omniverse://omniverse-server/Projects",
    "omniverse://omniverse-server/NVIDIA/Assets/DigitalTwin",
]

found = []

for path in search_paths:
    if path.startswith("/") and os.path.exists(path):
        for root, _, files in os.walk(path):
            for f in files:
                if _query.lower() in f.lower() and f.endswith(('.usd', '.usda', '.usdc')):
                    found.append(os.path.join(root, f))

def _scan_nucleus(base, query, results, depth=0, max_depth=3):
    if depth > max_depth:
        return
    res, entries = omni.client.list(base)
    if res == omni.client.Result.OK:
        for e in entries:
            full = base + "/" + e.relative_path
            if query.lower() in e.relative_path.lower() and e.relative_path.endswith(('.usd', '.usda', '.usdc')):
                results.append(full)
            if e.flags & omni.client.ItemFlags.CAN_HAVE_CHILDREN:
                _scan_nucleus(full, query, results, depth + 1, max_depth)

for path in search_paths:
    if path.startswith("omniverse://"):
        _scan_nucleus(path, _query, found)

if found:
    print(f"Found {{len(found)}} asset(s) matching {{repr(_query)}}:")
    for f in found[:20]:
        print(f"  {{f}}")
    if len(found) > 20:
        print(f"  ... and {{len(found) - 20}} more")
else:
    print(f"No assets matching {{repr(_query)}} found.")
"""
    response = _send("execute", {"code": code})
    if response.get("status") == "success":
        return response.get("result", "Search failed.")
    return f"Asset search error: {response.get('message')}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True))
def list_loadable_scenes() -> str:
    """
    Lists all USD scene files found in standard AODT install paths and
    Omniverse Nucleus project directories. Use load_stage() to open one.
    """
    return _run("""
import os, omni.client

search_paths = [
    "/home/sal-garfield/.local/share/ov/pkg/aodt-1.4.1/assets",
    "/home/sal-garfield/aodt_1.4.1/assets",
    "omniverse://omniverse-server/Users/aerial",
    "omniverse://omniverse-server/Projects",
    "omniverse://omniverse-server/NVIDIA/Assets/DigitalTwin",
]

found = []
for path in search_paths:
    if path.startswith("/") and os.path.exists(path):
        for root, _, files in os.walk(path):
            for f in files:
                if f.endswith(('.usd', '.usda', '.usdc')):
                    found.append(os.path.join(root, f))

def _scan(base, results, depth=0, max_depth=2):
    if depth > max_depth: return
    res, entries = omni.client.list(base)
    if res == omni.client.Result.OK:
        for e in entries:
            full = base + "/" + e.relative_path
            if e.relative_path.endswith(('.usd', '.usda', '.usdc')):
                results.append(full)
            if e.flags & omni.client.ItemFlags.CAN_HAVE_CHILDREN:
                _scan(full, results, depth + 1, max_depth)

for path in search_paths:
    if path.startswith("omniverse://"): _scan(path, found)

if found:
    print(f"Found {len(found)} USD scene(s):")
    for f in found[:30]: print(f"  {f}")
    if len(found) > 30: print(f"  ... and {len(found) - 30} more")
else:
    print("No USD scenes found in standard search paths.")
""")


# ─── 13. Raw execution ────────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False))
def execute_aodt_command(code: str) -> str:
    """
    Executes raw Python code directly inside AODT. Full access to omni.*, pxr.*, carb.*,
    and all AODT internal modules (aodt.common, aodt.deployer, aodt.telemetry, etc.).

    Use this as a last resort when no other tool covers the operation you need.
    NOTE: Can modify or destroy stage content — prefer specific tools for common tasks.
    Variables defined in one call persist to subsequent calls (shared exec scope).

    Args:
        code: Python code string to execute inside AODT.
    """
    response = _send("execute", {"code": code})
    if response.get("status") == "success":
        result = response.get("result", "")
        return result if result else "Execution successful (no output)."
    return f"Execution failed:\n{response.get('message', 'Unknown error')}"


if __name__ == "__main__":
    mcp.run()
