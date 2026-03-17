import socket
import json
import traceback
from typing import Any

# Use standard mcp python library to create FastMCP server
from mcp.server.fastmcp import FastMCP

# Define standard connection settings matching our plan
AODT_HOST = "localhost"
AODT_PORT = 9876

# Create MCP server
mcp = FastMCP("Nvidia AODT MCP Server")

def send_to_aodt(command_type: str, params: dict = None) -> dict:
    """Helper method to send JSON payloads to the AODT TCP server."""
    payload = {
        "type": command_type,
        "params": params or {}
    }
    
    try:
        # Create a new connection for each request (simplest, most reliable)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(10.0) # 10 second timeout for AODT to respond
            s.connect((AODT_HOST, AODT_PORT))
            
            # Send payload as json string followed by newline
            s.sendall((json.dumps(payload) + "\n").encode('utf-8'))
            
            # Receive response (read until newline or connection close)
            response_data = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                response_data += chunk
                if b"\n" in chunk:
                    break
                    
            if not response_data:
                return {"status": "error", "message": "Empty response from AODT"}
                
            return json.loads(response_data.decode('utf-8').strip())
            
    except ConnectionRefusedError:
        return {
            "status": "error", 
            "message": f"Connection refused. Ensure AODT is running and the aodt_socket_server.py script has been executed to listen on port {AODT_PORT}."
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Communication error: {str(e)}"
        }

@mcp.tool()
def execute_aodt_command(code: str) -> str:
    """
    Executes raw Python code inside the Nvidia Aerial Omniverse Digital Twin (AODT) environment.
    Use this to invoke Omniverse Kit commands (`omni.*`), manipulate the USD stage, 
    or run any standard python logic inside AODT.
    
    Args:
        code: The raw python string to execute. Example: "import omni.kit.commands\nomni.kit.commands.execute('CreateMeshPrimWithDefaultXform', prim_type='Cube')"
    """
    response = send_to_aodt("execute", {"code": code})
    
    if response.get("status") == "success":
        output = response.get("result", "")
        # If output is empty but it succeeded, provide a generic success feedback
        return output if output else "Execution successful (no output)."
    else:
        error_msg = response.get("message", "Unknown error")
        return f"Execution Failed:\n{error_msg}"

@mcp.tool()
def get_aodt_stage_hierarchy(max_depth: int = 3) -> str:
    """
    Returns a text-based tree representing the current USD stage hierarchy in AODT.
    
    Args:
        max_depth: Maximum depth to traverse (default is 3). Increase this if you need to see deeper nesting.
    """
    code = f"""
import omni.usd
stage = omni.usd.get_context().get_stage()

def traverse(prim, current_depth, max_depth):
    if current_depth > max_depth:
        return []
    
    # Format current prim
    indent = "  " * current_depth
    line = indent + str(prim.GetPath()) + " [" + prim.GetTypeName() + "]"
    lines = [line]
    
    children = prim.GetChildren()
    if children:
        if current_depth == max_depth:
            lines.append("  " * (current_depth + 1) + "... (children truncated, increase max_depth to see more)")
        else:
            for child in children:
                lines.extend(traverse(child, current_depth + 1, max_depth))
    return lines

if stage:
    # Typical AODT stages can have millions of prims (grass, particles, etc)
    # We traverse from pseudo-root
    hierarchy = traverse(stage.GetPseudoRoot(), 0, {max_depth})
    print("\\n".join(hierarchy))
    
    # Also print a summary count
    total_prims = len(list(stage.Traverse()))
    print(f"\\n--- Summary: Total Prims in Stage: {{total_prims}} ---")
else:
    print("No active stage found.")
"""
    response = send_to_aodt("execute", {"code": code})
    if response.get("status") == "success":
        result = response.get("result", "No hierarchy found.")
        # Final safety truncation for the MCP response string itself
        if len(result) > 50000:
            result = result[:50000] + "\n... (Response truncated by MCP server for length) ..."
        return result
    return f"Failed to get hierarchy: {response.get('message')}"

@mcp.tool()
def search_aodt_assets(query: str) -> str:
    """
    Searches for USD assets within local paths and Omniverse Nucleus server.
    
    Args:
        query: Search term for the asset (e.g., 'tokyo', 'berlin').
    """
    code = f"""
import os
import omni.client

search_paths = [
    "/home/sal-garfield/.local/share/ov/pkg/aodt-1.4.1/assets",
    "/home/sal-garfield/aodt_1.4.1/assets",
    "omniverse://omniverse-server/Users/aerial",
    "omniverse://omniverse-server/Projects",
    "omniverse://omniverse-server/NVIDIA/Assets/DigitalTwin"
]

found_files = []

# 1. Search Local Files
for path in search_paths:
    if path.startswith("/") and os.path.exists(path):
        for root, dirs, files in os.walk(path):
            for file in files:
                if "{query}".lower() in file.lower() and file.endswith(('.usd', '.usda', '.usdc')):
                    found_files.append(os.path.join(root, file))

# 2. Search Nucleus Files (Recursive list)
def search_nucleus(base_path, query, results, depth=0, max_depth=3):
    if depth > max_depth:
        return
    res, entries = omni.client.list(base_path)
    if res == omni.client.Result.OK:
        for entry in entries:
            name = entry.relative_path
            full_path = base_path + "/" + name
            if "{query}".lower() in name.lower() and name.endswith(('.usd', '.usda', '.usdc')):
                results.append(full_path)
            if entry.flags & omni.client.ItemFlags.CAN_HAVE_CHILDREN:
                search_nucleus(full_path, query, results, depth + 1, max_depth)

for path in search_paths:
    if path.startswith("omniverse://"):
        search_nucleus(path, "{query}", found_files)

if found_files:
    print(f"Found {{len(found_files)}} assets matching '{query}':")
    for f in found_files[:15]:
        print(f"- {{f}}")
else:
    print(f"No assets matching '{query}' found in local or Nucleus paths.")
"""
    response = send_to_aodt("execute", {"code": code})
    if response.get("status") == "success":
        return response.get("result", "Search failed.")
    return f"Asset search error: {response.get('message')}"

@mcp.resource("aodt://status")
def get_aodt_status() -> str:
    """Check if AODT is reachable and get its current status."""
    response = send_to_aodt("ping")
    if response.get("status") == "success":
        return json.dumps({"connected": True, "message": "AODT socket server is active."})
    return json.dumps({"connected": False, "error": response.get("message")})

if __name__ == "__main__":
    # Start the FastMCP server, communicating over stdio (standard for MCP clients like Claude)
    mcp.run()
