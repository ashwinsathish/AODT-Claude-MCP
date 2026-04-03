import json
import re
import socket
import time
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

AODT_HOST = "localhost"
AODT_PORT = 8765

mcp = FastMCP("Nvidia AODT MCP Server")


_WRITABLE_EDIT_TARGET_SNIPPET = """
ctx = omni.usd.get_context()
stage = ctx.get_stage()
if stage:
    try:
        session_layer = stage.GetSessionLayer()
        if session_layer:
            current_layer = stage.GetEditTarget().GetLayer()
            current_id = current_layer.identifier if current_layer else ""
            session_id = session_layer.identifier
            if current_id != session_id:
                stage.SetEditTarget(session_layer)
                print(f"Info: using session edit target: {session_id}")
    except Exception as _edit_err:
        print(f"Warning: edit-target auto switch unavailable: {_edit_err}")
"""


# ─── Transport helpers ────────────────────────────────────────────────────────

def _send(command_type: str, params: dict = None) -> dict:
    """Send a JSON command to the AODT socket server and return the response dict."""
    payload = {"type": command_type, "params": params or {}}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(120.0)
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
        # When AODT-side code returns success but prints an error traceback/message,
        # append context + logs to keep agent actions grounded in runtime state.
        if _looks_like_aodt_error_output(result):
            diag = _collect_aodt_diagnostics(log_lines=80, truncate=30_000)
            if diag:
                if len(diag) > 30_000:
                    diag = diag[:30_000] + "\n... (diagnostics truncated)"
                result = f"{result}\n\n--- Auto Diagnostics ---\n{diag}"
        if len(result) > truncate:
            result = result[:truncate] + "\n... (output truncated)"
        return result
    err_msg = response.get("message", "Unknown error")
    err = f"Error: {err_msg}"
    # Avoid immediate recursive execute calls when the main-thread execution already timed out.
    if "Execution timed out on the main thread" in err_msg:
        return (
            f"{err}\n"
            "Hint: this usually means tool code blocked AODT's main thread. "
            "Retry with lightweight operations first and inspect get_recent_aodt_logs()."
        )
    diag = _collect_aodt_diagnostics(log_lines=80, truncate=30_000)
    if diag:
        if len(diag) > 30_000:
            diag = diag[:30_000] + "\n... (diagnostics truncated)"
        return f"{err}\n\n--- Auto Diagnostics ---\n{diag}"
    return err


def _looks_like_aodt_error_output(output: str) -> bool:
    """Heuristic: detect embedded runtime errors in stdout from successful execute calls."""
    if not output:
        return False
    lowered = output.lower()
    if "traceback (most recent call last)" in lowered:
        return True
    for line in output.splitlines():
        s = line.strip().lower()
        if not s:
            continue
        if s.startswith("error:") or s.startswith("exception:"):
            return True
    return False


def _collect_aodt_diagnostics(log_lines: int = 80, truncate: int = 30_000) -> str:
    """
    Pull context + recent logs from inside AODT.
    Used automatically on command failures to reduce blind retries/workarounds.
    """
    safe_lines = max(20, min(int(log_lines), 500))
    code = f"""
import glob
import os
import carb.settings
import carb.tokens
import omni.usd
import omni.timeline
import omni.kit.usd.layers as layers
from aodt.common import constants
from aodt.progress_bar.progress_bar import ProgressModel
from aodt.common.utils import get_stage_meters_per_unit, get_scale_factor

def _tail(path, n):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        data = f.readlines()
    return data[-n:]

settings = carb.settings.get_settings()
stage = omni.usd.get_context().get_stage()
tl = omni.timeline.get_timeline_interface()
pm = ProgressModel.get_model_instance()
live_syncing = layers.get_layers().get_live_syncing()
worker_uuid = settings.get(constants.WORKER_UUID_SETTING_PATH) or ""
db_connected = bool(settings.get(constants.DB_CONNECTED_STATE_PATH))

print("Runtime Context:")
print(f"  Stage loaded:      {{bool(stage)}}")
if stage:
    print(f"  Stage path:        {{stage.GetRootLayer().realPath or stage.GetRootLayer().identifier}}")
    print(f"  Meters per unit:   {{get_stage_meters_per_unit()}}")
    print(f"  Asset scale factor:{{get_scale_factor()}}")
print(f"  Worker attached:   {{bool(worker_uuid)}}")
if worker_uuid:
    print(f"  Worker UUID:       {{worker_uuid}}")
print(f"  DB connected:      {{db_connected}}")
print(f"  Live session:      {{bool(live_syncing and live_syncing.is_stage_in_live_session())}}")
print(f"  Sim running:       {{pm.is_sim_running()}}")
print(f"  Sim paused:        {{pm.is_sim_paused()}}")
print(f"  Sim progress:      {{pm.get_value_as_string()}}")
timeline_state = "playing" if tl.is_playing() else ("stopped" if tl.is_stopped() else "paused")
print(f"  Timeline state:    {{timeline_state}} @ {{tl.get_current_time():.3f}}s")

print()
print("Recent Logs:")
kit_log = settings.get("/log/file")
if kit_log and os.path.isfile(kit_log):
    print(f"  KIT log: {{kit_log}}")
    for line in _tail(kit_log, {safe_lines}):
        print(line.rstrip("\\n"))
else:
    print(f"  KIT log not found: {{kit_log!r}}")

print()
control_dir = carb.tokens.get_tokens_interface().resolve("${{omni_logs}}") + "/Kit/aodt.control"
control_logs = sorted(glob.glob(os.path.join(control_dir, "control_*.log")), key=os.path.getmtime)
if control_logs:
    latest = control_logs[-1]
    print(f"  AODT control log: {{latest}}")
    for line in _tail(latest, {safe_lines}):
        print(line.rstrip("\\n"))
else:
    print(f"  AODT control log not found under {{control_dir}}")
"""
    response = _send("execute", {"code": code})
    if response.get("status") != "success":
        return f"Unable to collect diagnostics: {response.get('message', 'unknown error')}"
    out = response.get("result", "")
    if len(out) > truncate:
        out = out[:truncate] + "\n... (output truncated)"
    return out


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
{_WRITABLE_EDIT_TARGET_SNIPPET}
if not stage:
    print("Error: no stage loaded.")
    raise SystemExit
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
{_WRITABLE_EDIT_TARGET_SNIPPET}
if not stage:
    print("Error: no stage loaded.")
    raise SystemExit
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
{_WRITABLE_EDIT_TARGET_SNIPPET}
if not stage:
    print("Error: no stage loaded.")
    raise SystemExit
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
{_WRITABLE_EDIT_TARGET_SNIPPET}
if not stage:
    print("Error: no stage loaded.")
    raise SystemExit
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

{_WRITABLE_EDIT_TARGET_SNIPPET}
if not stage:
    print("Error: no stage loaded.")
    raise SystemExit
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
{_WRITABLE_EDIT_TARGET_SNIPPET}
if not stage:
    print("Error: no stage loaded.")
    raise SystemExit
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
def select_and_focus_prims(prim_paths: list[str], frame: bool = False) -> str:
    """
    Selects the given prims in the stage and moves the viewport camera to frame them.
    Use this to quickly navigate to any object by path.

    Args:
        prim_paths: List of USD paths to select and frame
                    (e.g. ['/RUs/ru_0001'] or ['/World/Cube', '/World/Sphere']).
        frame: If True, attempts viewport framing. Default False for stability.
    """
    code = f"""
import omni.usd, omni.kit.commands
from omni.kit.viewport.utility import get_active_viewport
from pxr import Usd, UsdGeom, Sdf

_paths = {json.dumps(prim_paths)}
_frame = {str(frame)}
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
    if _frame:
        # Frame the selection using the active viewport camera.
        # This can be unstable on some heavy scenes, so it's opt-in.
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
    else:
        print(f"Selected: {{valid}} (framing disabled by default for stability)")
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
    Returns simulation status across both:
    1) AODT worker-driven simulation pipeline, and
    2) USD timeline state.
    """
    return _run("""
import carb.settings
import omni.timeline
import omni.kit.usd.layers as layers
from aodt.common import constants
from aodt.progress_bar.progress_bar import ProgressModel

tl = omni.timeline.get_timeline_interface()
timeline_state = "playing" if tl.is_playing() else ("stopped" if tl.is_stopped() else "paused")
pm = ProgressModel.get_model_instance()
settings = carb.settings.get_settings()
worker_uuid = settings.get(constants.WORKER_UUID_SETTING_PATH) or ""
db_connected = bool(settings.get(constants.DB_CONNECTED_STATE_PATH))
live_syncing = layers.get_layers().get_live_syncing()
in_live = bool(live_syncing and live_syncing.is_stage_in_live_session())

print("AODT Worker Simulation:")
print(f"  Worker attached: {bool(worker_uuid)}")
if worker_uuid:
    print(f"  Worker UUID:     {worker_uuid}")
print(f"  DB connected:    {db_connected}")
print(f"  In live session: {in_live}")
print(f"  Running:         {pm.is_sim_running()}")
print(f"  Paused:          {pm.is_sim_paused()}")
print(f"  Progress:        {pm.get_value_as_string()}")
print()
print("USD Timeline:")
print(f"  State:       {timeline_state}")
print(f"Current time: {tl.get_current_time():.3f}s")
print(f"Time range:   {tl.get_start_time():.3f}s - {tl.get_end_time():.3f}s")
""")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True))
def validate_control_readiness(require_live_session: bool = True, require_saved_stage: bool = False) -> str:
    """
    Returns a preflight checklist for agentic AODT control.
    Includes readiness booleans for mobility generation and simulation start.
    """
    code = """
import json
import carb.settings
import omni.usd
import omni.kit.usd.layers as layers
from aodt.common import constants
from aodt.common.prims import get_total_ues
from aodt.progress_bar.progress_bar import ProgressModel
from aodt.telemetry import TelemetryExt
from aodt.toolbar.extension import validate_ref_freq, validate_ru_du_assignment

_require_live = __REQUIRE_LIVE__
_require_saved = __REQUIRE_SAVED__

stage = omni.usd.get_context().get_stage()
settings = carb.settings.get_settings()
live_syncing = layers.get_layers().get_live_syncing()
worker_uuid = settings.get(constants.WORKER_UUID_SETTING_PATH) or ""
db_connected = bool(settings.get(constants.DB_CONNECTED_STATE_PATH))
in_live = bool(live_syncing and live_syncing.is_stage_in_live_session())
pm = ProgressModel.get_model_instance()

stage_loaded = bool(stage)
stage_path = ""
stage_saved = False
has_scenario = False
ru_count = 0
du_count = 0
ue_count = 0
panel_count = 0
has_panel = False

if stage:
    root = stage.GetRootLayer()
    stage_path = root.realPath or root.identifier or ""
    stage_saved = bool(root.realPath)
    has_scenario = stage.GetPrimAtPath("/Scenario").IsValid()
    ru_parent = stage.GetPrimAtPath("/RUs")
    du_parent = stage.GetPrimAtPath("/DUs")
    ue_parent = stage.GetPrimAtPath("/UEs")
    panel_parent = stage.GetPrimAtPath("/Panels")
    ru_count = len(ru_parent.GetChildren()) if ru_parent.IsValid() else 0
    du_count = len(du_parent.GetChildren()) if du_parent.IsValid() else 0
    ue_count = len(ue_parent.GetChildren()) if ue_parent.IsValid() else 0
    panels = list(panel_parent.GetChildren()) if panel_parent.IsValid() else []
    panel_count = len(panels)
    has_panel = any(p.GetName().startswith("panel_") for p in panels) if panels else False

total_ues = get_total_ues() if stage else 0

try:
    mobility_in_sync = bool(TelemetryExt.is_ue_mobility_in_sync_with_db())
except Exception:
    mobility_in_sync = False

try:
    validation_errors = sorted(list(validate_ref_freq().union(validate_ru_du_assignment())))
except Exception as e:
    validation_errors = ["validation call failed: " + str(e)]

start_requirements = {
    "stage_loaded": stage_loaded,
    "has_scenario_prim": has_scenario,
    "worker_attached": bool(worker_uuid),
    "has_panel": has_panel,
    "has_ues": total_ues > 0,
    "mobility_in_sync_with_db": mobility_in_sync,
    "rf_and_ru_du_validation_passed": len(validation_errors) == 0,
}
if _require_live:
    start_requirements["in_live_session"] = in_live
if _require_saved:
    start_requirements["stage_saved"] = stage_saved
start_ready = all(start_requirements.values())

mobility_requirements = {
    "stage_loaded": stage_loaded,
    "worker_attached": bool(worker_uuid),
    "stage_saved": stage_saved,
}
if _require_live:
    mobility_requirements["in_live_session"] = in_live
mobility_ready = all(mobility_requirements.values())

recommendations = []
if not stage_loaded:
    recommendations.append("Load or create an AODT stage.")
if stage_loaded and not has_scenario:
    recommendations.append("Ensure /Scenario prim exists (load a valid AODT scene).")
if not worker_uuid:
    recommendations.append("Attach a worker from AODT configuration before mobility/simulation.")
if _require_live and not in_live:
    recommendations.append("Create or join a live session before starting simulation.")
if not has_panel:
    recommendations.append("Create at least one panel under /Panels (create_panel).")
if total_ues <= 0:
    recommendations.append("Create at least one UE (create_ue or create_tx_rx_pair).")
if not mobility_in_sync:
    recommendations.append("Run generate_mobility and wait for DB sync.")
if not stage_saved:
    recommendations.append("Save the stage before mobility generation.")
if validation_errors:
    recommendations.append("Fix RF / RU-DU validation errors listed in validation_errors.")

payload = {
    "checks": {
        "stage_loaded": stage_loaded,
        "stage_path": stage_path,
        "stage_saved": stage_saved,
        "worker_attached": bool(worker_uuid),
        "worker_uuid": worker_uuid,
        "db_connected": db_connected,
        "in_live_session": in_live,
        "sim_running": pm.is_sim_running(),
        "sim_paused": pm.is_sim_paused(),
        "sim_progress": pm.get_value_as_string(),
        "has_scenario_prim": has_scenario,
        "has_panel": has_panel,
        "mobility_in_sync_with_db": mobility_in_sync,
    },
    "counts": {
        "rus": ru_count,
        "dus": du_count,
        "ues": ue_count,
        "panels": panel_count,
        "total_ues": total_ues,
    },
    "start_sim_requirements": start_requirements,
    "start_sim_ready": start_ready,
    "mobility_requirements": mobility_requirements,
    "mobility_ready": mobility_ready,
    "validation_errors": validation_errors,
    "recommendations": recommendations,
}

print(json.dumps(payload, indent=2))
"""
    code = code.replace("__REQUIRE_LIVE__", str(require_live_session)).replace(
        "__REQUIRE_SAVED__", str(require_saved_stage)
    )
    return _run(code)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True))
def wait_for_mobility_sync(timeout_seconds: int = 180, poll_interval_seconds: float = 1.0) -> str:
    """
    Waits until UE mobility is in sync with DB (TelemetryExt check), or times out.
    Useful immediately after generate_mobility().
    """
    timeout_s = max(1, int(timeout_seconds))
    poll_s = max(0.1, float(poll_interval_seconds))
    probe_code = """
import json
from aodt.telemetry import TelemetryExt
payload = {"synced": False}
try:
    payload["synced"] = bool(TelemetryExt.is_ue_mobility_in_sync_with_db())
except Exception as e:
    payload["error"] = str(e)
print(json.dumps(payload))
"""
    start = time.time()
    samples = []
    while True:
        elapsed = time.time() - start
        response = _send("execute", {"code": probe_code})
        synced = False
        error_text = None
        if response.get("status") == "success":
            raw = (response.get("result") or "").strip()
            try:
                data = json.loads(raw)
            except Exception:
                data = {"synced": False, "error": f"non-json probe output: {raw[:200]}"}
            synced = bool(data.get("synced", False))
            error_text = data.get("error")
        else:
            error_text = response.get("message", "probe execute failed")

        sample = {"t": round(elapsed, 2), "synced": synced}
        if error_text:
            sample["error"] = str(error_text)
        samples.append(sample)

        if synced:
            return json.dumps(
                {"synced": True, "elapsed_seconds": round(elapsed, 2), "samples": samples[-40:]},
                indent=2,
            )
        if elapsed >= timeout_s:
            return json.dumps(
                {
                    "synced": False,
                    "reason": "timeout",
                    "elapsed_seconds": round(elapsed, 2),
                    "samples": samples[-40:],
                },
                indent=2,
            )
        time.sleep(poll_s)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True))
def wait_for_sim_completion(
    timeout_seconds: int = 240,
    poll_interval_seconds: float = 1.0,
    fail_if_not_running_initially: bool = True,
) -> str:
    """
    Waits for worker simulation to finish (or timeout) using ProgressModel state.
    """
    timeout_s = max(1, int(timeout_seconds))
    poll_s = max(0.1, float(poll_interval_seconds))
    fail_if_not_running = bool(fail_if_not_running_initially)
    probe_code = """
import json
from aodt.progress_bar.progress_bar import ProgressModel
pm = ProgressModel.get_model_instance()
running = bool(pm.is_sim_running())
paused = bool(pm.is_sim_paused())
progress_float = float(pm.get_value_as_float())
progress_str = pm.get_value_as_string()
state = "running" if running and not paused else ("paused" if paused else "idle")
print(json.dumps({
    "sim_running": running,
    "sim_paused": paused,
    "progress": progress_str,
    "progress_float": round(progress_float, 4),
    "state": state,
}))
"""

    def _probe():
        resp = _send("execute", {"code": probe_code})
        if resp.get("status") != "success":
            return None, resp.get("message", "probe execute failed")
        raw = (resp.get("result") or "").strip()
        try:
            return json.loads(raw), None
        except Exception:
            return None, f"non-json probe output: {raw[:200]}"

    first, first_err = _probe()
    if first is None:
        return json.dumps({"completed": False, "reason": "probe_error", "error": first_err}, indent=2)

    if fail_if_not_running and not bool(first.get("sim_running", False)):
        return json.dumps(
            {
                "completed": False,
                "reason": "simulation_not_running_at_start",
                "sim_running": bool(first.get("sim_running", False)),
                "sim_paused": bool(first.get("sim_paused", False)),
                "progress": first.get("progress"),
                "progress_float": first.get("progress_float"),
            },
            indent=2,
        )

    start = time.time()
    saw_running = bool(first.get("sim_running", False))
    history = [
        {
            "t": 0.0,
            "state": first.get("state"),
            "progress": first.get("progress"),
            "progress_float": first.get("progress_float"),
        }
    ]

    while True:
        elapsed = time.time() - start
        snap, err = _probe()
        if snap is None:
            history.append({"t": round(elapsed, 2), "state": "probe_error", "error": str(err)})
            if elapsed >= timeout_s:
                return json.dumps(
                    {
                        "completed": False,
                        "reason": "timeout",
                        "elapsed_seconds": round(elapsed, 2),
                        "history": history[-40:],
                    },
                    indent=2,
                )
            time.sleep(poll_s)
            continue

        running = bool(snap.get("sim_running", False))
        paused = bool(snap.get("sim_paused", False))
        progress_float = float(snap.get("progress_float", 0.0))
        state = snap.get("state", "unknown")
        if running:
            saw_running = True

        if (not history) or state != history[-1].get("state") or abs(progress_float - float(history[-1].get("progress_float", 0.0))) >= 0.01:
            history.append(
                {
                    "t": round(elapsed, 2),
                    "state": state,
                    "progress": snap.get("progress"),
                    "progress_float": round(progress_float, 4),
                }
            )

        completed = (saw_running and not running and not paused) or progress_float >= 1.0
        if completed:
            return json.dumps(
                {
                    "completed": True,
                    "elapsed_seconds": round(elapsed, 2),
                    "final_state": state,
                    "progress": snap.get("progress"),
                    "progress_float": round(progress_float, 4),
                    "history": history[-40:],
                },
                indent=2,
            )
        if elapsed >= timeout_s:
            return json.dumps(
                {
                    "completed": False,
                    "reason": "timeout",
                    "elapsed_seconds": round(elapsed, 2),
                    "state": state,
                    "sim_running": running,
                    "sim_paused": paused,
                    "progress": snap.get("progress"),
                    "progress_float": round(progress_float, 4),
                    "history": history[-40:],
                },
                indent=2,
            )
        time.sleep(poll_s)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def start_simulation() -> str:
    """
    Starts AODT worker-driven simulation (equivalent to the toolbar Play flow),
    including validation checks required by AODT.
    """
    return _run("""
import carb.settings
import omni.usd
import omni.kit.usd.layers as layers
from pxr import Sdf
from aodt.common import constants, messages
from aodt.common.prims import get_total_ues
from aodt.configuration.worker_manager import get_worker_manager_instance
from aodt.progress_bar.progress_bar import ProgressModel
from aodt.telemetry import TelemetryExt
from aodt.toolbar.extension import validate_ref_freq, validate_ru_du_assignment

settings = carb.settings.get_settings()
stage = omni.usd.get_context().get_stage()
if not stage:
    print("Error: no stage loaded.")
    raise SystemExit
if not stage.GetPrimAtPath("/Scenario").IsValid():
    print("Error: /Scenario prim not found.")
    raise SystemExit

worker_uuid = settings.get(constants.WORKER_UUID_SETTING_PATH) or ""
if not worker_uuid:
    print("Error: worker is not attached. Attach worker from AODT before starting simulation.")
    raise SystemExit

live_syncing = layers.get_layers().get_live_syncing()
if not (live_syncing and live_syncing.is_stage_in_live_session()):
    ok = get_worker_manager_instance().create_or_join_live_session(force_create=True)
    if not ok:
        print("Error: failed to create/join live session.")
        raise SystemExit

panel_parent = stage.GetPrimAtPath("/Panels")
has_panel = bool(panel_parent and panel_parent.IsValid() and any(
    p.GetName().startswith("panel_") for p in panel_parent.GetChildren()
))
if not has_panel:
    print("Error: no panel found in /Panels. Create at least one panel first.")
    raise SystemExit

if not TelemetryExt.is_ue_mobility_in_sync_with_db():
    print("Error: UE mobility is not in sync with DB. Generate mobility first.")
    raise SystemExit

total_ues = get_total_ues()
if not total_ues:
    print("Error: no UEs found in stage.")
    raise SystemExit

freq_errors = validate_ref_freq()
du_errors = validate_ru_du_assignment()
all_errors = list(freq_errors.union(du_errors))
if all_errors:
    print("Error: simulation validation failed:")
    for e in sorted(all_errors):
        print(f"  - {e}")
    raise SystemExit

scenario_prim = stage.GetPrimAtPath("/Scenario")
scenario_prim.CreateAttribute("sim:num_users", Sdf.ValueTypeNames.UInt, True).Set(total_ues)

pm = ProgressModel.get_model_instance()
if pm.is_sim_running() and not pm.is_sim_paused():
    print("Error: simulation is already running.")
    raise SystemExit

pm.handle_progress(0.0)
pm.set_sim_paused(False)
TelemetryExt.update_rays_enabled_ru_ue_pairs(update_raypaths=False)
get_worker_manager_instance().send_message(messages.StartSimRequest())
print("StartSimRequest sent to worker.")
""")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def stop_simulation() -> str:
    """Pauses a worker-driven AODT simulation."""
    return _run("""
import carb.settings
from aodt.common import constants, messages
from aodt.configuration.worker_manager import get_worker_manager_instance
from aodt.progress_bar.progress_bar import ProgressModel

settings = carb.settings.get_settings()
worker_uuid = settings.get(constants.WORKER_UUID_SETTING_PATH) or ""
if not worker_uuid:
    print("Error: worker is not attached.")
else:
    ProgressModel.get_model_instance().set_sim_paused(True)
    get_worker_manager_instance().send_message(messages.PauseSimRequest())
    print("PauseSimRequest sent to worker.")
""")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
def reset_simulation() -> str:
    """Stops worker simulation and resets timeline to t=0."""
    return _run("""
import carb.settings
import omni.timeline
from aodt.common import constants, messages
from aodt.configuration.worker_manager import get_worker_manager_instance
from aodt.progress_bar.progress_bar import ProgressModel

settings = carb.settings.get_settings()
worker_uuid = settings.get(constants.WORKER_UUID_SETTING_PATH) or ""
if worker_uuid:
    get_worker_manager_instance().send_message(messages.StopSimRequest())
    ProgressModel.get_model_instance().set_sim_paused(False)
    ProgressModel.get_model_instance().handle_progress(-1.0)
    print("StopSimRequest sent to worker.")
else:
    print("Warning: worker not attached; only timeline will be reset.")
omni.timeline.get_timeline_interface().stop()
print("Timeline reset to t=0.")
""")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def generate_mobility() -> str:
    """
    Triggers AODT mobility generation through the worker pipeline.
    """
    return _run("""
import time
import carb.settings
import omni.usd
from pxr import Sdf
from aodt.common import constants, messages
from aodt.common.prims import get_total_ues
from aodt.configuration.worker_manager import get_worker_manager_instance

settings = carb.settings.get_settings()
stage = omni.usd.get_context().get_stage()
if not stage:
    print("Error: no stage loaded.")
    raise SystemExit
worker_uuid = settings.get(constants.WORKER_UUID_SETTING_PATH) or ""
if not worker_uuid:
    print("Error: worker is not attached.")
    raise SystemExit
scene_url = stage.GetRootLayer().realPath
if not scene_url:
    print("Error: current stage must be saved before generating mobility.")
    raise SystemExit

scenario_prim = stage.GetPrimAtPath("/Scenario")
if scenario_prim.IsValid():
    scenario_prim.CreateAttribute("sim:num_users", Sdf.ValueTypeNames.UInt, True).Set(get_total_ues())

wm = get_worker_manager_instance()
live_session_name = constants.LIVE_SESSION_PREFIX + (settings.get(constants.SESSION_NAME_SETTING_PATH) or "")
wm.skip_next_db_update = True
wm.send_message(messages.OpenSceneRequest(scene_url=scene_url, live_session_name=live_session_name))
time.sleep(0.3)
wm.send_message(messages.MobilityRequest())
print("MobilityRequest sent to worker.")
""")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True))
def get_aodt_runtime_context() -> str:
    """
    Returns a concise readiness/context snapshot for agentic control.
    """
    return _run("""
import json
import carb.settings
import omni.kit.usd.layers as layers
import omni.usd
from aodt.common import constants
from aodt.common.utils import get_scale_factor, get_stage_meters_per_unit
from aodt.progress_bar.progress_bar import ProgressModel

stage = omni.usd.get_context().get_stage()
settings = carb.settings.get_settings()
live_syncing = layers.get_layers().get_live_syncing()
in_live = bool(live_syncing and live_syncing.is_stage_in_live_session())
worker_uuid = settings.get(constants.WORKER_UUID_SETTING_PATH) or ""
db_connected = bool(settings.get(constants.DB_CONNECTED_STATE_PATH))
scenario = stage.GetPrimAtPath("/Scenario") if stage else None
ru_count = len(stage.GetPrimAtPath("/RUs").GetChildren()) if stage and stage.GetPrimAtPath("/RUs").IsValid() else 0
du_count = len(stage.GetPrimAtPath("/DUs").GetChildren()) if stage and stage.GetPrimAtPath("/DUs").IsValid() else 0
ue_count = len(stage.GetPrimAtPath("/UEs").GetChildren()) if stage and stage.GetPrimAtPath("/UEs").IsValid() else 0
panel_count = len(stage.GetPrimAtPath("/Panels").GetChildren()) if stage and stage.GetPrimAtPath("/Panels").IsValid() else 0
pm = ProgressModel.get_model_instance()

payload = {
    "stage_loaded": bool(stage),
    "stage_path": stage.GetRootLayer().realPath if stage else "",
    "stage_meters_per_unit": get_stage_meters_per_unit(),
    "asset_scale_factor": get_scale_factor(),
    "has_scenario_prim": bool(scenario and scenario.IsValid()),
    "worker_attached": bool(worker_uuid),
    "worker_uuid": worker_uuid,
    "db_connected": db_connected,
    "in_live_session": in_live,
    "sim_running": pm.is_sim_running(),
    "sim_paused": pm.is_sim_paused(),
    "sim_progress": pm.get_value_as_string(),
    "counts": {
        "rus": ru_count,
        "dus": du_count,
        "ues": ue_count,
        "panels": panel_count,
    },
}
print(json.dumps(payload, indent=2))
""")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True))
def get_recent_aodt_logs(max_lines: int = 200) -> str:
    """
    Returns recent lines from the current Kit log and latest aodt.control log.
    """
    code = f"""
import glob
import os
import carb.settings
import carb.tokens

_max_lines = max(20, min(int({max_lines}), 2000))

def _tail(path, n):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        data = f.readlines()
    return data[-n:]

settings = carb.settings.get_settings()
kit_log = settings.get("/log/file")
print(f"Requested lines: {{_max_lines}}")
print()

if kit_log and os.path.isfile(kit_log):
    print(f"=== KIT LOG: {{kit_log}} ===")
    for line in _tail(kit_log, _max_lines):
        print(line.rstrip("\\n"))
else:
    print(f"KIT log not found: {{kit_log!r}}")

print()
control_dir = carb.tokens.get_tokens_interface().resolve("${{omni_logs}}") + "/Kit/aodt.control"
control_logs = sorted(glob.glob(os.path.join(control_dir, "control_*.log")), key=os.path.getmtime)
if control_logs:
    latest = control_logs[-1]
    print(f"=== AODT CONTROL LOG: {{latest}} ===")
    for line in _tail(latest, _max_lines):
        print(line.rstrip("\\n"))
else:
    print(f"No aodt.control logs found under {{control_dir}}")
    """
    return _run(code)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True))
def stream_aodt_logs(max_new_lines: int = 200, reset_cursor: bool = False) -> str:
    """
    Streams only new log lines since the previous call (Kit + latest aodt.control log).
    Keeps per-log cursors inside AODT Python exec scope for low-noise observability.
    """
    code = f"""
import glob
import json
import os
import carb.settings
import carb.tokens

_max_lines = max(20, min(int({max_new_lines}), 2000))
_reset = {str(reset_cursor)}

if "__mcp_log_cursors" not in globals():
    __mcp_log_cursors = {{}}

def _read_new_lines(path, key, max_lines, reset):
    if reset:
        __mcp_log_cursors[key] = 0
    if key not in __mcp_log_cursors:
        __mcp_log_cursors[key] = 0
    cursor = int(__mcp_log_cursors.get(key, 0) or 0)
    if not os.path.isfile(path):
        return {{
            "path": path,
            "found": False,
            "new_lines": [],
            "cursor": 0,
            "truncated": False,
        }}
    size = os.path.getsize(path)
    if cursor > size:
        cursor = 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(cursor)
        chunk = f.read()
        cursor = f.tell()
    __mcp_log_cursors[key] = cursor
    lines = chunk.splitlines()
    truncated = False
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        truncated = True
    return {{
        "path": path,
        "found": True,
        "new_lines": lines,
        "cursor": cursor,
        "truncated": truncated,
    }}

settings = carb.settings.get_settings()
kit_log = settings.get("/log/file")
control_dir = carb.tokens.get_tokens_interface().resolve("${{omni_logs}}") + "/Kit/aodt.control"
control_logs = sorted(glob.glob(os.path.join(control_dir, "control_*.log")), key=os.path.getmtime)
control_log = control_logs[-1] if control_logs else ""

payload = {{
    "cursor_reset": _reset,
    "max_new_lines": _max_lines,
    "kit": _read_new_lines(kit_log, "kit_log", _max_lines, _reset) if kit_log else {{"found": False, "path": "", "new_lines": [], "cursor": 0, "truncated": False}},
    "control": _read_new_lines(control_log, "control_log", _max_lines, _reset) if control_log else {{"found": False, "path": control_log, "new_lines": [], "cursor": 0, "truncated": False}},
}}

print(json.dumps(payload, indent=2))
"""
    return _run(code)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def refresh_raypaths(read_db: bool = True) -> str:
    """
    Refreshes ray path visualization from telemetry settings/database.
    """
    code = f"""
from aodt.telemetry import TelemetryExt
TelemetryExt.update_rays_enabled_ru_ue_pairs(update_raypaths=False)
TelemetryExt.update_raypaths(read_db={str(read_db)})
pairs = TelemetryExt.get_rays_enabled_ru_ue_pairs()
print(f"Raypath refresh requested (read_db={read_db}). Enabled RU-UE pairs: {{len(pairs)}}")
for ru_id, ue_id in pairs[:30]:
    print(f"  RU {{ru_id}} -> UE {{ue_id}}")
if len(pairs) > 30:
    print(f"  ... and {{len(pairs)-30}} more pairs")
"""
    return _run(code)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def set_ray_pair_enabled(ru_prim_path: str, ue_prim_path: str, enabled: bool = True) -> str:
    """
    Enables/disables ray tracing visibility for a specific RU-UE pair.
    """
    code = f"""
import re
import omni.usd
from pxr import Sdf, Vt
from aodt.telemetry import TelemetryExt

_ru_path = {json.dumps(ru_prim_path)}
_ue_path = {json.dumps(ue_prim_path)}
_enabled = {str(enabled)}
{_WRITABLE_EDIT_TARGET_SNIPPET}
if not stage:
    print("Error: no stage loaded.")
    raise SystemExit
ru = stage.GetPrimAtPath(_ru_path)
ue = stage.GetPrimAtPath(_ue_path)
if not ru.IsValid():
    print(f"Error: invalid RU path {{_ru_path}}")
    raise SystemExit
if not ue.IsValid():
    print(f"Error: invalid UE path {{_ue_path}}")
    raise SystemExit

rm = re.search(r"ru_(\\d+)", _ru_path)
if not rm:
    print(f"Error: unable to infer RU id from {{_ru_path}}")
    raise SystemExit
ru_id = int(rm.group(1))

if not ru.HasAttribute("aerial:gnb:enable_rays"):
    ru.CreateAttribute("aerial:gnb:enable_rays", Sdf.ValueTypeNames.Bool, True)
ru.GetAttribute("aerial:gnb:enable_rays").Set(True if _enabled else False)

if ue.HasAttribute("aerial:ue:rays_enabled_ru_ids"):
    ids = ue.GetAttribute("aerial:ue:rays_enabled_ru_ids").Get()
    ids = [] if ids is None else list(ids)
else:
    ue.CreateAttribute("aerial:ue:rays_enabled_ru_ids", Sdf.ValueTypeNames.IntArray, True)
    ids = []

if _enabled and ru_id not in ids:
    ids.append(ru_id)
if (not _enabled) and ru_id in ids:
    ids.remove(ru_id)
ids = sorted(set(ids))
ue.GetAttribute("aerial:ue:rays_enabled_ru_ids").Set(Vt.IntArray(ids))

pairs = TelemetryExt.update_rays_enabled_ru_ue_pairs(update_raypaths=False) or []
print(f"Pair {{'enabled' if _enabled else 'disabled'}}: RU {{ru_id}} <-> {{_ue_path}}")
print(f"Current enabled pair count: {{len(pairs)}}")
"""
    return _run(code)


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
if not stage:
    print("Error: no stage loaded.")
    raise SystemExit

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
def create_panel() -> str:
    """
    Creates a new panel prim under /Panels with the next available ID.
    """
    return _run(f"""
import omni.usd
{_WRITABLE_EDIT_TARGET_SNIPPET}
from aodt.common.prims import create_panel_prim
path = create_panel_prim()
print(f"Panel created at {{path}}")
""")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True))
def list_panels() -> str:
    """
    Lists all panel prims and key RF attributes.
    """
    return _run("""
import omni.usd
stage = omni.usd.get_context().get_stage()
parent = stage.GetPrimAtPath("/Panels")
if not parent.IsValid():
    print("No /Panels prim found.")
else:
    panels = list(parent.GetChildren())
    print(f"Panels ({len(panels)}):")
    for p in panels:
        attrs = {}
        for name in (
            "aerial:panel:reference_freq",
            "aerial:panel:num_horizontal_elements",
            "aerial:panel:num_vertical_elements",
            "aerial:panel:dual_polarized",
        ):
            a = p.GetAttribute(name)
            attrs[name] = a.Get() if a and a.IsValid() else None
        print(f"  {p.GetPath()}: {attrs}")
""")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def set_default_panels(ru_panel_name: str = "", ue_panel_name: str = "") -> str:
    """
    Sets default panel assignments on /Scenario (sim:gnb:panel_type, sim:ue:panel_type).
    """
    code = f"""
import omni.usd
from pxr import Sdf
_ru = {json.dumps(ru_panel_name)}
_ue = {json.dumps(ue_panel_name)}
{_WRITABLE_EDIT_TARGET_SNIPPET}
if not stage:
    print("Error: no stage loaded.")
    raise SystemExit
scenario = stage.GetPrimAtPath("/Scenario")
if not scenario.IsValid():
    print("Error: /Scenario prim not found.")
    raise SystemExit

if _ru:
    if not scenario.HasAttribute("sim:gnb:panel_type"):
        scenario.CreateAttribute("sim:gnb:panel_type", Sdf.ValueTypeNames.String, True)
    scenario.GetAttribute("sim:gnb:panel_type").Set(_ru)
    print(f"Set sim:gnb:panel_type = {{_ru!r}}")
if _ue:
    if not scenario.HasAttribute("sim:ue:panel_type"):
        scenario.CreateAttribute("sim:ue:panel_type", Sdf.ValueTypeNames.String, True)
    scenario.GetAttribute("sim:ue:panel_type").Set(_ue)
    print(f"Set sim:ue:panel_type = {{_ue!r}}")
if (not _ru) and (not _ue):
    print("No panel values provided.")
"""
    return _run(code)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def create_ue(position: list[float], position_units: str = "stage") -> str:
    """
    Places a new User Equipment (UE / mobile device) in the AODT scene.

    Args:
        position: [x, y, z] world coordinates.
        position_units: 'stage' (default), 'meters', or 'centimeters'.
    """
    code = f"""
import re, omni.usd, carb
from pxr import Sdf, UsdGeom, Gf
from aodt.common.constants import UE_ASSET_SETTING_PATH
from aodt.common.prims import get_stage_next_free_path
from aodt.common.utils import get_scale_factor, get_stage_meters_per_unit

_pos_in = {json.dumps(position)}
_units = {json.dumps(position_units)}.lower()
{_WRITABLE_EDIT_TARGET_SNIPPET}
if not stage:
    print("Error: no stage loaded.")
    raise SystemExit
stage_units = get_stage_meters_per_unit()

if _units in ("meter", "meters", "m"):
    unit_scale = 1.0 / stage_units
elif _units in ("centimeter", "centimeters", "cm"):
    unit_scale = 0.01 / stage_units
elif _units in ("stage", "unit", "units"):
    unit_scale = 1.0
else:
    print(f"Error: unsupported position_units '{{_units}}'. Use 'stage', 'meters', or 'centimeters'.")
    raise SystemExit
_pos = [float(v) * unit_scale for v in _pos_in]

path = get_stage_next_free_path(stage, "/UEs/ue", False)
prim = stage.DefinePrim(path)

m = re.search(r'/UEs/ue_(\\d+)', path)
ue_id = int(m.group(1)) if m else 1
prim.CreateAttribute("aerial:ue:user_id",       Sdf.ValueTypeNames.Int).Set(ue_id)
prim.CreateAttribute("aerial:ue:manual",         Sdf.ValueTypeNames.Bool).Set(True)
prim.CreateAttribute("aerial:ue:radiated_power", Sdf.ValueTypeNames.Float).Set(0.001)
prim.CreateAttribute("aerial:ue:mech_tilt",      Sdf.ValueTypeNames.Float).Set(0.0)
prim.CreateAttribute("aerial:ue:panel_type",     Sdf.ValueTypeNames.String)

# Inherit height / radius / panel_type from /Scenario if present
ue_height_m = 1.5
ue_radius_m = 0.5
scenario = stage.GetPrimAtPath("/Scenario")
if scenario.IsValid():
    if scenario.HasAttribute("sim:ue:height"):
        ue_height_m = scenario.GetAttribute("sim:ue:height").Get()
    if scenario.HasAttribute("sim:ue:radius"):
        ue_radius_m = scenario.GetAttribute("sim:ue:radius").Get()
    if scenario.HasAttribute("sim:ue:panel_type"):
        prim.GetAttribute("aerial:ue:panel_type").Set(
            scenario.GetAttribute("sim:ue:panel_type").Get())

# Z offset so UE base sits at the given z (mirrors create_ue_prim offset logic)
offset = (ue_height_m / 2 + ue_radius_m) / stage_units
UsdGeom.Xformable(prim).AddTranslateOp().Set(Gf.Vec3d(_pos[0], _pos[1], _pos[2] + offset))
scale_attr = prim.GetAttribute("xformOp:scale")
if scale_attr and scale_attr.Get() is not None:
    scale_attr.Set(scale_attr.Get() * get_scale_factor())

# AddReference LAST — no attribute reads or viewport selection after this call.
# Placing it last keeps the Nucleus fetch async and avoids stalling the update stream.
ue_asset = carb.settings.get_settings().get(UE_ASSET_SETTING_PATH)
if ue_asset:
    prim.GetReferences().AddReference(ue_asset)
else:
    prim.SetTypeName("Capsule")  # fallback visual if no asset configured

print(f"UE created at {{path}}  input_position={{_pos_in}}  stage_position={{_pos}} units={{_units}}")
"""
    return _run(code)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def create_ru(position: list[float], position_units: str = "stage") -> str:
    """
    Places a new Radio Unit (RU / base station antenna) in the AODT scene.

    Args:
        position: [x, y, z] world coordinates.
        position_units: 'stage' (default), 'meters', or 'centimeters'.
    """
    code = f"""
import re, omni.usd, carb
from pxr import Sdf, UsdGeom, Gf
from aodt.common.constants import RU_ASSET_SETTING_PATH
from aodt.common.prims import get_stage_next_free_path
from aodt.common.utils import get_scale_factor, get_stage_meters_per_unit
from aodt.deployer.model import get_ru_model_instance

_pos_in = {json.dumps(position)}
_units = {json.dumps(position_units)}.lower()
{_WRITABLE_EDIT_TARGET_SNIPPET}
if not stage:
    print("Error: no stage loaded.")
    raise SystemExit
stage_units = get_stage_meters_per_unit()

if _units in ("meter", "meters", "m"):
    unit_scale = 1.0 / stage_units
elif _units in ("centimeter", "centimeters", "cm"):
    unit_scale = 0.01 / stage_units
elif _units in ("stage", "unit", "units"):
    unit_scale = 1.0
else:
    print(f"Error: unsupported position_units '{{_units}}'. Use 'stage', 'meters', or 'centimeters'.")
    raise SystemExit
_pos = [float(v) * unit_scale for v in _pos_in]

path = get_stage_next_free_path(stage, "/RUs/ru", False)
prim = stage.DefinePrim(path)

m = re.search(r'/RUs/ru_(\\d+)', path)
cell_id = int(m.group(1)) if m else 1
prim.CreateAttribute("aerial:gnb:cell_id",      Sdf.ValueTypeNames.Int).Set(cell_id)
prim.CreateAttribute("aerial:gnb:mech_azimuth", Sdf.ValueTypeNames.Float).Set(0.0)
prim.CreateAttribute("aerial:gnb:height",       Sdf.ValueTypeNames.Float).Set(0.5)
prim.CreateAttribute("aerial:gnb:panel_type",   Sdf.ValueTypeNames.String)

# Inherit panel_type from /Scenario if present
scenario = stage.GetPrimAtPath("/Scenario")
if scenario.IsValid() and scenario.HasAttribute("sim:gnb:panel_type"):
    prim.GetAttribute("aerial:gnb:panel_type").Set(
        scenario.GetAttribute("sim:gnb:panel_type").Get())

ru_h_m = prim.GetAttribute("aerial:gnb:height").Get() if prim.HasAttribute("aerial:gnb:height") else 0.0
ru_z = _pos[2] + (ru_h_m / stage_units) / 2.0
UsdGeom.Xformable(prim).AddTranslateOp().Set(Gf.Vec3d(_pos[0], _pos[1], ru_z))
scale_attr = prim.GetAttribute("xformOp:scale")
if scale_attr and scale_attr.Get() is not None:
    scale_attr.Set(scale_attr.Get() * get_scale_factor())

# DU assignment (reads Panel for num_antennas/reference_freq; silent no-op if no Panel)
try:
    get_ru_model_instance().assign_ru_to_du(prim)
except Exception:
    pass

# AddReference LAST — no attribute reads or viewport selection after this call.
# Placing it last keeps the Nucleus fetch async and avoids stalling the update stream.
ru_asset = carb.settings.get_settings().get(RU_ASSET_SETTING_PATH)
if ru_asset:
    prim.GetReferences().AddReference(ru_asset)

print(f"RU created at {{path}}  input_position={{_pos_in}}  stage_position={{_pos}} units={{_units}}")
"""
    return _run(code)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def create_du(position: list[float], position_units: str = "stage") -> str:
    """
    Places a new Distributed Unit (DU / baseband processing unit) in the AODT scene.
    DUs connect to RUs and carry the baseband configuration (num_antennas, reference_freq).

    Args:
        position: [x, y, z] world coordinates.
        position_units: 'stage' (default), 'meters', or 'centimeters'.
    """
    code = f"""
import re, omni.usd, carb
from pxr import Sdf, UsdGeom, Gf
from aodt.common.constants import DU_ASSET_SETTING_PATH
from aodt.common.prims import get_stage_next_free_path
from aodt.common.utils import get_scale_factor, get_stage_meters_per_unit
from aodt.deployer.model import DU_TO_BUILDING_METERS, MIN_DU_Z_METERS, get_ru_model_instance

_pos_in = {json.dumps(position)}
_units = {json.dumps(position_units)}.lower()
{_WRITABLE_EDIT_TARGET_SNIPPET}
if not stage:
    print("Error: no stage loaded.")
    raise SystemExit
stage_units = get_stage_meters_per_unit()

if _units in ("meter", "meters", "m"):
    unit_scale = 1.0 / stage_units
elif _units in ("centimeter", "centimeters", "cm"):
    unit_scale = 0.01 / stage_units
elif _units in ("stage", "unit", "units"):
    unit_scale = 1.0
else:
    print(f"Error: unsupported position_units '{{_units}}'. Use 'stage', 'meters', or 'centimeters'.")
    raise SystemExit
_pos = [float(v) * unit_scale for v in _pos_in]

path = get_stage_next_free_path(stage, "/DUs/du", False)
prim = stage.DefinePrim(path)

m = re.search(r'/DUs/du_(\\d+)', path)
du_id = int(m.group(1)) if m else 1
prim.CreateAttribute("aerial:du:id",             Sdf.ValueTypeNames.Int).Set(du_id)
prim.CreateAttribute("aerial:du:num_antennas",   Sdf.ValueTypeNames.Int)
prim.CreateAttribute("aerial:du:reference_freq", Sdf.ValueTypeNames.Double)

du_z = max(_pos[2] + DU_TO_BUILDING_METERS / 2.0 / stage_units, MIN_DU_Z_METERS / 2.0 / stage_units)
UsdGeom.Xformable(prim).AddTranslateOp().Set(Gf.Vec3d(_pos[0], _pos[1], du_z))
scale_attr = prim.GetAttribute("xformOp:scale")
if scale_attr and scale_attr.Get() is not None:
    scale_attr.Set(scale_attr.Get() * get_scale_factor())

# Re-evaluate RU->DU assignments now that a new DU exists
try:
    get_ru_model_instance().refresh_ru_du_associations()
except Exception:
    pass

# AddReference LAST — same reasoning as create_ru / create_ue.
du_asset = carb.settings.get_settings().get(DU_ASSET_SETTING_PATH)
if du_asset:
    prim.GetReferences().AddReference(du_asset)

print(f"DU created at {{path}}  input_position={{_pos_in}}  stage_position={{_pos}} units={{_units}}")
"""
    return _run(code)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def create_tx_rx_pair(
    tx_position: list[float],
    rx_position: list[float],
    position_units: str = "meters",
    enable_rays: bool = True,
) -> str:
    """
    Creates a basic TX/RX pair for AODT:
    - TX: RU at tx_position
    - RX: UE at rx_position
    Optionally enables ray-path visibility for that pair.
    """
    code = f"""
import re, omni.usd, carb
from pxr import Sdf, UsdGeom, Gf, Vt
from aodt.common.constants import RU_ASSET_SETTING_PATH, UE_ASSET_SETTING_PATH
from aodt.common.prims import get_stage_next_free_path
from aodt.common.utils import get_scale_factor, get_stage_meters_per_unit
from aodt.deployer.model import get_ru_model_instance
from aodt.telemetry import TelemetryExt

_tx_in = {json.dumps(tx_position)}
_rx_in = {json.dumps(rx_position)}
_units = {json.dumps(position_units)}.lower()
_enable_rays = {str(enable_rays)}
{_WRITABLE_EDIT_TARGET_SNIPPET}
if not stage:
    print("Error: no stage loaded.")
    raise SystemExit
stage_units = get_stage_meters_per_unit()
if _units in ("meter", "meters", "m"):
    unit_scale = 1.0 / stage_units
elif _units in ("centimeter", "centimeters", "cm"):
    unit_scale = 0.01 / stage_units
elif _units in ("stage", "unit", "units"):
    unit_scale = 1.0
else:
    print(f"Error: unsupported position_units '{{_units}}'")
    raise SystemExit
_tx = [float(v) * unit_scale for v in _tx_in]
_rx = [float(v) * unit_scale for v in _rx_in]

# --- Create RU (TX)
ru_path = get_stage_next_free_path(stage, "/RUs/ru", False)
ru = stage.DefinePrim(ru_path)
m = re.search(r'/RUs/ru_(\\d+)', ru_path)
ru_id = int(m.group(1)) if m else 1
ru.CreateAttribute("aerial:gnb:cell_id", Sdf.ValueTypeNames.Int).Set(ru_id)
ru.CreateAttribute("aerial:gnb:mech_azimuth", Sdf.ValueTypeNames.Float).Set(0.0)
ru.CreateAttribute("aerial:gnb:height", Sdf.ValueTypeNames.Float).Set(0.5)
ru.CreateAttribute("aerial:gnb:panel_type", Sdf.ValueTypeNames.String)
if not ru.HasAttribute("aerial:gnb:enable_rays"):
    ru.CreateAttribute("aerial:gnb:enable_rays", Sdf.ValueTypeNames.Bool, True)
ru.GetAttribute("aerial:gnb:enable_rays").Set(True if _enable_rays else False)
scenario = stage.GetPrimAtPath("/Scenario")
if scenario.IsValid() and scenario.HasAttribute("sim:gnb:panel_type"):
    ru.GetAttribute("aerial:gnb:panel_type").Set(scenario.GetAttribute("sim:gnb:panel_type").Get())
ru_h_m = ru.GetAttribute("aerial:gnb:height").Get() if ru.HasAttribute("aerial:gnb:height") else 0.0
ru_z = _tx[2] + (ru_h_m / stage_units) / 2.0
UsdGeom.Xformable(ru).AddTranslateOp().Set(Gf.Vec3d(_tx[0], _tx[1], ru_z))
if ru.GetAttribute("xformOp:scale") and ru.GetAttribute("xformOp:scale").Get() is not None:
    ru.GetAttribute("xformOp:scale").Set(ru.GetAttribute("xformOp:scale").Get() * get_scale_factor())
try:
    get_ru_model_instance().assign_ru_to_du(ru)
except Exception:
    pass
ru_asset = carb.settings.get_settings().get(RU_ASSET_SETTING_PATH)
if ru_asset:
    ru.GetReferences().AddReference(ru_asset)

# --- Create UE (RX)
ue_path = get_stage_next_free_path(stage, "/UEs/ue", False)
ue = stage.DefinePrim(ue_path)
m = re.search(r'/UEs/ue_(\\d+)', ue_path)
ue_id = int(m.group(1)) if m else 1
ue.CreateAttribute("aerial:ue:user_id", Sdf.ValueTypeNames.Int).Set(ue_id)
ue.CreateAttribute("aerial:ue:manual", Sdf.ValueTypeNames.Bool).Set(True)
ue.CreateAttribute("aerial:ue:radiated_power", Sdf.ValueTypeNames.Float).Set(0.001)
ue.CreateAttribute("aerial:ue:mech_tilt", Sdf.ValueTypeNames.Float).Set(0.0)
ue.CreateAttribute("aerial:ue:panel_type", Sdf.ValueTypeNames.String)
ue_h_m = 1.5
ue_r_m = 0.5
if scenario.IsValid():
    if scenario.HasAttribute("sim:ue:height"):
        ue_h_m = scenario.GetAttribute("sim:ue:height").Get()
    if scenario.HasAttribute("sim:ue:radius"):
        ue_r_m = scenario.GetAttribute("sim:ue:radius").Get()
    if scenario.HasAttribute("sim:ue:panel_type"):
        ue.GetAttribute("aerial:ue:panel_type").Set(scenario.GetAttribute("sim:ue:panel_type").Get())
ue_off = (ue_h_m / 2 + ue_r_m) / stage_units
UsdGeom.Xformable(ue).AddTranslateOp().Set(Gf.Vec3d(_rx[0], _rx[1], _rx[2] + ue_off))
if ue.GetAttribute("xformOp:scale") and ue.GetAttribute("xformOp:scale").Get() is not None:
    ue.GetAttribute("xformOp:scale").Set(ue.GetAttribute("xformOp:scale").Get() * get_scale_factor())
ue_asset = carb.settings.get_settings().get(UE_ASSET_SETTING_PATH)
if ue_asset:
    ue.GetReferences().AddReference(ue_asset)

# Enable rays for the pair if requested.
if _enable_rays:
    if ue.HasAttribute("aerial:ue:rays_enabled_ru_ids"):
        ids = ue.GetAttribute("aerial:ue:rays_enabled_ru_ids").Get()
        ids = [] if ids is None else list(ids)
    else:
        ue.CreateAttribute("aerial:ue:rays_enabled_ru_ids", Sdf.ValueTypeNames.IntArray, True)
        ids = []
    if ru_id not in ids:
        ids.append(ru_id)
    ue.GetAttribute("aerial:ue:rays_enabled_ru_ids").Set(Vt.IntArray(sorted(set(ids))))
    TelemetryExt.update_rays_enabled_ru_ue_pairs(update_raypaths=False)

print(f"TX RU: {{ru_path}}  RX UE: {{ue_path}}  units={{_units}}  rays_enabled={{_enable_rays}}")
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

home = os.path.expanduser("~")
search_paths = [
    f"{home}/.local/share/ov/pkg/aodt-1.4.1/assets",
    f"{home}/aodt_1.4.1/assets",
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

home = os.path.expanduser("~")
search_paths = [
    f"{home}/.local/share/ov/pkg/aodt-1.4.1/assets",
    f"{home}/aodt_1.4.1/assets",
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


# ─── 13. Guarded Workflow Layer ───────────────────────────────────────────────

def _try_parse_json(text: str):
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        pass
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(s):
        if ch not in "{[":
            continue
        try:
            obj, _ = decoder.raw_decode(s[idx:])
            return obj
        except Exception:
            continue
    return None


def _operation_failed(output: str) -> bool:
    if output is None:
        return True
    text = str(output)
    if text.strip().startswith("Error:"):
        return True
    if text.strip().startswith("Execution failed:"):
        return True
    return _looks_like_aodt_error_output(text)


def _run_readiness_snapshot() -> tuple[dict, str]:
    raw = validate_control_readiness(require_live_session=False, require_saved_stage=False)
    parsed = _try_parse_json(raw)
    if isinstance(parsed, dict):
        return parsed, raw
    return {}, raw


def _get_guarded_operation_registry() -> dict:
    return {
        "new_stage": new_stage,
        "load_stage": load_stage,
        "save_stage": save_stage,
        "list_network_entities": list_network_entities,
        "get_scenario_info": get_scenario_info,
        "create_panel": create_panel,
        "list_panels": list_panels,
        "set_default_panels": set_default_panels,
        "create_ue": create_ue,
        "create_ru": create_ru,
        "create_du": create_du,
        "create_tx_rx_pair": create_tx_rx_pair,
        "set_ray_pair_enabled": set_ray_pair_enabled,
        "refresh_raypaths": refresh_raypaths,
        "get_simulation_status": get_simulation_status,
        "start_simulation": start_simulation,
        "stop_simulation": stop_simulation,
        "reset_simulation": reset_simulation,
        "generate_mobility": generate_mobility,
        "wait_for_mobility_sync": wait_for_mobility_sync,
        "wait_for_sim_completion": wait_for_sim_completion,
        "get_aodt_runtime_context": get_aodt_runtime_context,
        "get_recent_aodt_logs": get_recent_aodt_logs,
        "stream_aodt_logs": stream_aodt_logs,
        "validate_control_readiness": validate_control_readiness,
    }


def _execute_guarded_operation_internal(operation: str, args: dict, auto_fix: bool = True) -> dict:
    operation = (operation or "").strip().lower()
    args = args or {}
    registry = _get_guarded_operation_registry()
    if operation not in registry:
        return {
            "success": False,
            "operation": operation,
            "error": f"Unsupported guarded operation '{operation}'.",
            "supported_operations": sorted(registry.keys()),
        }

    stage_required = {
        "save_stage",
        "list_network_entities",
        "get_scenario_info",
        "create_panel",
        "list_panels",
        "set_default_panels",
        "create_ue",
        "create_ru",
        "create_du",
        "create_tx_rx_pair",
        "set_ray_pair_enabled",
        "refresh_raypaths",
        "generate_mobility",
        "start_simulation",
        "stop_simulation",
        "reset_simulation",
        "get_simulation_status",
        "validate_control_readiness",
    }
    worker_required = {"generate_mobility", "start_simulation"}
    stage_saved_required = {"generate_mobility"}
    scenario_required = {"start_simulation"}
    panel_required = {"start_simulation"}
    ues_required = {"start_simulation"}
    mobility_sync_required = {"start_simulation"}

    pre_actions = []

    def _record_pre_action(name: str, ok: bool, output: str):
        pre_actions.append({"action": name, "success": bool(ok), "output": output})

    readiness, readiness_raw = _run_readiness_snapshot()
    checks = readiness.get("checks", {}) if isinstance(readiness, dict) else {}
    counts = readiness.get("counts", {}) if isinstance(readiness, dict) else {}
    stage_loaded = bool(checks.get("stage_loaded", False))
    stage_saved = bool(checks.get("stage_saved", False))
    worker_attached = bool(checks.get("worker_attached", False))
    has_scenario = bool(checks.get("has_scenario_prim", False))
    has_panel = bool(checks.get("has_panel", False))
    mobility_in_sync = bool(checks.get("mobility_in_sync_with_db", False))
    total_ues = int(counts.get("total_ues", 0) or 0)

    if operation in stage_required and not stage_loaded:
        if auto_fix and operation in {"create_panel", "create_ue", "create_ru", "create_du", "create_tx_rx_pair"}:
            out = new_stage()
            ok = not _operation_failed(out)
            _record_pre_action("new_stage", ok, out)
            if not ok:
                return {
                    "success": False,
                    "operation": operation,
                    "error": "Failed to create stage automatically.",
                    "pre_actions": pre_actions,
                    "readiness": readiness or {"raw": readiness_raw},
                }
            readiness, readiness_raw = _run_readiness_snapshot()
            checks = readiness.get("checks", {}) if isinstance(readiness, dict) else {}
            counts = readiness.get("counts", {}) if isinstance(readiness, dict) else {}
            stage_loaded = bool(checks.get("stage_loaded", False))
            stage_saved = bool(checks.get("stage_saved", False))
            worker_attached = bool(checks.get("worker_attached", False))
            has_scenario = bool(checks.get("has_scenario_prim", False))
            has_panel = bool(checks.get("has_panel", False))
            mobility_in_sync = bool(checks.get("mobility_in_sync_with_db", False))
            total_ues = int(counts.get("total_ues", 0) or 0)
        else:
            return {
                "success": False,
                "operation": operation,
                "error": "Stage is required but not loaded.",
                "pre_actions": pre_actions,
                "readiness": readiness or {"raw": readiness_raw},
            }

    if operation in worker_required and not worker_attached:
        return {
            "success": False,
            "operation": operation,
            "error": "Worker is not attached. Attach worker in AODT before this operation.",
            "pre_actions": pre_actions,
            "readiness": readiness or {"raw": readiness_raw},
        }

    if operation in scenario_required and not has_scenario:
        return {
            "success": False,
            "operation": operation,
            "error": "/Scenario prim is missing. Load a valid AODT scene before starting simulation.",
            "pre_actions": pre_actions,
            "readiness": readiness or {"raw": readiness_raw},
        }

    if operation in panel_required and not has_panel and auto_fix:
        out = create_panel()
        ok = not _operation_failed(out)
        _record_pre_action("create_panel", ok, out)
        readiness, readiness_raw = _run_readiness_snapshot()
        checks = readiness.get("checks", {}) if isinstance(readiness, dict) else {}
        has_panel = bool(checks.get("has_panel", False))
        if (not ok) or (not has_panel):
            return {
                "success": False,
                "operation": operation,
                "error": "Panel precondition failed. Could not auto-create panel.",
                "pre_actions": pre_actions,
                "readiness": readiness or {"raw": readiness_raw},
            }

    if operation in panel_required and not has_panel:
        return {
            "success": False,
            "operation": operation,
            "error": "No panel found. Create panel(s) before starting simulation.",
            "pre_actions": pre_actions,
            "readiness": readiness or {"raw": readiness_raw},
        }

    if operation in ues_required and total_ues <= 0:
        return {
            "success": False,
            "operation": operation,
            "error": "No UEs found. Create UE(s) before starting simulation.",
            "pre_actions": pre_actions,
            "readiness": readiness or {"raw": readiness_raw},
        }

    if operation in stage_saved_required and not stage_saved and auto_fix:
        out = save_stage()
        ok = not _operation_failed(out)
        _record_pre_action("save_stage", ok, out)
        readiness, readiness_raw = _run_readiness_snapshot()
        checks = readiness.get("checks", {}) if isinstance(readiness, dict) else {}
        stage_saved = bool(checks.get("stage_saved", False))
        if (not ok) or (not stage_saved):
            return {
                "success": False,
                "operation": operation,
                "error": "Stage must be saved before mobility generation; auto-save failed.",
                "pre_actions": pre_actions,
                "readiness": readiness or {"raw": readiness_raw},
            }

    if operation in stage_saved_required and not stage_saved:
        return {
            "success": False,
            "operation": operation,
            "error": "Stage must be saved before mobility generation.",
            "pre_actions": pre_actions,
            "readiness": readiness or {"raw": readiness_raw},
        }

    if operation in mobility_sync_required and not mobility_in_sync and auto_fix:
        if not stage_saved:
            out = save_stage()
            ok = not _operation_failed(out)
            _record_pre_action("save_stage", ok, out)
            readiness, readiness_raw = _run_readiness_snapshot()
            checks = readiness.get("checks", {}) if isinstance(readiness, dict) else {}
            stage_saved = bool(checks.get("stage_saved", False))
            if (not ok) or (not stage_saved):
                return {
                    "success": False,
                    "operation": operation,
                    "error": "Cannot auto-generate mobility because stage is not saved.",
                    "pre_actions": pre_actions,
                    "readiness": readiness or {"raw": readiness_raw},
                }

        out_mob = generate_mobility()
        ok_mob = not _operation_failed(out_mob)
        _record_pre_action("generate_mobility", ok_mob, out_mob)
        if not ok_mob:
            return {
                "success": False,
                "operation": operation,
                "error": "Auto mobility generation failed.",
                "pre_actions": pre_actions,
                "readiness": readiness or {"raw": readiness_raw},
            }

        out_wait = wait_for_mobility_sync(timeout_seconds=180, poll_interval_seconds=1.0)
        parsed_wait = _try_parse_json(out_wait)
        synced = bool(isinstance(parsed_wait, dict) and parsed_wait.get("synced", False))
        _record_pre_action("wait_for_mobility_sync", synced, out_wait)
        readiness, readiness_raw = _run_readiness_snapshot()
        checks = readiness.get("checks", {}) if isinstance(readiness, dict) else {}
        mobility_in_sync = bool(checks.get("mobility_in_sync_with_db", False))
        if not mobility_in_sync:
            return {
                "success": False,
                "operation": operation,
                "error": "Mobility is still not in sync after auto-fix attempts.",
                "pre_actions": pre_actions,
                "readiness": readiness or {"raw": readiness_raw},
            }

    if operation in mobility_sync_required and not mobility_in_sync:
        return {
            "success": False,
            "operation": operation,
            "error": "Mobility is not in sync. Run generate_mobility + wait_for_mobility_sync first.",
            "pre_actions": pre_actions,
            "readiness": readiness or {"raw": readiness_raw},
        }

    fn = registry[operation]
    try:
        output = fn(**args)
    except TypeError as e:
        return {
            "success": False,
            "operation": operation,
            "error": f"Invalid arguments for '{operation}': {e}",
            "pre_actions": pre_actions,
            "args": args,
        }
    except Exception as e:
        return {
            "success": False,
            "operation": operation,
            "error": f"Unexpected failure during '{operation}': {e}",
            "pre_actions": pre_actions,
            "args": args,
        }

    success = not _operation_failed(output)
    parsed_output = _try_parse_json(output)
    if operation == "wait_for_mobility_sync" and isinstance(parsed_output, dict):
        success = bool(parsed_output.get("synced", False))
    if operation == "wait_for_sim_completion" and isinstance(parsed_output, dict):
        success = bool(parsed_output.get("completed", False))
    final_readiness, final_raw = _run_readiness_snapshot()
    return {
        "success": success,
        "operation": operation,
        "args": args,
        "pre_actions": pre_actions,
        "output": output,
        "readiness_after": final_readiness or {"raw": final_raw},
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True))
def get_workflow_contracts() -> str:
    """
    Returns machine-readable workflow contracts (preconditions and auto-fix behavior)
    for guarded operations. Agents should consult this to plan safe task order.
    """
    payload = {
        "use_for_natural_language_control": True,
        "preferred_executor": "autonomous_aodt_task",
        "supported_operations": sorted(_get_guarded_operation_registry().keys()),
        "precondition_summary": {
            "worker_required": ["generate_mobility", "start_simulation"],
            "stage_saved_required": ["generate_mobility"],
            "panel_required": ["start_simulation"],
            "ues_required": ["start_simulation"],
            "mobility_sync_required": ["start_simulation"],
        },
        "auto_fix_summary": {
            "creates_new_stage_when_safe": ["create_panel", "create_ue", "create_ru", "create_du", "create_tx_rx_pair"],
            "auto_create_panel_for_start_simulation": True,
            "auto_generate_and_wait_mobility_for_start_simulation": True,
            "auto_save_stage_when_needed_for_mobility": True,
            "cannot_auto_attach_worker": True,
            "cannot_auto_invent_ues_for_start_simulation": True,
        },
    }
    return json.dumps(payload, indent=2)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def execute_guarded_operation(operation: str, args_json: str = "{}", auto_fix: bool = True) -> str:
    """
    Executes a single operation with workflow safety checks and optional auto-fix.
    This is the recommended entrypoint for reliable AODT control from natural language.

    Args:
        operation: Operation name (see get_workflow_contracts()).
        args_json: JSON object string containing operation arguments.
        auto_fix:  If true, safe preconditions are auto-resolved before execution.
    """
    parsed_args = _try_parse_json(args_json)
    if parsed_args is None:
        parsed_args = {}
    if not isinstance(parsed_args, dict):
        return json.dumps(
            {
                "success": False,
                "operation": operation,
                "error": "args_json must be a JSON object string.",
            },
            indent=2,
        )

    result = _execute_guarded_operation_internal(operation=operation, args=parsed_args, auto_fix=auto_fix)
    return json.dumps(result, indent=2)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def execute_guarded_sequence(steps_json: str, auto_fix: bool = True, stop_on_error: bool = True) -> str:
    """
    Executes multiple operations in order, validating preconditions before each step.

    steps_json format:
      [
        {"operation": "create_tx_rx_pair", "args": {"tx_position": [0,0,0], "rx_position": [5,0,0], "position_units": "meters"}},
        {"operation": "start_simulation", "args": {}}
      ]
    """
    parsed = _try_parse_json(steps_json)
    if not isinstance(parsed, list):
        return json.dumps(
            {"success": False, "error": "steps_json must be a JSON array of step objects."},
            indent=2,
        )

    results = []
    overall_success = True
    for idx, step in enumerate(parsed, start=1):
        if not isinstance(step, dict):
            step_result = {
                "step_index": idx,
                "success": False,
                "error": "Each step must be an object with 'operation' and optional 'args'.",
            }
            results.append(step_result)
            overall_success = False
            if stop_on_error:
                break
            continue

        operation = str(step.get("operation", "")).strip()
        args = step.get("args", {})
        if args is None:
            args = {}
        if not isinstance(args, dict):
            step_result = {
                "step_index": idx,
                "operation": operation,
                "success": False,
                "error": "'args' must be an object.",
            }
            results.append(step_result)
            overall_success = False
            if stop_on_error:
                break
            continue

        step_result = _execute_guarded_operation_internal(operation=operation, args=args, auto_fix=auto_fix)
        step_result["step_index"] = idx
        results.append(step_result)
        if not step_result.get("success", False):
            overall_success = False
            if stop_on_error:
                break

    payload = {
        "success": overall_success,
        "auto_fix": auto_fix,
        "stop_on_error": stop_on_error,
        "steps_executed": len(results),
        "results": results,
    }
    return json.dumps(payload, indent=2)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def autonomous_aodt_task(task: str, auto_fix: bool = True) -> str:
    """
    High-level autonomous entrypoint for natural-language AODT requests.
    It infers a guarded operation/sequence, runs it with workflow checks,
    and returns structured execution + verification output.
    """
    t = (task or "").strip()
    n = t.lower()
    if not t:
        return json.dumps({"success": False, "error": "task must be a non-empty string."}, indent=2)

    def run_op(op: str, args: dict | None = None):
        return _execute_guarded_operation_internal(operation=op, args=args or {}, auto_fix=auto_fix)

    result = {"success": False, "task": t, "mode": "autonomous", "actions": []}

    # TX/RX creation intent (most common failure-prone workflow)
    if (("transmitter" in n or " tx" in n or "tx " in n) and ("receiver" in n or " rx" in n or "rx " in n)) or ("tx/rx" in n):
        coord_matches = re.findall(r"\[([^\]]+)\]", t)
        tx = [0.0, 0.0, 10.0]
        rx = [60.0, 0.0, 1.5]
        units = "meters"
        if "centimeter" in n or " cm" in n:
            units = "centimeters"
        if "stage unit" in n or "stage units" in n:
            units = "stage"
        try:
            if len(coord_matches) >= 2:
                tx_vals = [float(x.strip()) for x in coord_matches[0].split(",")]
                rx_vals = [float(x.strip()) for x in coord_matches[1].split(",")]
                if len(tx_vals) == 3 and len(rx_vals) == 3:
                    tx, rx = tx_vals, rx_vals
        except Exception:
            pass

        a1 = run_op(
            "create_tx_rx_pair",
            {"tx_position": tx, "rx_position": rx, "position_units": units, "enable_rays": True},
        )
        result["actions"].append({"operation": "create_tx_rx_pair", "result": a1})
        if not a1.get("success", False):
            result["success"] = False
            return json.dumps(result, indent=2)

        a2 = run_op("list_network_entities", {})
        result["actions"].append({"operation": "list_network_entities", "result": a2})
        a3 = run_op("validate_control_readiness", {"require_live_session": False, "require_saved_stage": False})
        result["actions"].append({"operation": "validate_control_readiness", "result": a3})
        result["success"] = True
        return json.dumps(result, indent=2)

    if ("list" in n and ("network" in n or "ru" in n or "ue" in n or "du" in n)) or ("entities" in n):
        a = run_op("list_network_entities", {})
        result["actions"].append({"operation": "list_network_entities", "result": a})
        result["success"] = bool(a.get("success", False))
        return json.dumps(result, indent=2)

    if "readiness" in n or "preflight" in n:
        a = run_op("validate_control_readiness", {"require_live_session": False, "require_saved_stage": False})
        result["actions"].append({"operation": "validate_control_readiness", "result": a})
        result["success"] = bool(a.get("success", False))
        return json.dumps(result, indent=2)

    if "mobility" in n and ("generate" in n or "sync" in n):
        a1 = run_op("generate_mobility", {})
        result["actions"].append({"operation": "generate_mobility", "result": a1})
        if a1.get("success", False):
            a2 = run_op("wait_for_mobility_sync", {"timeout_seconds": 180, "poll_interval_seconds": 1.0})
            result["actions"].append({"operation": "wait_for_mobility_sync", "result": a2})
            result["success"] = bool(a2.get("success", False))
        else:
            result["success"] = False
        return json.dumps(result, indent=2)

    if "start sim" in n or "start simulation" in n or "run simulation" in n:
        a1 = run_op("start_simulation", {})
        result["actions"].append({"operation": "start_simulation", "result": a1})
        if a1.get("success", False):
            a2 = run_op("wait_for_sim_completion", {"timeout_seconds": 240, "poll_interval_seconds": 1.0})
            result["actions"].append({"operation": "wait_for_sim_completion", "result": a2})
            result["success"] = bool(a2.get("success", False))
        else:
            result["success"] = False
        return json.dumps(result, indent=2)

    if "stop simulation" in n or "pause simulation" in n:
        a = run_op("stop_simulation", {})
        result["actions"].append({"operation": "stop_simulation", "result": a})
        result["success"] = bool(a.get("success", False))
        return json.dumps(result, indent=2)

    if "reset simulation" in n or "reset sim" in n:
        a = run_op("reset_simulation", {})
        result["actions"].append({"operation": "reset_simulation", "result": a})
        result["success"] = bool(a.get("success", False))
        return json.dumps(result, indent=2)

    if "log" in n:
        a = run_op("get_recent_aodt_logs", {"max_lines": 200})
        result["actions"].append({"operation": "get_recent_aodt_logs", "result": a})
        result["success"] = bool(a.get("success", False))
        return json.dumps(result, indent=2)

    # Fallback with explicit contract guidance so agent can continue autonomously.
    result["error"] = (
        "Could not infer a safe operation from task text. "
        "Use get_workflow_contracts and execute_guarded_operation/sequence."
    )
    result["supported_operations"] = sorted(_get_guarded_operation_registry().keys())
    return json.dumps(result, indent=2)


# ─── 14. Raw execution ────────────────────────────────────────────────────────

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
    return _run(code, truncate=100_000)


if __name__ == "__main__":
    mcp.run()
