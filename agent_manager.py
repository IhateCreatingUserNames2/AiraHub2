import asyncio
import json
import logging
import os
import signal
import sys
import time
import traceback
from contextlib import asynccontextmanager
from typing import List, Dict, Any, Optional, Tuple, Union

import httpx
from fastapi import FastAPI, Request, HTTPException, Body
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError

# --- Configuration & Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s:%(lineno)d - %(levelname)s - %(message)s')
logger = logging.getLogger("AgentManager")

# --- Environment Variables or Defaults ---
AIRA_HUB_REGISTER_URL = os.getenv("AIRA_HUB_URL", "http://localhost:8017") + "/register"
AIRA_HUB_URL = os.getenv("AIRA_HUB_URL", "http://localhost:8017")
AGENT_MANAGER_HOST = os.getenv("AGENT_MANAGER_HOST", "localhost") # Host where this manager runs
AGENT_MANAGER_PORT = int(os.getenv("AGENT_MANAGER_PORT", "9010")) # Port this manager listens on
CONFIG_FILE = "mcp_servers.json"
MCP_TIMEOUT = 20  # Seconds for MCP calls (initialize, tools/list) to subprocess
HUB_TIMEOUT = 10   # Seconds for registering with AIRA Hub
STDIO_READ_TIMEOUT = 15 # Slightly longer timeout for stdio responses

# --- Pydantic Models --- (Copied from registrar for consistency)
class ConnectionConfig(BaseModel):
    type: str
    command: Optional[List[str]] = None
    endpoint_url: Optional[str] = None
    env: Optional[Dict[str, str]] = None

class AiraHubRegistrationConfig(BaseModel):
    base_url: str
    description: Optional[str] = None
    tags: List[str] = []
    category: Optional[str] = "Uncategorized"
    provider_name: Optional[str] = "Unknown"

class ServerConfig(BaseModel):
    id: str
    name: str
    enabled: bool = True
    connection: ConnectionConfig
    aira_hub_registration: AiraHubRegistrationConfig

# --- MCP Models --- (Simplified for communication)
class MCPRequest(BaseModel):
    jsonrpc: str = "2.0"
    method: str
    params: Optional[Any] = None
    id: Optional[Union[str, int]] = None

class MCPResponse(BaseModel):
    jsonrpc: str = "2.0"
    id: Optional[Union[str, int]] = None
    result: Optional[Any] = None
    error: Optional[Dict[str, Any]] = None

class MCPError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"MCP Error {code}: {message}")

# --- Global State for Managed Processes ---
# WARNING: Global state is simple for this example, but consider dependency injection
# or a dedicated class for managing state in a larger application.
managed_processes: Dict[str, asyncio.subprocess.Process] = {}
server_configs: Dict[str, ServerConfig] = {} # Store config by ID
# Use asyncio Locks for safe concurrent access to stdio processes
process_locks: Dict[str, asyncio.Lock] = {}

# --- Stdio MCP Communication Logic ---
async def stdio_mcp_request(process: asyncio.subprocess.Process, request: MCPRequest) -> Dict[str, Any]:
    """Sends a single MCP request via stdio and returns the full JSON-RPC response dict."""
    server_id = next((sid for sid, proc in managed_processes.items() if proc == process), "unknown")
    lock = process_locks.get(server_id)
    if not lock:
         raise MCPError(-32000, f"Lock not found for server {server_id}")

    if process.stdin is None or process.stdout is None:
        raise MCPError(-32000, "Stdio process stdin/stdout not available")

    # Acquire lock to ensure only one request interacts with this stdio process at a time
    async with lock:
        request_str = request.model_dump_json() + '\n'
        logger.debug(f"STDIO Request to {server_id} (PID {process.pid}): {request_str.strip()}")

        try:
            process.stdin.write(request_str.encode('utf-8'))
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, AttributeError) as e: # Added AttributeError for safety
            logger.error(f"Stdio pipe broken for {server_id} before writing: {type(e).__name__}")
            # Attempt to mark process as dead? Or handle upstream.
            raise MCPError(-32000, f"Stdio pipe broken before writing: {type(e).__name__}") from e

        # Read response line by line with timeout
        response_str = ""
        try:
            while True:
                 line_bytes = await asyncio.wait_for(process.stdout.readline(), timeout=STDIO_READ_TIMEOUT)
                 if not line_bytes:
                      logger.error(f"Stdio process {server_id} closed connection while reading response")
                      # Mark process as dead?
                      raise MCPError(-32000, "Stdio process closed connection while reading response")
                 line_str = line_bytes.decode('utf-8').strip()
                 if line_str:
                      response_str = line_str
                      break
                 logger.debug(f"Received empty line from stdio {server_id}, continuing read...")
        except asyncio.TimeoutError:
            logger.error(f"Stdio response from {server_id} timed out after {STDIO_READ_TIMEOUT}s")
            raise MCPError(-32000, f"Stdio response timed out after {STDIO_READ_TIMEOUT}s") from None
        except (BrokenPipeError, ConnectionResetError, AttributeError) as e:
            logger.error(f"Stdio pipe broken for {server_id} while reading: {type(e).__name__}")
            raise MCPError(-32000, f"Stdio pipe broken while reading: {type(e).__name__}") from e

        logger.debug(f"STDIO Response from {server_id}: {response_str}")

        try:
            mcp_response_dict = json.loads(response_str)
            # Basic validation
            if not isinstance(mcp_response_dict, dict) or "jsonrpc" not in mcp_response_dict:
                 raise ValueError("Response is not a valid JSON-RPC object")
            # Return the full dict, let caller check for error/result
            return mcp_response_dict
        except json.JSONDecodeError as e:
            logger.error(f"JSON Decode Error parsing stdio response from {server_id}: {response_str}")
            raise MCPError(-32000, "JSON Decode Error parsing stdio response", response_str) from e
        except Exception as e:
            logger.error(f"Unexpected error processing stdio response from {server_id}: {type(e).__name__} - {e}")
            raise MCPError(-32000, f"Unexpected client error processing stdio response: {type(e).__name__}", str(e)) from e

# --- Process Management ---
async def manage_stdio_server(config: ServerConfig) -> Optional[asyncio.subprocess.Process]:
    """Starts and manages a stdio MCP server process. Returns process or None."""
    if not config.connection.command:
        logger.error(f"Cannot start stdio server {config.name}: 'command' not specified.")
        return None

    cmd_str = " ".join(config.connection.command)
    logger.info(f"Starting stdio server '{config.name}' (ID: {config.id}): {cmd_str}")

    try:
        process = await asyncio.create_subprocess_exec(
            *config.connection.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **(config.connection.env or {})}
        )
        logger.info(f"Stdio process for '{config.name}' started (PID: {process.pid})")

        # Task to log stderr
        async def log_stderr(proc: asyncio.subprocess.Process, name: str):
            if proc.stderr:
                while True:
                    try:
                         line = await proc.stderr.readline()
                         if not line: break
                         logger.warning(f"[{name} stderr - PID {proc.pid}]: {line.decode(errors='ignore').strip()}")
                    except Exception as e:
                         logger.error(f"Error reading stderr for {name}: {e}")
                         break
            logger.info(f"Stderr logging finished for {name} (PID {proc.pid})")
        asyncio.create_task(log_stderr(process, config.name))

        # Task to monitor if process exits unexpectedly
        async def monitor_process(proc: asyncio.subprocess.Process, server_id: str):
             await proc.wait()
             logger.warning(f"Stdio process for '{config.name}' (ID: {server_id}, PID: {proc.pid}) exited unexpectedly with code {proc.returncode}.")
             # Remove from managed processes - simple cleanup
             if server_id in managed_processes:
                  del managed_processes[server_id]
             if server_id in process_locks:
                  del process_locks[server_id]
        asyncio.create_task(monitor_process(process, config.id))


        # Brief pause for initialization (simplistic readiness check)
        await asyncio.sleep(2) # Increased slightly

        if process.returncode is not None:
             logger.error(f"Stdio process for '{config.name}' exited immediately after start (code {process.returncode}).")
             return None

        return process

    except FileNotFoundError:
         logger.error(f"Cannot start stdio server {config.name}: Command not found: {config.connection.command[0]}")
         return None
    except Exception as e:
        logger.error(f"Failed to start stdio server {config.name}: {e}")
        logger.error(traceback.format_exc())
        return None

# --- AIRA Hub Registration Logic ---
async def register_with_aira_hub(
    server_config: ServerConfig,
    server_info: Dict,
    tools: List[Dict],
    http_client: httpx.AsyncClient
):
    """Formats payload and registers/updates agent with AIRA Hub."""
    hub_reg_config = server_config.aira_hub_registration
    connection_type = server_config.connection.type
    formatted_tools = []
    for tool in tools:
        if isinstance(tool, dict) and "name" in tool and "inputSchema" in tool:
            formatted_tools.append({
                "name": tool["name"],
                "description": tool.get("description"),
                "inputSchema": tool["inputSchema"],
                "annotations": tool.get("annotations")
            })
        else:
            logger.warning(f"Skipping malformed tool from {server_config.name}: {tool}")

    registration_payload = {
        "url": hub_reg_config.base_url, # e.g., local://...
        "name": server_info.get("name", server_config.name),
        "description": hub_reg_config.description or server_info.get("description", f"MCP Server: {server_config.name}"),
        "version": server_info.get("version", "1.0.0"),
        "mcp_tools": formatted_tools,
        "aira_capabilities": ["mcp"],
        "tags": hub_reg_config.tags + ["managed"], # Add 'managed' tag
        "category": hub_reg_config.category,
        "provider": {"name": hub_reg_config.provider_name or server_info.get("provider", "AgentManager")},
        "mcp_url": None,
        "mcp_sse_url": None,
        "mcp_stream_url": None,
        "stdio_command": None
    }

    logger.info(
        f"Attempting POST to AIRA Hub for agent: {registration_payload.get('name')} with URL: {registration_payload.get('url')}")
    logger.debug(
        f"Full registration payload for {registration_payload.get('name')}: {json.dumps(registration_payload)}")

    # Set the *Hub's* callable endpoint URL based on the type *we* are proxying
    if connection_type == "stdio":
         # Point the Hub to *this manager's* proxy endpoint
         proxy_url = f"http://{AGENT_MANAGER_HOST}:{AGENT_MANAGER_PORT}/proxy/{server_config.id}"
         registration_payload["mcp_stream_url"] = proxy_url # Hub uses this for streaming POSTs
         registration_payload["mcp_url"] = proxy_url # Hub might also use this for simple POSTs
         # Keep stdio_command for informational purposes if needed by clients directly
         registration_payload["stdio_command"] = server_config.connection.command
         logger.info(f"Registering stdio server {server_config.name} with proxy URL: {proxy_url}")
    elif connection_type == "http":
        # Register the direct HTTP endpoint of the remote server
        registration_payload["mcp_url"] = server_config.connection.endpoint_url
        registration_payload["mcp_stream_url"] = server_config.connection.endpoint_url
        logger.info(f"Registering HTTP server {server_config.name} with direct URL: {registration_payload['mcp_url']}")
    elif connection_type == "sse":
         # Register the direct SSE endpoint
         registration_payload["mcp_sse_url"] = server_config.connection.endpoint_url
         registration_payload["mcp_stream_url"] = server_config.connection.endpoint_url
         logger.info(f"Registering SSE server {server_config.name} with direct URL: {registration_payload['mcp_sse_url']}")


    # Send registration to AIRA Hub
    try:
        logger.info(f"Registering/Updating '{registration_payload['name']}' with AIRA Hub...")
        reg_headers = {"Content-Type": "application/json"}
        response = await http_client.post(AIRA_HUB_REGISTER_URL, json=registration_payload, headers=reg_headers, timeout=HUB_TIMEOUT)
        response.raise_for_status()
        hub_response = response.json()
        logger.info(f"Successfully registered/updated '{registration_payload['name']}' (Hub Agent ID: {hub_response.get('agent_id')}) with AIRA Hub.")
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP Error registering '{registration_payload['name']}' with AIRA Hub: {e.response.status_code} - {e.response.text[:200]}")
    except Exception as e:
        logger.error(f"Error registering '{registration_payload['name']}' with AIRA Hub: {type(e).__name__} - {e}")

# --- Lifespan Function for Setup/Teardown ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Agent Manager starting...")
    # Load config
    try:
        with open(CONFIG_FILE, 'r') as f:
            raw_config_list = json.load(f)
        loaded_configs = [ServerConfig(**item) for item in raw_config_list]
        enabled_configs = [c for c in loaded_configs if c.enabled]
        for cfg in enabled_configs:  # Store by ID for lookup
            server_configs[cfg.id] = cfg
        logger.info(f"Loaded {len(enabled_configs)} enabled configurations.")
    except Exception as e:
        logger.critical(f"Failed to load or parse {CONFIG_FILE}: {e}", exc_info=True)
        raise RuntimeError(f"Configuration error in {CONFIG_FILE}") from e

    # Start stdio servers and register all enabled servers
    async with httpx.AsyncClient() as http_client:
        registration_tasks = []
        processed_ids_for_registration = set()

        for config in enabled_configs:
            logger.info(f"Processing config loop for ID: {config.id}")
            process = None
            server_info = {"name": config.name, "version": "unknown"}
            tools: List[Dict[str, Any]] = []  # Ensure tools is always initialized as a list
            should_register_this_agent = False

            if config.connection.type == "stdio":
                process = await manage_stdio_server(config)
                if process:
                    managed_processes[config.id] = process
                    process_locks[config.id] = asyncio.Lock()
                    should_register_this_agent = True
                else:
                    logger.error(f"Failed to start stdio server {config.name}, skipping discovery and registration.")
                    continue

            # --- Step 2: Discover Tools (only for successfully started stdio for now) ---
            if process:  # Only discover if stdio process is running
                try:
                    # Initialize
                    init_params = {"protocolVersion": "2024-11-05",
                                   "clientInfo": {"name": "AgentManagerInit", "version": "0.1"}, "capabilities": {}}
                    init_req_id = f"init-mgr-{config.id}-{int(time.time())}"  # Added timestamp for more unique ID
                    init_request_obj = MCPRequest(method="initialize", params=init_params, id=init_req_id)
                    init_response_dict = await stdio_mcp_request(process, init_request_obj)  # Renamed for clarity

                    # Check for error in init response
                    if "error" in init_response_dict:
                        logger.error(
                            f"MCP Error during initialize for {config.name}: {json.dumps(init_response_dict['error'])}")
                        raise MCPError(init_response_dict['error'].get('code', -32000),
                                       init_response_dict['error'].get('message', 'Unknown initialization error'))

                    # server_info should come from init_response_dict["result"]["serverInfo"]
                    if "result" in init_response_dict and isinstance(init_response_dict["result"], dict):
                        server_info_from_init = init_response_dict["result"].get("serverInfo")
                        if isinstance(server_info_from_init, dict):
                            server_info.update(server_info_from_init)  # Merge/update
                        else:
                            logger.warning(f"serverInfo from {config.name} is not a dict: {server_info_from_init}")
                    else:
                        logger.warning(
                            f"No 'result' in initialize response from {config.name} or result is not a dict.")

                    # List Tools
                    tools_req_id = f"tools-mgr-{config.id}-{int(time.time())}"  # Added timestamp
                    tools_request_obj = MCPRequest(method="tools/list", params={}, id=tools_req_id)
                    tools_result_dict = await stdio_mcp_request(process, tools_request_obj)  # Renamed for clarity

                    logger.info(
                        f"Raw tools_result_dict for {config.name} from stdio: {json.dumps(tools_result_dict)}")

                    # Check for error in tools/list response
                    if "error" in tools_result_dict:
                        logger.error(
                            f"MCP Error during tools/list for {config.name}: {json.dumps(tools_result_dict['error'])}")
                        # tools list will remain empty as initialized
                    elif "result" in tools_result_dict and isinstance(tools_result_dict["result"], dict) and "tools" in \
                            tools_result_dict["result"]:
                        fetched_tools = tools_result_dict["result"]["tools"]
                        if isinstance(fetched_tools, list):
                            tools = fetched_tools
                            logger.info(
                                f"Successfully discovered {len(tools)} tools for {config.name} (from 'result.tools'): {json.dumps(tools)}")
                        else:
                            logger.error(
                                f"'result.tools' from {config.name} is not a list: {type(fetched_tools)}. Full result: {json.dumps(tools_result_dict['result'])}")
                            # tools list remains empty
                    elif "tools" in tools_result_dict and isinstance(tools_result_dict["tools"],
                                                                     list):  # Fallback for non-standard
                        tools = tools_result_dict["tools"]
                        logger.warning(
                            f"Discovered {len(tools)} tools for {config.name} from non-standard 'tools' key (no 'result' wrapper): {json.dumps(tools)}")
                    else:
                        logger.error(
                            f"Failed to get 'tools' array from tools_list response for {config.name}. Full response: {json.dumps(tools_result_dict)}")
                        # tools list remains empty

                except Exception as e_discover:  # This is the 'except' that was expected
                    logger.error(
                        f"Exception during initial discovery for stdio server {config.name}: {type(e_discover).__name__} - {e_discover}",
                        exc_info=True)
                    logger.warning(f"Proceeding to register {config.name} without discovered tools due to error.")
                    tools = []  # Ensure tools list is empty if discovery failed

            # --- Step 3: Decide if HTTP/SSE servers should be registered ---
            elif config.connection.type in ["http", "sse"]:
                logger.info(
                    f"Config found for {config.connection.type} server: {config.name}. Adding registration task.")
                should_register_this_agent = True
            elif config.connection.type != "stdio":
                logger.warning(
                    f"Unsupported connection type '{config.connection.type}' for server {config.name}. Skipping.")
                continue

            # --- Step 4: Add Registration Task if appropriate ---
            if should_register_this_agent and config.id not in processed_ids_for_registration:
                logger.info(f"Creating registration task for config ID: {config.id}")
                registration_tasks.append(
                    register_with_aira_hub(config, server_info, tools, http_client)
                )
                processed_ids_for_registration.add(config.id)
            elif config.id in processed_ids_for_registration:
                logger.warning(f"Registration task for {config.id} already created in this run. Skipping duplicate.")

        # --- END OF LOOP ---

        if registration_tasks:
            logger.info(f"Waiting for {len(registration_tasks)} registration tasks to complete...")
            await asyncio.gather(*registration_tasks)
            logger.info("Initial registration tasks complete.")
        else:
            logger.info("No valid registration tasks were created.")

    # --- Application runs here ---
    yield
    # --- Teardown ---
    logger.info("Agent Manager shutting down...")
    # Terminate managed stdio processes
    termination_tasks = []
    for server_id, process in managed_processes.items():
       async def terminate_and_wait(sid, proc):
           if proc.returncode is None:
               logger.info(f"Terminating {sid} (PID: {proc.pid})...")
               proc.terminate()
               try:
                   if proc.stdin and not proc.stdin.is_closing(): proc.stdin.close()
                   await asyncio.wait_for(proc.wait(), timeout=5.0)
                   logger.info(f"Process for {sid} terminated (code: {proc.returncode}).")
               except asyncio.TimeoutError:
                   logger.warning(f"Process {sid} kill timeout.")
                   proc.kill()
                   await proc.wait()
               except Exception as e: logger.error(f"Error terminating {sid}: {e}")
       termination_tasks.append(asyncio.create_task(terminate_and_wait(server_id, process)))

    if termination_tasks:
       await asyncio.gather(*termination_tasks)
       await asyncio.sleep(0.5) # Allow final cleanup
    logger.info("Agent Manager shutdown complete.")


# --- FastAPI App and Endpoints ---
app = FastAPI(title="Agent Manager & Stdio Proxy", lifespan=lifespan)

@app.post("/proxy/{server_id}")
async def proxy_stdio_request(server_id: str, request: Request, mcp_request: MCPRequest = Body(...)):
    """Proxies an MCP request to the managed stdio process identified by server_id."""
    logger.info(f"Proxy request received for server_id: {server_id}, method: {mcp_request.method}")

    process = managed_processes.get(server_id)
    config = server_configs.get(server_id)

    if not process or not config or config.connection.type != "stdio":
        logger.error(f"Proxy target server_id '{server_id}' not found or not a managed stdio process.")
        raise HTTPException(status_code=404, detail=f"Stdio server '{server_id}' not managed or found.")

    if process.returncode is not None:
         logger.error(f"Proxy target server_id '{server_id}' (PID: {process.pid}) has already exited with code {process.returncode}.")
         # Optionally try restarting it here? For simplicity, we just error out.
         raise HTTPException(status_code=503, detail=f"Stdio server '{server_id}' is not running.")

    try:
        # Forward request using the stdio MCP function
        mcp_response_dict = await stdio_mcp_request(process, mcp_request)
        # Return the exact response received from the stdio process
        return JSONResponse(content=mcp_response_dict)

    except MCPError as e:
         logger.error(f"MCPError during proxy for {server_id}: {e}")
         # Convert MCPError back to a JSON-RPC error response
         return JSONResponse(
             status_code=500, # Or map MCP codes to HTTP codes if desired
             content={"jsonrpc": "2.0", "id": mcp_request.id, "error": {"code": e.code, "message": e.message, "data": e.data}}
         )
    except Exception as e:
         logger.error(f"Unexpected error during proxy for {server_id}: {type(e).__name__} - {e}", exc_info=True)
         raise HTTPException(status_code=500, detail="Internal proxy error")

# --- Main Entry Point ---
if __name__ == "__main__":
    import uvicorn
    logger.info(f"Starting Agent Manager server on {AGENT_MANAGER_HOST}:{AGENT_MANAGER_PORT}")
    uvicorn.run(
        "__main__:app",
        host=AGENT_MANAGER_HOST,
        port=AGENT_MANAGER_PORT,
        log_level="info"
        # reload=True # Enable reload only if needed for development
    )