# NVIDIA Aerial Omniverse Digital Twin (AODT) MCP Server

A Model Context Protocol (MCP) server integration for [NVIDIA AODT](https://developer.nvidia.com/aerial-omniverse-digital-twin). This framework enables AI assistants to interface directly with a running AODT session to inspect the stage hierarchy, search for assets, and execute Python-based commands.

## Features

- **Stage Inspection**: Retrieve a structured text-based tree of the current USD stage hierarchy.
- **Asset Discovery**: Search for `.usd` assets across local directories and Omniverse Nucleus server paths.
- **Command Execution**: Execute arbitrary `omni.kit` or USD Python commands within the active AODT application.
- **Thread Safety**: Commands are dispatched to the Omniverse main application thread to ensure stage stability and UI responsiveness.

## Architecture

The system consists of two primary components:
1. **AODT Extension (`aodt.mcp_server`)**: A native Omniverse Kit extension that runs within AODT. It initializes a TCP socket server (default port 9876) to handle incoming requests.
2. **MCP Server (`mcp_server.py`)**: A FastMCP-based server that acts as a bridge between the AI assistant and the AODT extension.

## Installation

### 1. Enable the AODT Extension

AODT requires a configuration change to expose the Extension Manager.

1. Locate the AODT configuration file at `apps/aodt.kit` within your installation directory.
2. Ensure the following extensions are enabled in the dependencies section:
   ```toml
   "omni.kit.window.extensions" = {}
   "omni.kit.window.script_editor" = {}
   ```
3. Launch AODT.
4. Open **Window > Extensions**.
5. Click the **Gear Icon** (Settings) and add the `exts` folder of this repository to the **Extension Search Paths**.
6. Search for `AODT MCP Server` and toggle it to **Enabled**.

### 2. Configure the MCP Client (e.g., Claude Desktop)

Add the following to your `claude_desktop_config.json`:

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

## Usage

Once the MCP server is connected, the following tools become available:

- `get_aodt_stage_hierarchy(max_depth)`: Scans the current USD stage and returns the prim tree.
- `search_aodt_assets(query)`: Searches Nucleus (`omniverse://`) and local paths for matching assets.
- `execute_aodt_command(code)`: Runs a Python string directly in the AODT environment.

### Tool Examples:
- "List the current stage hierarchy to depth 4."
- "Search for 'tokyo' scenes in the Nucleus server."
- "Create a red cube at world coordinates (0, 0, 0)."

## Troubleshooting

- **Connection Refused**: Verify that the **AODT MCP Server Extension** is enabled in AODT and that the console logs indicate the socket server has started.
- **Port Conflict**: If port 9876 is unavailable, update the `PORT` constant in both `mcp_server.py` and `exts/aodt.mcp_server/aodt/mcp_server/__init__.py`.

## License

MIT License
