import socket
import json
import threading
import traceback
import sys
import io
import os

import omni.ext
import omni.kit.app

HOST = os.getenv("AODT_MCP_HOST", "127.0.0.1")
PORT = 8765

class AODTSocketServer:
    def __init__(self):
        self.server_socket = None
        self.running = False
        self.thread = None

    def start(self):
        if self.running:
            print(f"[AODT-MCP] Server is already running on port {PORT}")
            return

        self.running = True
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        try:
            self.server_socket.bind((HOST, PORT))
            self.server_socket.listen(5)
            print(f"[AODT-MCP] Started socket server on {HOST}:{PORT}")
            
            # Start a background thread so we don't block the AODT UI thread
            self.thread = threading.Thread(target=self._listen_loop, daemon=True)
            self.thread.start()
        except Exception as e:
            print(f"[AODT-MCP] Failed to start server: {e}")
            self.running = False

    def stop(self):
        self.running = False
        if self.server_socket:
            try:
                self.server_socket.shutdown(socket.SHUT_RDWR)
                self.server_socket.close()
            except Exception:
                pass
        self.server_socket = None
        
        if self.thread and self.thread.is_alive():
            # In a real scenario you might wait for the thread, 
            # but daemon thread will eventually die anyway.
            pass
            
        print("[AODT-MCP] Server stopped.")

    def _listen_loop(self):
        while self.running:
            try:
                # Accept incoming connection
                self.server_socket.settimeout(1.0) # Check self.running continuously
                try:
                    client_socket, addr = self.server_socket.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break # Socket closed
                    
                print(f"[AODT-MCP] Connection from {addr}")
                
                # Handle client in a new thread
                client_thread = threading.Thread(target=self._handle_client, args=(client_socket,), daemon=True)
                client_thread.start()
            except Exception as e:
                if self.running:
                    print(f"[AODT-MCP] Accept error: {e}")

    def _handle_client(self, client_socket):
        try:
            # Read data until newline
            data = b""
            while True:
                chunk = client_socket.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"\n" in chunk:
                    break
            
            if not data:
                return

            request = json.loads(data.decode('utf-8').strip())
            command_type = request.get("type", "")
            params = request.get("params", {})
            
            response = {"status": "error", "message": f"Unknown command type: {command_type}"}
            
            if command_type == "ping":
                response = {"status": "success", "result": "pong"}
            elif command_type == "execute":
                code = params.get("code", "")
                response = self._execute_code_in_main_thread(code)

            # Send response
            client_socket.sendall((json.dumps(response) + "\n").encode('utf-8'))
            
        except json.JSONDecodeError:
            error_response = {"status": "error", "message": "Invalid JSON"}
            client_socket.sendall((json.dumps(error_response) + "\n").encode('utf-8'))
        except Exception as e:
            error_response = {"status": "error", "message": traceback.format_exc()}
            client_socket.sendall((json.dumps(error_response) + "\n").encode('utf-8'))
        finally:
            client_socket.close()

    def _execute_code_in_main_thread(self, code):
        """
        Omniverse API calls must happen on the main UI thread.
        We will use an Event to wait for the execution to finish and return the result.
        """
        result_container = {"status": "pending", "output": "", "error": None}
        done_event = threading.Event()

        def execution_task():
            # Capture stdout/stderr
            old_stdout = sys.stdout
            old_stderr = sys.stderr
            redirected_output = io.StringIO()
            sys.stdout = redirected_output
            sys.stderr = redirected_output
            
            try:
                # Execute the code in the global scope
                exec(code, globals())
                result_container["status"] = "success"
            except BaseException:
                result_container["status"] = "error"
                result_container["error"] = traceback.format_exc()
            finally:
                # Restore stdout/stderr
                sys.stdout = old_stdout
                sys.stderr = old_stderr
                result_container["output"] = redirected_output.getvalue()
                done_event.set()

        sub = [None]
        def on_update(e):
            sub[0] = None # Hold reference and then clear it
            execution_task()

        # Subscribe to the next frame update to run the task on the main thread
        sub[0] = omni.kit.app.get_app().get_update_event_stream().create_subscription_to_pop(on_update)
        
        # Wait for the task to finish (timeout after 120 seconds — allows Nucleus asset loading)
        finished = done_event.wait(120.0)
        
        if not finished:
            return {"status": "error", "message": "Execution timed out on the main thread."}
            
        MAX_OUTPUT = 100_000  # chars; prevents unbounded socket transfers
        if result_container["status"] == "error":
            msg = result_container["error"] + "\nOutput:\n" + result_container["output"]
            if len(msg) > MAX_OUTPUT:
                msg = msg[:MAX_OUTPUT] + "\n... (output truncated at extension level)"
            return {"status": "error", "message": msg}

        result = result_container["output"]
        if len(result) > MAX_OUTPUT:
            result = result[:MAX_OUTPUT] + "\n... (output truncated at extension level)"
        return {"status": "success", "result": result}

# Global instance
_mcp_server = None

class AODTMCPServerExtension(omni.ext.IExt):
    def on_startup(self, ext_id):
        global _mcp_server
        print(f"[AODT-MCP] Starting Extension: {ext_id}")
        _mcp_server = AODTSocketServer()
        _mcp_server.start()

    def on_shutdown(self):
        global _mcp_server
        print("[AODT-MCP] Shutting down Extension")
        if _mcp_server:
            _mcp_server.stop()
            _mcp_server = None
