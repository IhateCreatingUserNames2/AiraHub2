import asyncio
import json
import logging
import os
import sys
import time
import traceback
from typing import List, Dict, Any, Optional, Tuple

import httpx
from pydantic import BaseModel, Field, ValidationError

# --- Add this block to print the PATH ---
print("-" * 60)
print("Environment PATH as seen by Python script:")
print(os.environ.get('PATH', 'PATH environment variable not found!'))
print("-" * 60)

# --- Configuration & Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("MCPRegistrar")

AIRA_HUB_REGISTER_URL = os.getenv("AIRA_HUB_URL", "http://localhost:8015") + "/register"
CONFIG_FILE = "mcp_servers.json"
MCP_TIMEOUT = 20  # Seconds for MCP calls like initialize, tools/list
HUB_TIMEOUT = 10   # Seconds for registering with AIRA Hub
STDIO_READ_TIMEOUT = 5 # Seconds to wait for a response line from stdio

# --- Pydantic Models for Config Validation (Optional but Recommended) ---
class ConnectionConfig(BaseModel):
    type: str # stdio, http, sse
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

# --- MCP Client Abstraction ---
# Base class or protocol could be used here for more complex scenarios

class MCPError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"MCP Error {code}: {message}")

# --- HTTP MCP Client Logic ---
async def http_mcp_request(client: httpx.AsyncClient, endpoint: str, method: str, params: Dict, req_id: str) -> Dict[str, Any]:
    """Sends a single MCP request via HTTP POST and returns the MCP result or raises MCPError."""
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": req_id}
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    try:
        response = await client.post(endpoint, json=payload, headers=headers, timeout=MCP_TIMEOUT)
        # Log details even before raising
        response_text = "<binary response>" if "application/octet-stream" in response.headers.get("content-type","") else response.text
        logger.debug(f"HTTP MCP Request to {endpoint} ({method}) Status: {response.status_code}, Response text (start): {response_text[:200]}")
        response.raise_for_status()
        mcp_response = response.json()

        if "error" in mcp_response and mcp_response["error"]:
            err = mcp_response["error"]
            raise MCPError(err.get("code", -32000), err.get("message", "Unknown MCP Error"), err.get("data"))
        elif "result" in mcp_response:
            return mcp_response["result"]
        else:
            raise MCPError(-32001, "Invalid MCP response format (no result or error)", mcp_response)

    except httpx.HTTPStatusError as e:
        raise MCPError(-32000, f"HTTP Error {e.response.status_code}", e.response.text[:500]) from e
    except httpx.RequestError as e:
        raise MCPError(-32000, f"Request/Network Error: {type(e).__name__}", str(e)) from e
    except json.JSONDecodeError as e:
        raise MCPError(-32000, "JSON Decode Error", response_text[:500]) from e
    except Exception as e:
        # Catch-all for unexpected issues during the request/response cycle
        raise MCPError(-32000, f"Unexpected client error: {type(e).__name__}", str(e)) from e


# --- Stdio MCP Client Logic ---
async def stdio_mcp_request(process: asyncio.subprocess.Process, method: str, params: Dict, req_id: str) -> Dict[str, Any]:
    """Sends a single MCP request via stdio and returns the result or raises MCPError."""
    if process.stdin is None or process.stdout is None:
        raise MCPError(-32000, "Stdio process stdin/stdout not available")

    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": req_id}
    request_str = json.dumps(payload) + '\n' # MCP stdio uses newline delimiter
    logger.debug(f"STDIO MCP Request ({method}): {request_str.strip()}")

    try:
        process.stdin.write(request_str.encode('utf-8'))
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError) as e:
        raise MCPError(-32000, f"Stdio pipe broken before writing: {type(e).__name__}") from e

    # Read response line by line with timeout
    response_str = ""
    try:
        while True: # Read until newline
             line_bytes = await asyncio.wait_for(process.stdout.readline(), timeout=STDIO_READ_TIMEOUT)
             if not line_bytes: # EOF reached unexpectedly
                  raise MCPError(-32000, "Stdio process closed connection while reading response")
             line_str = line_bytes.decode('utf-8').strip()
             if line_str: # Found non-empty line
                  response_str = line_str
                  break
             # Continue reading if empty lines received
    except asyncio.TimeoutError:
        raise MCPError(-32000, f"Stdio response timed out after {STDIO_READ_TIMEOUT}s") from None
    except (BrokenPipeError, ConnectionResetError) as e:
        raise MCPError(-32000, f"Stdio pipe broken while reading: {type(e).__name__}") from e

    logger.debug(f"STDIO MCP Response ({method}): {response_str}")

    try:
        mcp_response = json.loads(response_str)
        if "error" in mcp_response and mcp_response["error"]:
            err = mcp_response["error"]
            raise MCPError(err.get("code", -32000), err.get("message", "Unknown MCP Error"), err.get("data"))
        elif "result" in mcp_response:
            return mcp_response["result"]
        else:
            raise MCPError(-32001, "Invalid MCP response format (no result or error)", mcp_response)
    except json.JSONDecodeError as e:
        raise MCPError(-32000, "JSON Decode Error parsing stdio response", response_str) from e
    except Exception as e:
        # Catch-all for unexpected issues during parsing
        raise MCPError(-32000, f"Unexpected client error processing stdio response: {type(e).__name__}", str(e)) from e


# --- SSE MCP Client Logic (Placeholder) ---
async def sse_mcp_request(client: httpx.AsyncClient, endpoint: str, method: str, params: Dict, req_id: str) -> Dict[str, Any]:
    """Placeholder for SSE MCP Client logic."""
    logger.warning(f"SSE MCP client not implemented. Cannot process request for {endpoint}")
    raise NotImplementedError("SSE MCP client functionality is not implemented in this script.")


# --- Discovery and Registration Logic ---
async def discover_and_register(
    server_config: ServerConfig,
    http_client: httpx.AsyncClient,
    process_info: Optional[Tuple[asyncio.subprocess.Process, str]] = None # (process, unique_id)
):
    """Connects to an MCP server, discovers tools, and registers with AIRA Hub."""
    logger.info(f"Processing server: {server_config.name} (ID: {server_config.id})")
    server_info: Dict[str, Any] = {"name": server_config.name, "version": "unknown"} # Default
    tools: List[Dict[str, Any]] = []
    connection_type = server_config.connection.type
    process = process_info[0] if process_info else None
    unique_id = process_info[1] if process_info else None # Unique ID for logging if stdio

    try:
        # 1. Initialize with the target MCP server
        init_params = {
            "protocolVersion": "2024-11-05",
            "clientInfo": {"name": "AIRA Hub Registrar", "version": "0.1.0"},
            "capabilities": {}
        }
        init_req_id = f"init-{server_config.id}"

        if connection_type == "http":
            if not server_config.connection.endpoint_url: raise ValueError("HTTP connection needs endpoint_url")
            init_result = await http_mcp_request(http_client, server_config.connection.endpoint_url, "initialize", init_params, init_req_id)
        elif connection_type == "stdio":
            if not process: raise ValueError("Stdio connection needs running process")
            init_result = await stdio_mcp_request(process, "initialize", init_params, init_req_id)
        elif connection_type == "sse":
            if not server_config.connection.endpoint_url: raise ValueError("SSE connection needs endpoint_url")
            # init_result = await sse_mcp_request(http_client, server_config.connection.endpoint_url, "initialize", init_params, init_req_id)
            raise NotImplementedError("SSE client not implemented") # Skip SSE for now
        else:
            raise ValueError(f"Unsupported connection type: {connection_type}")

        server_info = init_result.get("serverInfo", server_info) # Update with actual server info
        logger.info(f"Successfully initialized with {server_config.name} v{server_info.get('version', 'N/A')}")

        # 2. List Tools from the target MCP server
        tools_req_id = f"tools-{server_config.id}"
        tools_result = None
        if connection_type == "http":
             tools_result = await http_mcp_request(http_client, server_config.connection.endpoint_url, "tools/list", {}, tools_req_id)
        elif connection_type == "stdio":
             tools_result = await stdio_mcp_request(process, "tools/list", {}, tools_req_id)
        # Add SSE logic here when implemented

        if tools_result and "tools" in tools_result:
            tools = tools_result["tools"]
            logger.info(f"Discovered {len(tools)} tools from {server_config.name}")
        else:
             logger.warning(f"No 'tools' field found in tools/list response from {server_config.name}. Response: {tools_result}")


    except MCPError as e:
        logger.error(f"MCP Error processing {server_config.name}: {e}")
        # Optionally decide if you still want to register based on partial info
        return # Stop processing this server on MCP error
    except NotImplementedError:
         logger.error(f"Cannot process {server_config.name} due to unimplemented {connection_type} client.")
         return
    except Exception as e:
        logger.error(f"Unexpected error processing {server_config.name}: {type(e).__name__} - {e}")
        logger.error(traceback.format_exc()) # Log full traceback for unexpected errors
        return # Stop processing this server

    # 3. Format and Register with AIRA Hub
    hub_reg_config = server_config.aira_hub_registration
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
        "url": hub_reg_config.base_url,
        "name": server_info.get("name", server_config.name), # Prefer name from init response
        "description": hub_reg_config.description or server_info.get("description", f"MCP Server: {server_config.name}"),
        "version": server_info.get("version", "1.0.0"),
        "mcp_tools": formatted_tools,
        "aira_capabilities": ["mcp"], # Can derive more from init_result["capabilities"] if needed
        "tags": hub_reg_config.tags,
        "category": hub_reg_config.category,
        "provider": {"name": hub_reg_config.provider_name or server_info.get("provider", "Unknown")},
    }

    # Set specific MCP endpoint URLs based on connection type
    if connection_type == "http":
        registration_payload["mcp_url"] = server_config.connection.endpoint_url
        registration_payload["mcp_stream_url"] = server_config.connection.endpoint_url
    elif connection_type == "sse":
        registration_payload["mcp_sse_url"] = server_config.connection.endpoint_url
        registration_payload["mcp_stream_url"] = server_config.connection.endpoint_url # Assume SSE endpoint can handle stream POSTs
    elif connection_type == "stdio":
        # Stdio servers aren't directly reachable via URL by the hub itself
        # Registration indicates discovery, but proxying won't work unless
        # the hub or another gateway manages the stdio process.
        # We can omit the URLs or use the special local:// URL.
        # Omitting might be clearer that the hub cannot proxy directly.
         logger.warning(f"Registering stdio server {server_config.name}. Hub cannot directly proxy calls to it.")
         pass # No mcp_url, mcp_sse_url, mcp_stream_url

    try:
        logger.info(f"Registering '{registration_payload['name']}' with AIRA Hub...")
        reg_headers = {"Content-Type": "application/json"}
        response = await http_client.post(AIRA_HUB_REGISTER_URL, json=registration_payload, headers=reg_headers, timeout=HUB_TIMEOUT)
        response.raise_for_status()
        hub_response = response.json()
        logger.info(f"Successfully registered/updated '{registration_payload['name']}' (ID: {hub_response.get('agent_id')}) with AIRA Hub.")
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP Error registering '{registration_payload['name']}' with AIRA Hub: {e.response.status_code} - {e.response.text[:200]}")
    except Exception as e:
        logger.error(f"Error registering '{registration_payload['name']}' with AIRA Hub: {type(e).__name__} - {e}")


async def manage_stdio_server(config: ServerConfig) -> Optional[Tuple[asyncio.subprocess.Process, str]]:
    """Starts and manages a stdio MCP server process."""
    if not config.connection.command:
        logger.error(f"Cannot start stdio server {config.name}: 'command' not specified.")
        return None

    unique_id = f"stdio-{config.id}-{int(time.time())}" # For logging correlation
    cmd_str = " ".join(config.connection.command) # For logging
    logger.info(f"Starting stdio server '{config.name}' (ID: {unique_id}): {cmd_str}")

    try:
        process = await asyncio.create_subprocess_exec(
            *config.connection.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, # Capture stderr for debugging
            env={**os.environ, **(config.connection.env or {})} # Merge env vars
        )
        logger.info(f"Stdio process for '{config.name}' started (PID: {process.pid})")

        # Optional: Start a background task to log stderr from the process
        async def log_stderr():
            if process.stderr:
                while True:
                    line = await process.stderr.readline()
                    if not line: break
                    logger.warning(f"[{config.name} stderr]: {line.decode(errors='ignore').strip()}")
        asyncio.create_task(log_stderr())

        # Give the process a moment to initialize
        await asyncio.sleep(1) # Adjust as needed, or implement a readiness check

        # Quick check if process exited immediately
        if process.returncode is not None:
             logger.error(f"Stdio process for '{config.name}' exited immediately with code {process.returncode}.")
             return None

        return process, unique_id

    except FileNotFoundError:
         logger.error(f"Cannot start stdio server {config.name}: Command not found: {config.connection.command[0]}")
         return None
    except Exception as e:
        logger.error(f"Failed to start stdio server {config.name}: {e}")
        logger.error(traceback.format_exc())
        return None

async def main():
    """Main function to load config, manage servers, discover, and register."""
    try:
        with open(CONFIG_FILE, 'r') as f:
            raw_config_list = json.load(f)
        # Validate config using Pydantic
        servers_to_process = [ServerConfig(**item) for item in raw_config_list if ServerConfig(**item).enabled]
        logger.info(f"Loaded {len(servers_to_process)} enabled MCP server configurations from {CONFIG_FILE}")
    except FileNotFoundError:
        logger.error(f"Configuration file not found: {CONFIG_FILE}")
        return
    except json.JSONDecodeError:
        logger.error(f"Invalid JSON in configuration file: {CONFIG_FILE}")
        return
    except ValidationError as e:
         logger.error(f"Configuration validation error in {CONFIG_FILE}:\n{e}")
         return
    except Exception as e:
         logger.error(f"Error loading configuration: {e}")
         return

    if not servers_to_process:
        logger.info("No enabled servers found in configuration. Exiting.")
        return

    managed_processes: Dict[str, asyncio.subprocess.Process] = {}
    discovery_tasks = []

    async with httpx.AsyncClient() as hub_http_client:
        # Start local stdio servers first
        for config in servers_to_process:
            if config.connection.type == "stdio":
                process_info = await manage_stdio_server(config)
                if process_info:
                    process, unique_id = process_info
                    managed_processes[config.id] = process # Store process for later termination
                    # Create discovery task *with* the process info
                    task = asyncio.create_task(discover_and_register(config, hub_http_client, process_info))
                    discovery_tasks.append(task)
                else:
                    logger.error(f"Failed to start or manage stdio server {config.name}, skipping registration.")

        # Create discovery tasks for remote servers
        for config in servers_to_process:
             if config.connection.type != "stdio":
                  # Create task *without* process info
                  task = asyncio.create_task(discover_and_register(config, hub_http_client))
                  discovery_tasks.append(task)

        # Wait for all discovery and registration tasks to complete
        if discovery_tasks:
            logger.info(f"Waiting for {len(discovery_tasks)} discovery/registration tasks to complete...")
            await asyncio.gather(*discovery_tasks)
            logger.info("All discovery/registration tasks finished.")
        else:
            logger.info("No discovery tasks were created (perhaps only stdio servers failed to start).")

    # Cleanup: Terminate managed stdio processes
    if managed_processes:
        logger.info(f"Terminating {len(managed_processes)} managed stdio processes...")
        for server_id, process in managed_processes.items():
            if process.returncode is None: # Only terminate if still running
                try:
                    logger.info(f"Terminating process for server {server_id} (PID: {process.pid})...")
                    process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=5)
                    logger.info(f"Process for {server_id} terminated gracefully.")
                except asyncio.TimeoutError:
                    logger.warning(f"Process for {server_id} did not terminate gracefully after 5s, killing...")
                    process.kill()
                    await process.wait() # Wait for kill
                except Exception as e:
                    logger.error(f"Error terminating process for {server_id}: {e}")
            else:
                 logger.info(f"Process for server {server_id} already exited with code {process.returncode}.")

    logger.info("MCP Registrar finished.")

if __name__ == "__main__":
    asyncio.run(main())