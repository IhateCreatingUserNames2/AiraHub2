# AIRA Hub & Agent Manager

A modular system for integrating local MCP (Model Context Protocol) servers with a central registry to enable AI assistants like Claude to leverage local tools.

## Overview

This project consists of two main components:

- **AIRA Hub** (`aira_hub.py`): A central registry and proxy for MCP servers that enables discovery and routing of tool requests
- **Agent Manager** (`agent_manager.py`): A service that manages local stdio-based MCP servers and registers them with AIRA Hub

This architecture enables clients like Claude Desktop to discover and use tools provided by local processes without connecting directly to them.

## System Architecture

```
+-----------------+         +-------------------+        +------------------------+
| MCP Client      |         | AIRA Hub          |        | User's Local Machine   |
| (Claude Desktop)|-------->| (Remote Server)   |------->|                        |
+-----------------+         +-------------------+        | +-----------------+    |
                            |                   |        | | Agent Manager   |    |
                            |                   |<-------| | (localhost)     |    |
                            +--------+----------+        | +---------+-------+    |
                                     |                   |           |            |
                                     |                   |           |            |
                                     v                   |           v            |
                            +--------+----------+        | +---------+-------+    |
                            | MongoDB           |        | | Local MCP Server |    |
                            | (Database)        |        | | (stdio process)  |    |
                            +-------------------+        | +-----------------+    |
                                                         +------------------------+
```

## Prerequisites

- Python 3.8+
- Node.js & npm/npx (for certain MCP servers)
- MongoDB (local or cloud)
- Tunneling service (ngrok or Cloudflare Tunnel)
- Required Python packages:
  - For AIRA Hub: `fastapi`, `uvicorn`, `motor`, `pydantic`, `python-dotenv`, `httpx`
  - For Agent Manager: `fastapi`, `uvicorn`, `pydantic`, `python-dotenv`, `httpx`

## Setup Instructions

### 1. Set up AIRA Hub

1. Install dependencies: `pip install fastapi uvicorn motor pydantic python-dotenv httpx`
2. Configure MongoDB connection in `.env`:
   ```
   MONGODB_URL=mongodb+srv://user:pass@cluster.mongodb.net/aira_hub?retryWrites=true&w=majority
   ```
3. Run the hub: `python aira_hub.py`

### 2. Set up Agent Manager

1. Install dependencies: `pip install fastapi uvicorn pydantic python-dotenv httpx`
2. Create `mcp_servers.json` to configure your local MCP servers
3. Create `.env` file with required environment variables:
   ```
   AIRA_HUB_URL=https://your-hub.onrender.com
   AGENT_MANAGER_PUBLIC_URL=https://your-tunnel-url.ngrok-free.app
   ```

### 3. Set up Tunneling

1. Choose and install a tunneling service (ngrok or Cloudflare Tunnel)
2. Start the tunnel pointing to your Agent Manager port:
   - ngrok: `ngrok http 9010`
   - Cloudflare: `cloudflared tunnel --url http://localhost:9010 run <your-tunnel-name>`
3. Update the `AGENT_MANAGER_PUBLIC_URL` in your `.env` file with the public URL

### 4. Run The System

Start components in this order:
1. MongoDB
2. AIRA Hub: `python aira_hub.py`
3. Tunnel service
4. Agent Manager: `python agent_manager.py`

### 5. Connect Claude Desktop

Edit Claude Desktop's configuration file to include your AIRA Hub instance:
```json
{
  "mcpServers": {
    "my-remote-hub": {
      "url": "https://your-hub.onrender.com/mcp/stream"
    }
  }
}
```

## Tool Call Flow

1. Claude Desktop sends tool call to AIRA Hub
2. AIRA Hub identifies registered agent and forwards request to tunnel URL
3. Tunnel forwards request to local Agent Manager
4. Agent Manager processes request through appropriate stdio MCP server
5. Response flows back through the same chain to Claude Desktop

## Troubleshooting

Common issues:
- Connection errors: Check tunnel configuration and URL registration
- Missing tools: Verify server enabled status and proper registration
- Process errors: Check command paths and environment variables

## Configuration Examples

### Example mcp_servers.json

```json
[
  {
    "id": "sequential-thinking-local-stdio",
    "name": "Sequential Thinking",
    "enabled": true,
    "connection": {
      "type": "stdio",
      "command": ["npx.cmd", "-y", "@modelcontextprotocol/server-sequential-thinking"]
    },
    "aira_hub_registration": {
      "base_url": "local://mcpservers.org/sequentialthinking/stdio/managed",
      "description": "Sequential Thinking MCP Server (Managed)",
      "tags": ["thinking", "problem-solving", "ai", "local", "managed"],
      "category": "AI Tools",
      "provider_name": "AgentManager/mcpservers.org"
    }
  }
]
```

For more details, consult the full documentation.
