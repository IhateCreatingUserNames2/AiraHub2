# AIRA Hub - Discover and BroadCast MCP tools And A2A Skills Thru the Internet 

[![AIRA Hub Status](https://img.shields.io/website?down_message=online&label=AIRA%20Hub%20Status&up_message=online&url=https%3A%2F%2Fairahub2.onrender.com%2Fhealth)](https://airahub2.onrender.com/status)

**AIRA Hub (Active Instance): [https://airahub2.onrender.com/](https://airahub2.onrender.com/)**

*** HTTP URL : https://airahub2.onrender.com/mcp/stream *** 

A modular system for integrating local and remote MCP (Model Context Protocol) and A2A (Agent-to-Agent) servers with a central registry. This enables AI assistants like Claude Desktop to discover and leverage a diverse ecosystem of tools and agents.

To test demo in claude use the Json or edit your Json.  https://github.com/IhateCreatingUserNames2/AiraHub2/blob/main/claude_desktop_config.json 


Lobe-Chat Demo : 

![image](https://github.com/user-attachments/assets/54120dc2-83a3-462d-b31d-4b7f3feadc18)

Custom Plugin Configuration :
![image](https://github.com/user-attachments/assets/fd4f0742-3cb8-4141-8fba-02a68ea5d1b2)


## Overview

This project facilitates a powerful new way for AI agents to collaborate and extend their capabilities. It consists of two primary components:

1.  **AIRA Hub (`aira_hub.py`)**:
    *   A central, cloud-hostable registry and intelligent proxy for MCP and A2A agents/servers.
    *   Enables dynamic discovery of agents and their tools/skills.
    *   Routes tool calls and task requests to the appropriate registered agents, whether they are remote HTTP-based agents or local stdio-based processes (via the Agent Manager).
    *   Supports A2A agent registration and translates A2A skills into MCP-compatible tools for broader accessibility.
    *   Utilizes MongoDB for persistent storage of agent registrations and task information.

2.  **Agent Manager (`agent_manager.py`)** (Optional, for local stdio servers, Example of How to Host an MCP Server and BroadCast it In Aira hub):
    *   A local service that runs on a user's machine or private network.
    *   Manages the lifecycle of local, stdio-based MCP servers (e.g., those run via `npx` or local Python scripts).
    *   Registers these local servers with a remote AIRA Hub instance, making their tools discoverable and usable by clients connected to the Hub.
    *   Requires a tunneling service (like ngrok or Cloudflare Tunnel) to expose itself to the AIRA Hub.

3.  ** LAIN (https://github.com/IhateCreatingUserNames2/Lain)** (Optional, for AI Agents, This Example is An AI Agent made with google ADK using A2A protocol, broadcasting A2A Skills(Conversation in this example) thru the Aira HUB):
    *   Uses OpenRouter for LLM inference

This architecture allows AI clients like Claude Desktop to seamlessly discover and utilize tools provided by various agents, including local processes running on your machine or specialized cloud-hosted agents, all through a unified AIRA Hub interface.



## System Architecture
Use code with caution.
Markdown
+-----------------+ +-----------------------+ +------------------------+
| MCP/A2A Client | ------> | AIRA Hub | <------> | Remote MCP/A2A Agent |
| (Claude Desktop,| | (Cloud - e.g., Render)| | (HTTP/S Endpoint) |
| Other Agents) | +-----------------------+ +------------------------+
+-----------------+ ^
| (Registration & Tool Calls via Tunnel)
|
v
+-------+-----------------+
| User's Local Machine / |
| Private Network |
| |
| +-------------------+ |
| | Agent Manager | |
| | (localhost) | |
| +-------+-----------+ |
| | |
| v |
| +-------+-----------+ |
| | Local MCP Server | |
| | (stdio process) | |
| +-------------------+ |
+-------------------------+
## Features

*   **Decentralized Agent Registration**: Agents can register from anywhere.
*   **MCP & A2A Protocol Support**: Bridges A2A agents into the MCP ecosystem.
*   **Tool/Skill Discovery**: Clients can query the Hub for available capabilities.
*   **Intelligent Routing**: Forwards requests to the correct agent based on tool/skill ID.
*   **Local Server Integration**: `Agent Manager` allows local stdio-based tools to be part of the network.
*   **Persistent Storage**: Uses MongoDB to store agent and task information.
*   **Streamable HTTP**: Leverages the modern MCP Streamable HTTP transport for efficient bi-directional communication.

## Prerequisites

*   **Python 3.8+**
*   **Node.js & npm/npx** (for running some community MCP servers or the `mcp-remote` client)
*   **MongoDB Atlas Account** (or a self-hosted MongoDB instance) for AIRA Hub.
*   **(Optional) Tunneling Service**: ngrok or Cloudflare Tunnel if you plan to use `Agent Manager` for local stdio servers.
*   **Python Packages**:
    *   **AIRA Hub**: `fastapi`, `uvicorn`, `motor`, `pydantic`, `python-dotenv`, `httpx`
    *   **(Optional) Agent Manager**: `fastapi`, `uvicorn`, `pydantic`, `python-dotenv`, `httpx`

## Setup Instructions

### 1. AIRA Hub (Cloud Deployment - e.g., on Render)

1.  **Clone the Repository**:
    ```bash
    git clone https://github.com/your-username/aira-hub-project.git # Replace with your repo
    cd aira-hub-project
    ```
2.  **Install Dependencies for `aira_hub.py`**:
    ```bash
    pip install fastapi uvicorn motor pydantic python-dotenv httpx
    ```
3.  **Configure Environment Variables**:
    Create a `.env` file in the directory containing `aira_hub.py` (or set environment variables directly on your hosting platform):
    ```env
    MONGODB_URL="your_mongodb_connection_string" # e.g., mongodb+srv://user:pass@cluster.mongodb.net/aira_hub?retryWrites=true&w=majority
    # Optional:
    # PORT=8017 # Default for local run, Render will use its own
    # HOST="0.0.0.0"
    # DEBUG="false" # Set to "true" for more verbose logging and debug endpoints
    # AGENT_CALL_TIMEOUT_SECONDS="120.0" # Timeout for calls from Hub to downstream agents
    ```
4.  **Deploy**:
    *   Deploy `aira_hub.py` to a platform like Render, Heroku, or any ASGI-compatible host.
    *   Ensure the `MONGODB_URL` environment variable is correctly set in your deployment environment.
    *   The active public instance is: **[https://airahub2.onrender.com/](https://airahub2.onrender.com/)**

### 2. (Optional) Agent Manager (For Local stdio MCP Servers)

If you want to make tools from local stdio-based MCP servers (like many `npx @modelcontextprotocol/server-*` examples) accessible through your AIRA Hub:

1.  **Navigate** to the directory containing `agent_manager.py`.
2.  **Install Dependencies**:
    ```bash
    pip install fastapi uvicorn pydantic python-dotenv httpx
    ```
3.  **Configure `mcp_servers.json`**:
    Create this file in the same directory as `agent_manager.py`. Define your local stdio MCP servers. Example:
    ```json
    [
      {
        "id": "sequential-thinking-local-stdio",
        "name": "Sequential Thinking (Local)",
        "enabled": true,
        "connection": {
          "type": "stdio",
          "command": ["npx.cmd", "-y", "@modelcontextprotocol/server-sequential-thinking"]
        },
        "aira_hub_registration": {
          "base_url": "local://mcpservers.org/sequentialthinking/stdio/managed",
          "description": "Sequential Thinking MCP Server (Managed by Local Agent Manager)",
          "tags": ["thinking", "problem-solving", "ai", "local", "managed"],
          "category": "AI Tools",
          "provider_name": "LocalAgentManager/mcpservers.org"
        }
      }
      // Add other local servers here
    ]
    ```
    *   **Important**: For Windows, `npx.cmd` might be needed. For Linux/macOS, use `npx`.
    *   The `base_url` in `aira_hub_registration` should be a unique identifier. `local://...` is a convention for locally managed agents.

4.  **Configure Agent Manager Environment Variables**:
    Create a `.env` file in the Agent Manager directory:
    ```env
    AIRA_HUB_URL="https://airahub2.onrender.com" # URL of YOUR deployed AIRA Hub
    AGENT_MANAGER_PUBLIC_URL="your_publicly_accessible_tunnel_url" # e.g., https://your-random-name.ngrok-free.app
    # Optional:
    # AGENT_MANAGER_PORT=9010
    # AGENT_MANAGER_HOST="localhost"
    ```
5.  **Set up Tunneling (ngrok or Cloudflare Tunnel)**:
    *   **ngrok**:
        ```bash
        ngrok http 9010 # Assuming Agent Manager runs on port 9010
        ```
        Copy the HTTPS forwarding URL provided by ngrok into `AGENT_MANAGER_PUBLIC_URL`.
    *   **Cloudflare Tunnel**: Follow Cloudflare's documentation to create a tunnel pointing to `http://localhost:9010`.
        ```bash
        cloudflared tunnel --url http://localhost:9010 run <your-tunnel-name>
        ```
        Use the public URL of your tunnel for `AGENT_MANAGER_PUBLIC_URL`.

6.  **Run Agent Manager**:
    ```bash
    python agent_manager.py
    ```
    The Agent Manager will start your configured local MCP servers and register them with the AIRA Hub using its public tunnel URL.

### 3. Connecting Claude Desktop (or other MCP Clients)

To use tools registered with an AIRA Hub instance in Claude Desktop:

1.  Locate Claude Desktop's configuration file:
    *   Windows: `%APPDATA%\Claude\claude_desktop_config.json`
    *   macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
    *   Linux: `~/.config/Claude/claude_desktop_config.json`
2.  Edit the `mcpServers` section to include your AIRA Hub. **You usually want to use `mcp-remote` for cloud-hosted Hubs**:

    ```json
    {
      "mcpServers": {
        "my-aira-hub": { // Choose a descriptive name
          "command": "npx",
          "args": [
            "mcp-remote", // Use mcp-remote to connect to a Streamable HTTP endpoint
            "https://airahub2.onrender.com/mcp/stream" // URL to YOUR AIRA Hub's MCP stream endpoint
          ]
        }
        // You can add other MCP servers here too
      }
    }
    ```
    *   Replace `"https://airahub2.onrender.com/mcp/stream"` with the URL of your deployed AIRA Hub instance if it's different.

3.  Restart Claude Desktop. Your AIRA Hub should now appear as a source, and its registered tools (including those from local agents via Agent Manager or A2A agents) should be discoverable.

## Example: Using Lain (A2A ADK Agent) via AIRA Hub

The Lain agent ([https://github.com/IhateCreatingUserNames2/Lain](https://github.com/IhateCreatingUserNames2/Lain)) is an A2A-compatible agent built with Google's Agent Development Kit (ADK).

1.  **Run Lain**: Ensure your Lain A2A agent is running and publicly accessible (e.g., via ngrok).
2.  **Register Lain with AIRA Hub**:
    *   Send a POST request to your AIRA Hub's `/register` endpoint with Lain's details. The Hub will fetch Lain's `agent.json`, discover its A2A skills, and translate them into MCP tools.
    ```json
    // Example payload for registering Lain (adjust URL and details as needed)
    {
      "url": "https://your-lain-ngrok-url.ngrok-free.app", // Lain's public base URL
      "name": "Lain_ADK_A2A_ngrok",
      "description": "Lain - An ADK-based A2A agent with memory capabilities.",
      "aira_capabilities": ["a2a", "mcp"], // Indicates it's A2A, and Hub will bridge to MCP
      "tags": ["adk", "memory", "conversational"],
      "category": "AI Assistants"
    }
    ```
3.  **Connect Claude Desktop to AIRA Hub** (as described in the previous section).
4.  **Interact**: You should now be able to see and use Lain's capabilities (e.g., "General Conversation & Memory Interaction") as tools within Claude Desktop, proxied through AIRA Hub.

   ![Claude using Lain via AIRA Hub](https://github.com/user-attachments/assets/082459bb-d8b8-4a9f-b2d7-4483f235b393)
   *Successful interaction with Lain, asking about SUV cars, proxied via AIRA Hub.*

## Tool Call Flow (Simplified)

1.  **Client (Claude)**: Sends a `tools/call` request for "Lain_ADK_A2A_ngrok_A2A_general_conversation" to **AIRA Hub**.
2.  **AIRA Hub**:
    *   Looks up the tool and finds it's an A2A-bridged tool provided by the registered "Lain_ADK_A2A_ngrok" agent.
    *   Reads the annotations for the tool (e.g., `aira_a2a_target_skill_id`, `aira_a2a_agent_url`).
    *   Constructs an A2A `tasks/send` JSON-RPC request.
    *   Sends this A2A request to **Lain's A2A Wrapper URL**.
3.  **Lain's A2A Wrapper**:
    *   Receives the A2A `tasks/send` request.
    *   Invokes its internal ADK agent with the user input.
    *   The ADK agent processes the request (potentially using its tools/memory).
    *   Lain's wrapper forms an A2A JSON-RPC response containing the result (e.g., text in an artifact).
    *   Sends the A2A response back to **AIRA Hub**.
4.  **AIRA Hub**:
    *   Receives Lain's A2A response.
    *   Translates the A2A result (e.g., extracts text from the artifact) into an MCP `CallToolResult` format (specifically, `{"content": [{"type": "text", "text": "..."}]}`).
    *   Sends this structured MCP response back to the **Client (Claude)** over the Streamable HTTP connection.
5.  **Client (Claude)**: Receives and displays Lain's response.

## API Endpoints

The AIRA Hub exposes several API endpoints:

*   `/register` (POST): Register or update an agent.
*   `/heartbeat/{agent_id}` (POST): Agent heartbeat.
*   `/agents` (GET): List registered agents (supports filtering).
*   `/agents/{agent_id}` (GET, DELETE): Get or unregister a specific agent.
*   `/tools` (GET): List available MCP tools.
*   `/tags` (GET): List unique tags.
*   `/categories` (GET): List unique categories.
*   `/status` (GET): System status.
*   `/health` (GET): Basic health check.
*   `/mcp/stream` (POST): The primary MCP Streamable HTTP endpoint for client communication.
*   `/a2a/discover` (POST): Discover A2A-capable agents.
*   `/a2a/skills` (GET): List A2A skills.
*   `/a2a/tasks/send` (POST): Submit an A2A task.
*   `/a2a/tasks/{task_id}` (GET): Get A2A task status.
*   `/admin/*`: Administrative endpoints (sync, cleanup, broadcast).
*   *(Debug) `/debug/register-test-agent` (POST): Registers a test agent if `DEBUG` mode is on.*

Refer to the `/docs` or `/redoc` paths on your running AIRA Hub instance for detailed OpenAPI documentation.

## Troubleshooting

*   **Connection Issues to AIRA Hub**:
    *   Verify your AIRA Hub (e.g., on Render) is running and accessible.
    *   Check the URL in your Claude Desktop `mcpServers` configuration. It must point to the `/mcp/stream` endpoint.
    *   Ensure `mcp-remote` is correctly installed and accessible in your `npx` environment if used by Claude.
*   **Agent Not Appearing / Tools Missing**:
    *   **AIRA Hub Logs**: Check for errors during agent registration.
    *   **Agent Manager Logs (if used)**: Ensure Agent Manager started successfully and registered its local servers with the Hub. Check for errors related to tunnel connectivity or stdio server startup.
    *   **Tunnel Status**: Verify your ngrok/Cloudflare tunnel is active and pointing to the correct local Agent Manager port.
    *   **Agent Status on Hub**: Use the `/agents` or `/status` endpoint on AIRA Hub to check if the agent is listed and `ONLINE`. Check its `last_seen` timestamp.
*   **Tool Calls Failing or Timing Out**:
    *   **AIRA Hub Logs**: Look for errors when forwarding requests to downstream agents (A2A or MCP). Note any timeouts or HTTP errors.
    *   **Downstream Agent Logs (Lain, Local MCP Server)**: Check if the agent received the request from AIRA Hub and if it encountered any internal errors during processing.
    *   **`AGENT_CALL_TIMEOUT_SECONDS` on AIRA Hub**: If downstream agents take a long time to respond (like Lain), ensure this timeout on the Hub is set sufficiently high.
    *   **MCP Response Structure**: Ensure agents (or AIRA Hub when translating A2A) are returning MCP responses in the exact format expected by the client (e.g., `tools/call` result structure). ZodErrors in `mcp-remote` often point to this.

## Contributing

Contributions are welcome! Please feel free to submit pull requests or open issues.

## License

(Specify your project's license here, e.g., MIT, Apache 2.0)

---

This README provides a comprehensive guide. Remember to replace placeholders like `your-username/aira-hub-project.git`, `your_mongodb_connection_string`, and specific ngrok URLs with your actual values.
