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
