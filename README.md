# NVIDIA Aerial Omniverse Digital Twin (AODT) MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io) server that gives Claude (or any MCP client) complete programmatic control over a running [NVIDIA AODT](https://developer.nvidia.com/aerial-omniverse-digital-twin) session. 

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
       │  TCP JSON (port 8765)
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

## Tools (51 total)

This server now includes a guarded workflow layer so agents can execute tasks in AODT-safe order without guessing preconditions.

### Guarded Workflow (Recommended for NL Agents)
| Tool | Description |
|---|---|
| `get_workflow_contracts` | Returns operation contracts, preconditions, and auto-fix behavior |
| `execute_guarded_operation(operation, args_json, auto_fix)` | Executes one operation with workflow checks and safe auto-fixes |
| `execute_guarded_sequence(steps_json, auto_fix, stop_on_error)` | Executes multi-step plans with validation at each step |

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
| `get_simulation_status` | Worker simulation + timeline state |
| `validate_control_readiness` | Preflight checklist for worker/live/panels/UE/mobility readiness |
| `start_simulation` | Starts worker-driven simulation with AODT validation checks |
| `stop_simulation` | Sends pause request to worker |
| `reset_simulation` | Sends stop request + timeline reset |
| `wait_for_sim_completion` | Waits for simulation completion or timeout |

### AODT Network Entities
| Tool | Description |
|---|---|
| `get_scenario_info` | All parameters on the `/Scenario` prim |
| `list_network_entities` | All RUs, DUs, and UEs with their positions |
| `create_panel` | Creates a panel under `/Panels` |
| `list_panels` | Lists panel prims and RF attributes |
| `set_default_panels` | Sets `/Scenario` default panel types |
| `create_ue(position, position_units)` | Deploy a User Equipment with scale-aware units |
| `create_ru(position, position_units)` | Deploy a Radio Unit with scale-aware units |
| `create_du(position, position_units)` | Deploy a Distributed Unit with scale-aware units |
| `create_tx_rx_pair(tx_position, rx_position, ...)` | One-call RU+UE creation for TX/RX setup |
| `generate_mobility` | Triggers mobility generation via worker pipeline |
| `wait_for_mobility_sync` | Waits until mobility sync with DB |
| `set_ray_pair_enabled` | Enables/disables ray visibility for RU-UE pair |
| `refresh_raypaths` | Refreshes ray visualization from telemetry |
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

### Runtime Context and Logs
| Tool | Description |
|---|---|
| `get_aodt_runtime_context` | Structured snapshot of stage/worker/session/simulation context |
| `get_recent_aodt_logs` | Tail Kit + `aodt.control` logs |
| `stream_aodt_logs` | Incremental log streaming with cursors (new lines only) |

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
   [AODT-MCP] Started socket server on 127.0.0.1:8765
   ```
   By default, the extension binds localhost for safety. You can override with:
   ```bash
   export AODT_MCP_HOST=0.0.0.0
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

For natural-language agents, prefer guarded execution:

```
Use execute_guarded_operation to create a TX/RX pair:
operation=create_tx_rx_pair
args={"tx_position":[0,0,10],"rx_position":[60,0,1.5],"position_units":"meters","enable_rays":true}

Then start simulation safely:
operation=start_simulation
auto_fix=true
```

## Troubleshooting

| Problem | Fix |
|---|---|
| **Connection refused** | Ensure the **AODT MCP Server** extension is enabled and the console shows `Started socket server on 127.0.0.1:8765` (or your `AODT_MCP_HOST`) |
| **Port conflict** | Change `PORT` in both `mcp_server.py` and `exts/aodt.mcp_server/aodt/mcp_server/__init__.py` |
| **Execution timeout** | Long-running code hits the 10 s timeout. Break it into smaller calls or use `execute_aodt_command` with async-safe code |
| **AODT entities not found** | Ensure an AODT scene is loaded (not a blank stage) before calling `create_ru`, `create_ue`, etc. |
| **Agent runs wrong step order** | Use `execute_guarded_operation` / `execute_guarded_sequence` so preconditions are auto-checked and safe fixes are applied |

## Evaluations

`evaluations.xml` contains 10 question-answer pairs for testing LLM effectiveness with this MCP server. Questions use the Kit test USD files bundled with AODT and cover stage loading, prim inspection, attribute reads, transforms, prim creation, and simulation state.

## License

MIT License
