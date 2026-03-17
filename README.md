# Nvidia Aerial Omniverse Digital Twin (AODT) MCP Server

An open-source Model Context Protocol (MCP) framework for [Nvidia AODT](https://developer.nvidia.com/aerial-omniverse-digital-twin) that allows AI assistants like Claude to execute natural language instructions as Python code directly inside the AODT environment.

This project is heavily inspired by [blender-mcp](https://github.com/ahujasid/blender-mcp), bringing the same "vibe coding" capabilities to Nvidia's Digital Twin ecosystem.

## Features
- Control AODT directly from Claude using natural language.
- **Stage Context**: Instantly list all USD objects (prims) and their hierarchy.
- **Asset Search**: Automatically find .usd files (scenarios/scenes) in your local folders.
- **Full Execution**: Run any `omni.kit` or USD Python command inside the active AODT session.
- **Async Threading**: Code is executed safely on the Omniverse main thread without freezing the UI.

## Architecture
The system consists of two parts:
1. `aodt.mcp_server`: A custom Omniverse Extension running **inside** AODT that listens for incoming code execution requests on a background TCP socket (Port 9876).
2. `mcp_server.py`: A standard FastMCP server that Claude connects to. It provides the AI with tools to send Python code to the AODT socket.

## Installation & Usage

### 1. Install the AODT Extension
By default, AODT does not show the "Extensions" manager or "Script Editor" in its interface. We have enabled them by modifying the core AODT configuration file (`apps/aodt.kit`).

1. **Restart AODT** on your machine if you had it open.
2. In the top menu bar, you will now see **Window > Extensions** and **Window > Script Editor**.
3. Go to **Window > Extensions**.
4. In the Extension Manager that opens, click the **Gear Icon** (Settings) at the top right.
5. Add a new **Extension Search Path** pointing to the `exts` folder in this repository. 
   - Example: `/home/sal-garfield/aodt-mcp/exts`
6. Close the settings panel. In the search bar at the top left of the Extension Manager, type `AODT MCP`.
7. Enable the **AODT MCP Server Extension** using the toggle switch.
8. You should see a message in the AODT Console (or terminal): `[AODT-MCP] Started socket server on 0.0.0.0:9876`.

*Note: You can turn on the **Autoload** toggle next to the extension if you want it to run every time you open AODT.*

### 2. Configure Claude Desktop
You need to tell Claude how to start the MCP server.

1. Install `uv` if you haven't already:
   - Linux/Mac: `curl -LsSf https://astral.sh/uv/install.sh | sh`
   - Windows: `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`
2. Open Claude Desktop settings:
   - Go to **Claude > Settings > Developer > Edit Config** (`claude_desktop_config.json`).
3. Add the AODT server to your configuration:
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
*(Replace `/absolute/path/to/aodt-mcp/` with the actual path where you cloned this repo).*

4. Restart Claude Desktop.

### 3. Usage Examples
Once connected, you can ask Claude to do things in AODT!

**Vibe Coding Prompts:**
- *"Search for a Berlin scene in my assets."*
- *"List the current stage hierarchy to see what's loaded."*
- *"Create a red cube at the origin."*
- *"Move all antennas in the scene up by 5 meters."*

## Troubleshooting
- **Connection Refused**: Ensure the **AODT MCP Server Extension** is enabled in the Extension Manager. Check the console for `[AODT-MCP] Started socket server`.
- **Port in Use**: If port 9876 is taken (e.g., by Blender MCP), close the other application or change the `PORT` variable in `exts/aodt.mcp_server/aodt/mcp_server/__init__.py` and `mcp_server.py`.

## License
MIT License. Feel free to contribute, open issues, or submit PRs!
