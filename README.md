# NVIDIA Aerial Omniverse Digital Twin (AODT) MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io) server that gives Claude (or any MCP client) complete programmatic control over a running [NVIDIA AODT](https://developer.nvidia.com/aerial-omniverse-digital-twin) session. A complete novice can operate AODT entirely through natural language — no GUI required.

## What you can do

Prompt Claude naturally and it handles the rest:

> *"Load the Berlin scene, deploy an RU on top of the tallest building, place 10 UEs around it, and start the simulation."*

> *"Show me all the Radio Units in the scene and frame the camera on the first one."*

> *"What is the current DL throughput for UE at /UEs/ue_0003?"*

> *"Move the antenna at /RUs/ru_0001 to position (500, 200, 30) and take a screenshot."*

## Architecture

```
Claude / MCP Client
       │  stdio (MCP protocol)
       ▼
  mcp_server.py          ← FastMCP bridge (this repo)
       │  TCP JSON (port 9876)
       ▼
  aodt.mcp_server         ← Omniverse Kit extension (this repo)
  (runs inside AODT)
       │  Python exec on main UI thread
       ▼
  AODT / Omniverse Kit
```

Two components ship in this repo:

| Component | Path | Role |
|---|---|---|
| MCP Bridge | `mcp_server.py` | FastMCP server; translates tool calls into TCP commands |
| Kit Extension | `exts/aodt.mcp_server/` | Runs inside AODT; executes code on the main thread and returns stdout |

## Tools (35 total)

### Connectivity
| Tool | Description |
|---|---|
| `ping_aodt` | Check whether the AODT socket server is reachable |

### Stage Management
| Tool | Description |
|---|---|
| `new_stage` | Create a blank USD stage |
| `load_stage(path)` | Open a local or Nucleus USD file |
| `save_stage` | Save the current stage to disk |
| `get_stage_info` | File path, up-axis, meters-per-unit, prim count |

### Stage Traversal
| Tool | Description |
|---|---|
| `get_aodt_stage_hierarchy(max_depth)` | Print the full prim tree |
| `find_prims(type_filter, name_filter, parent_path, max_results)` | Search prims by type and/or name |

### Prim Inspection
| Tool | Description |
|---|---|
| `get_prim_info(prim_path)` | Type, active state, visibility, all attributes, children |
| `get_prim_attribute(prim_path, attribute_name)` | Read a specific attribute value |

### Prim Manipulation
| Tool | Description |
|---|---|
| `create_prim(prim_path, prim_type)` | Create any USD prim type (Cube, Sphere, Light, Camera, …) |
| `delete_prim(prim_path)` | Delete a prim and its children |
| `duplicate_prim(source_path, dest_path)` | Copy a prim subtree |
| `set_prim_visibility(prim_path, visible)` | Show or hide a prim |
| `set_prim_attribute(prim_path, attribute_name, value)` | Set any attribute with automatic type inference |

### Transforms
| Tool | Description |
|---|---|
| `get_prim_transform(prim_path)` | Local and world position, rotation, scale |
| `set_prim_transform(prim_path, position, rotation, scale)` | Update any transform component |

### Viewport
| Tool | Description |
|---|---|
| `select_and_focus_prims(prim_paths)` | Select prims and move the camera to frame them |
| `take_screenshot(output_path)` | Capture the viewport to a PNG file |

### Simulation Control
| Tool | Description |
|---|---|
| `get_simulation_status` | Current state (playing / paused / stopped) and timeline position |
| `start_simulation` | Play the simulation |
| `stop_simulation` | Pause the simulation |
| `reset_simulation` | Stop and reset to t=0 |

### AODT Network Entities
| Tool | Description |
|---|---|
| `get_scenario_info` | All parameters on the `/Scenario` prim |
| `list_network_entities` | All RUs, DUs, and UEs with their positions |
| `create_ue(position)` | Deploy a User Equipment (mobile device) |
| `create_ru(position)` | Deploy a Radio Unit (base station antenna) |
| `create_du(position)` | Deploy a Distributed Unit (baseband processor) |
| `get_ue_performance(prim_path)` | Live DL/UL throughput telemetry for a UE |

### AODT Configuration
| Tool | Description |
|---|---|
| `get_aodt_setting(setting_path)` | Read a Carbonite setting (DB host, session name, asset paths, …) |
| `set_aodt_setting(setting_path, value)` | Write a Carbonite setting |

### History
| Tool | Description |
|---|---|
| `undo` | Undo the last stage modification |
| `redo` | Redo the last undone modification |

### Asset Discovery
| Tool | Description |
|---|---|
| `search_aodt_assets(query)` | Search local paths and Nucleus for USD assets by keyword |
| `list_loadable_scenes` | List all USD scenes in standard install and Nucleus paths |

### Raw Execution
| Tool | Description |
|---|---|
| `execute_aodt_command(code)` | Run arbitrary Python inside AODT (`omni.*`, `pxr.*`, `aodt.*`) |

## Installation

### 1. Enable the AODT Extension

1. Locate `apps/aodt.kit` in your AODT installation directory and ensure these extensions are present in the dependencies section:
   ```toml
   "omni.kit.window.extensions" = {}
   "omni.kit.window.script_editor" = {}
   ```
2. Launch AODT.
3. Open **Window > Extensions**.
4. Click the **Gear icon** (Settings) and add the `exts/` folder of this repository to the **Extension Search Paths**.
5. Search for **AODT MCP Server** and toggle it **Enabled**. The console should print:
   ```
   [AODT-MCP] Started socket server on 0.0.0.0:9876
   ```

### 2. Configure Claude Desktop

Add the following to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "aodt": {
      "command": "uv",
      "args": [
        "run",
        "--with", "mcp",
        "/absolute/path/to/aodt-mcp/mcp_server.py"
      ]
    }
  }
}
```

Or with a local virtualenv:

```json
{
  "mcpServers": {
    "aodt": {
      "command": "/absolute/path/to/aodt-mcp/aodt_env/bin/python",
      "args": ["/absolute/path/to/aodt-mcp/mcp_server.py"]
    }
  }
}
```

### 3. Verify

Once connected, ask Claude:

> *"Are you connected to AODT?"*

Claude will call `ping_aodt` and confirm the connection.

## Example prompts

```
Load /home/user/scenes/city.usd and show me the top-level hierarchy.

How many Radio Units are currently deployed?

Create a new RU at position (100, 200, 30) and move the camera to look at it.

Set the number of procedural UEs in the scenario to 50.

Start the simulation, wait, then check the throughput on /UEs/ue_0001.

Take a screenshot and save it to /tmp/scene.png.

Undo the last change.
```

## Troubleshooting

| Problem | Fix |
|---|---|
| **Connection refused** | Ensure the **AODT MCP Server** extension is enabled and the console shows `Started socket server on 0.0.0.0:9876` |
| **Port conflict** | Change `PORT` in both `mcp_server.py` and `exts/aodt.mcp_server/aodt/mcp_server/__init__.py` |
| **Execution timeout** | Long-running code hits the 10 s timeout. Break it into smaller calls or use `execute_aodt_command` with async-safe code |
| **AODT entities not found** | Ensure an AODT scene is loaded (not a blank stage) before calling `create_ru`, `create_ue`, etc. |

## Evaluations

`evaluations.xml` contains 10 question-answer pairs for testing LLM effectiveness with this MCP server. Questions use the Kit test USD files bundled with AODT and cover stage loading, prim inspection, attribute reads, transforms, prim creation, and simulation state.

## License

MIT License
