"""
AIRA Hub: A Decentralized Registry for MCP and A2A Tools

This implementation provides:
- Peer-to-peer agent discovery and registration
- MCP tools proxy and aggregation
- Zero-config integration with LLM clients (Claude, etc.)
- MongoDB integration for persistent storage
- Support for both MCP and A2A protocols
"""

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Set, Any, Union, Callable, Awaitable
from enum import Enum
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, BackgroundTasks, WebSocket, WebSocketDisconnect, HTTPException, Depends
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator
from sse_starlette.sse import EventSourceResponse
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("aira_hub.log")
    ]
)
logger = logging.getLogger("aira_hub")


# Constants
DEFAULT_HEARTBEAT_TIMEOUT = 300  # 5 minutes
DEFAULT_HEARTBEAT_INTERVAL = 60  # 1 minute
DEFAULT_CLEANUP_INTERVAL = 120  # 2 minutes
MAX_TOOLS_PER_AGENT = 100
DB_PERSIST_INTERVAL = 300  # 5 minutes

# MongoDB configuration
MONGODB_URL = os.getenv("MONGODB_URL")

# Create the MongoDB client
client = None
db = None


class AgentStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"


class ResourceType(str, Enum):
    MCP_TOOL = "mcp_tool"
    MCP_RESOURCE = "mcp_resource"
    MCP_PROMPT = "mcp_prompt"
    A2A_SKILL = "a2a_skill"
    API_ENDPOINT = "api_endpoint"
    OTHER = "other"


class MCPTool(BaseModel):
    """Representation of an MCP tool"""
    name: str
    description: Optional[str] = None
    inputSchema: Dict[str, Any]
    annotations: Optional[Dict[str, Any]] = None


class A2ASkill(BaseModel):
    """Representation of an A2A skill"""
    id: str
    name: str
    description: str
    version: str = "1.0.0"
    tags: List[str] = []
    parameters: Dict[str, Any] = {}
    examples: List[str] = []


class AgentMetrics(BaseModel):
    """Metrics for an agent"""
    request_count: int = 0
    success_count: int = 0
    error_count: int = 0
    avg_response_time: float = 0
    uptime: float = 0
    last_updated: float = Field(default_factory=time.time)


class AgentRegistration(BaseModel):
    """Agent registration information"""
    url: str
    name: str
    description: Optional[str] = None
    version: str = "1.0.0"

    # Capabilities and resources
    mcp_tools: List[MCPTool] = []
    a2a_skills: List[A2ASkill] = []
    aira_capabilities: List[str] = []

    # Status information
    status: AgentStatus = AgentStatus.ONLINE
    last_seen: float = Field(default_factory=time.time)
    created_at: float = Field(default_factory=time.time)

    # Additional metadata
    metrics: Optional[AgentMetrics] = None
    tags: List[str] = []
    category: Optional[str] = None
    provider: Optional[Dict[str, str]] = None

    # Connection details for MCP
    mcp_url: Optional[str] = None
    mcp_sse_url: Optional[str] = None

    # Unique ID for this registration
    agent_id: str = Field(default_factory=lambda: str(uuid.uuid4()))

    @validator('url')
    def url_must_be_valid(cls, v):
        if not v.startswith(('http://', 'https://')):
            raise ValueError('URL must start with http:// or https://')
        return v

    @validator('mcp_tools')
    def max_tools(cls, v):
        if len(v) > MAX_TOOLS_PER_AGENT:
            raise ValueError(f'Maximum of {MAX_TOOLS_PER_AGENT} tools allowed')
        return v

    class Config:
        schema_extra = {
            "example": {
                "url": "https://weather-agent.example.com",
                "name": "Weather Agent",
                "description": "Provides weather forecasts and historical data",
                "mcp_tools": [
                    {
                        "name": "get_weather",
                        "description": "Get current weather for a location",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "location": {
                                    "type": "string",
                                    "description": "City name or coordinates"
                                }
                            },
                            "required": ["location"]
                        }
                    }
                ],
                "aira_capabilities": ["mcp", "a2a"],
                "tags": ["weather", "forecast"],
                "category": "utilities",
                "mcp_url": "https://weather-agent.example.com/mcp/messages",
                "mcp_sse_url": "https://weather-agent.example.com/mcp/sse"
            }
        }


class DiscoverQuery(BaseModel):
    """Query parameters for agent discovery"""
    skill_id: Optional[str] = None
    skill_tags: Optional[List[str]] = None
    tool_name: Optional[str] = None
    agent_tags: Optional[List[str]] = None
    category: Optional[str] = None
    status: Optional[AgentStatus] = None
    capabilities: Optional[List[str]] = None
    offset: int = 0
    limit: int = 100


class MCPRequest(BaseModel):
    """MCP JSON-RPC request"""
    jsonrpc: str = "2.0"
    id: Optional[Union[str, int]] = None
    method: str
    params: Optional[Dict[str, Any]] = None


class MCPResponse(BaseModel):
    """MCP JSON-RPC response"""
    jsonrpc: str = "2.0"
    id: Optional[Union[str, int]] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None


# ==================== MongoDB Integration ====================

class MongoDBStorage:
    """MongoDB-based storage backend"""

    def __init__(self, connection_string: str):
        self.connection_string = connection_string
        self.client = None
        self.db = None
        self.tool_cache = {}  # Cache for tool lookups

    async def init(self):
        """Initialize the MongoDB connection"""
        try:
            # Create the MongoDB client
            self.client = AsyncIOMotorClient(self.connection_string)

            # Get database reference directly
            self.db = self.client.aira_hub

            # Create indexes for better query performance
            await self.db.agents.create_index("url", unique=True)
            await self.db.agents.create_index("agent_id", unique=True)
            await self.db.agents.create_index("last_seen")
            await self.db.agents.create_index("status")
            await self.db.agents.create_index("tags")
            await self.db.agents.create_index("aira_capabilities")
            await self.db.agents.create_index("category")
            await self.db.agents.create_index("mcp_tools.name")  # Index for tool lookups

            # Test connection
            await self.client.admin.command('ping')
            logger.info(f"Connected to MongoDB database: aira_hub")

            # Initialize tool cache
            await self.refresh_tool_cache()

        except Exception as e:
            logger.error(f"Error connecting to MongoDB: {str(e)}")
            raise e

    async def close(self):
        """Close the MongoDB connection"""
        if self.client:
            self.client.close()
            logger.info("MongoDB connection closed")

    async def refresh_tool_cache(self):
        """Refresh the tool cache from the database"""
        self.tool_cache = {}
        cursor = self.db.agents.find({"status": AgentStatus.ONLINE})

        async for agent in cursor:
            # Using agent_id as string to be consistent
            agent_id = str(agent.get("agent_id"))

            for tool in agent.get("mcp_tools", []):
                tool_name = tool.get("name")
                if tool_name:
                    self.tool_cache[tool_name] = agent_id

        logger.info(f"Tool cache refreshed with {len(self.tool_cache)} tools")

    async def save_agent(self, agent: AgentRegistration):
        """Save an agent to MongoDB"""
        # Convert model to dict
        agent_dict = agent.dict()

        # Update the tool cache
        for tool in agent.mcp_tools:
            self.tool_cache[tool.name] = agent.agent_id

        try:
            # Use upsert to create or update
            result = await self.db.agents.update_one(
                {"agent_id": agent.agent_id},
                {"$set": agent_dict},
                upsert=True
            )

            # Log the operation result
            if result.upserted_id:
                logger.info(f"New agent created: {agent.name} ({agent.agent_id})")
            else:
                logger.info(f"Agent updated: {agent.name} ({agent.agent_id})")

            return True

        except Exception as e:
            logger.error(f"Error saving agent {agent.name} ({agent.agent_id}): {str(e)}")
            return False

    async def get_agent(self, agent_id: str) -> Optional[AgentRegistration]:
        """Get an agent by ID"""
        try:
            agent_dict = await self.db.agents.find_one({"agent_id": agent_id})
            if agent_dict:
                # Convert ObjectId to string for _id if present
                if "_id" in agent_dict and isinstance(agent_dict["_id"], ObjectId):
                    agent_dict["_id"] = str(agent_dict["_id"])

                return AgentRegistration(**agent_dict)
            return None

        except Exception as e:
            logger.error(f"Error retrieving agent {agent_id}: {str(e)}")
            return None

    async def get_agent_by_url(self, url: str) -> Optional[AgentRegistration]:
        """Get an agent by URL"""
        try:
            agent_dict = await self.db.agents.find_one({"url": url})
            if agent_dict:
                # Convert ObjectId to string for _id if present
                if "_id" in agent_dict and isinstance(agent_dict["_id"], ObjectId):
                    agent_dict["_id"] = str(agent_dict["_id"])

                return AgentRegistration(**agent_dict)
            return None

        except Exception as e:
            logger.error(f"Error retrieving agent by URL {url}: {str(e)}")
            return None

    async def get_agent_by_tool(self, tool_name: str) -> Optional[AgentRegistration]:
        """Get an agent that provides a specific tool"""
        try:
            # Check cache first for fast lookup
            agent_id = self.tool_cache.get(tool_name)
            if agent_id:
                return await self.get_agent(agent_id)

            # If not in cache, query the database
            agent_dict = await self.db.agents.find_one(
                {"mcp_tools.name": tool_name, "status": AgentStatus.ONLINE}
            )

            if agent_dict:
                # Update cache
                self.tool_cache[tool_name] = agent_dict["agent_id"]

                # Convert ObjectId to string for _id if present
                if "_id" in agent_dict and isinstance(agent_dict["_id"], ObjectId):
                    agent_dict["_id"] = str(agent_dict["_id"])

                return AgentRegistration(**agent_dict)

            return None

        except Exception as e:
            logger.error(f"Error retrieving agent by tool {tool_name}: {str(e)}")
            return None

    async def list_agents(self) -> List[AgentRegistration]:
        """List all agents"""
        agents = []
        try:
            cursor = self.db.agents.find()

            async for agent_dict in cursor:
                # Convert ObjectId to string for _id if present
                if "_id" in agent_dict and isinstance(agent_dict["_id"], ObjectId):
                    agent_dict["_id"] = str(agent_dict["_id"])

                agents.append(AgentRegistration(**agent_dict))

            return agents

        except Exception as e:
            logger.error(f"Error listing agents: {str(e)}")
            return []

    async def list_tools(self) -> List[Dict[str, Any]]:
        """List all tools from all agents"""
        tools = []
        try:
            # Optimize query to only fetch what we need
            cursor = self.db.agents.find(
                {"status": AgentStatus.ONLINE},
                {"mcp_tools": 1, "name": 1, "agent_id": 1}
            )

            async for agent in cursor:
                agent_name = agent.get("name", "Unknown")
                agent_id = agent.get("agent_id", "")

                for tool in agent.get("mcp_tools", []):
                    tools.append({
                        "name": tool.get("name"),
                        "description": tool.get("description"),
                        "agent": agent_name,
                        "agent_id": agent_id,
                        "inputSchema": tool.get("inputSchema")
                    })

            return tools

        except Exception as e:
            logger.error(f"Error listing tools: {str(e)}")
            return []

    async def delete_agent(self, agent_id: str):
        """Delete an agent by ID"""
        try:
            # Get the agent first to update the tool cache
            agent = await self.get_agent(agent_id)
            if agent:
                # Remove from tool cache
                for tool in agent.mcp_tools:
                    if tool.name in self.tool_cache:
                        del self.tool_cache[tool.name]

                # Delete from database
                result = await self.db.agents.delete_one({"agent_id": agent_id})
                if result.deleted_count > 0:
                    logger.info(f"Agent deleted: {agent.name} ({agent_id})")
                    return True

            return False

        except Exception as e:
            logger.error(f"Error deleting agent {agent_id}: {str(e)}")
            return False

    async def update_agent_heartbeat(self, agent_id: str, timestamp: float):
        """Update agent heartbeat timestamp"""
        try:
            result = await self.db.agents.update_one(
                {"agent_id": agent_id},
                {"$set": {"last_seen": timestamp}}
            )

            return result.modified_count > 0

        except Exception as e:
            logger.error(f"Error updating heartbeat for agent {agent_id}: {str(e)}")
            return False

    async def count_agents(self, query: Dict[str, Any] = None) -> int:
        """Count agents matching a query"""
        try:
            return await self.db.agents.count_documents(query or {})

        except Exception as e:
            logger.error(f"Error counting agents: {str(e)}")
            return 0

    async def search_agents(self, query: Dict[str, Any] = None, skip: int = 0, limit: int = 100) -> List[
        AgentRegistration]:
        """Search agents with pagination"""
        agents = []
        try:
            cursor = self.db.agents.find(query or {}).skip(skip).limit(limit)

            async for agent_dict in cursor:
                # Convert ObjectId to string for _id if present
                if "_id" in agent_dict and isinstance(agent_dict["_id"], ObjectId):
                    agent_dict["_id"] = str(agent_dict["_id"])

                agents.append(AgentRegistration(**agent_dict))

            return agents

        except Exception as e:
            logger.error(f"Error searching agents: {str(e)}")
            return []


# ==================== MCP Session Manager ====================

class MCPSession:
    """Manages MCP sessions and connection state"""

    def __init__(self, storage):
        self.active_sessions: Dict[str, Dict[str, Any]] = {}
        self.client_connections: Dict[str, List[str]] = {}  # Maps client IDs to session IDs
        self.storage = storage

    def create_session(self, client_id: str) -> str:
        """Create a new MCP session"""
        session_id = str(uuid.uuid4())
        self.active_sessions[session_id] = {
            "client_id": client_id,
            "created_at": time.time(),
            "last_activity": time.time(),
            "state": {}
        }

        # Add to client connections
        if client_id not in self.client_connections:
            self.client_connections[client_id] = []
        self.client_connections[client_id].append(session_id)

        return session_id

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get session by ID"""
        return self.active_sessions.get(session_id)

    def update_session_activity(self, session_id: str):
        """Update session last activity timestamp"""
        if session_id in self.active_sessions:
            self.active_sessions[session_id]["last_activity"] = time.time()

    def close_session(self, session_id: str):
        """Close a session"""
        if session_id in self.active_sessions:
            client_id = self.active_sessions[session_id]["client_id"]
            if client_id in self.client_connections:
                if session_id in self.client_connections[client_id]:
                    self.client_connections[client_id].remove(session_id)
            del self.active_sessions[session_id]

    def get_client_sessions(self, client_id: str) -> List[str]:
        """Get all sessions for a client"""
        return self.client_connections.get(client_id, [])

    def cleanup_stale_sessions(self, max_age: int = 3600):
        """Clean up stale sessions older than max_age seconds"""
        now = time.time()
        to_remove = []

        for session_id, session in self.active_sessions.items():
            if now - session["last_activity"] > max_age:
                to_remove.append(session_id)

        for session_id in to_remove:
            self.close_session(session_id)

        return len(to_remove)


# ==================== Active Connections Manager ====================

class ConnectionManager:
    """Manages SSE connections to clients"""

    def __init__(self):
        self.active_connections: Dict[str, Dict[str, Any]] = {}
        self.client_queues: Dict[str, asyncio.Queue] = {}

    def register_connection(self, client_id: str, send_func: Callable[[str, Any], Awaitable[None]]):
        """Register a new connection"""
        self.active_connections[client_id] = {
            "connected_at": time.time(),
            "last_activity": time.time(),
            "send_func": send_func
        }

        # Create message queue if it doesn't exist
        if client_id not in self.client_queues:
            self.client_queues[client_id] = asyncio.Queue()

    def unregister_connection(self, client_id: str):
        """Unregister a connection"""
        if client_id in self.active_connections:
            del self.active_connections[client_id]

    async def send_event(self, client_id: str, event: str, data: Any):
        """Send an event to a client"""
        if client_id in self.active_connections:
            self.active_connections[client_id]["last_activity"] = time.time()
            send_func = self.active_connections[client_id]["send_func"]
            try:
                await send_func(event, data)
                return True
            except Exception as e:
                logger.error(f"Error sending event to client {client_id}: {str(e)}")
                self.unregister_connection(client_id)
                return False
        return False

    async def broadcast_event(self, event: str, data: Any):
        """Broadcast an event to all connected clients"""
        for client_id in list(self.active_connections.keys()):
            await self.send_event(client_id, event, data)

    async def add_to_queue(self, client_id: str, message: Dict[str, Any]):
        """Add a message to a client's queue"""
        if client_id not in self.client_queues:
            self.client_queues[client_id] = asyncio.Queue()
        await self.client_queues[client_id].put(message)

    async def get_from_queue(self, client_id: str, timeout: float = None) -> Optional[Dict[str, Any]]:
        """Get a message from a client's queue"""
        if client_id not in self.client_queues:
            return None

        try:
            if timeout is None:
                return await self.client_queues[client_id].get()
            else:
                return await asyncio.wait_for(self.client_queues[client_id].get(), timeout)
        except asyncio.TimeoutError:
            return None

    def is_connected(self, client_id: str) -> bool:
        """Check if a client is connected"""
        return client_id in self.active_connections

    def get_all_connections(self) -> List[str]:
        """Get all active connection IDs"""
        return list(self.active_connections.keys())


# ==================== Application Setup ====================

# Create MongoDB storage instance
storage = MongoDBStorage(MONGODB_URL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown"""
    # Startup - initialize components
    storage = MongoDBStorage(MONGODB_URL)

    try:
        # Initialize the MongoDB connection
        await storage.init()

        # Store components in app state
        app.state.storage = storage
        app.state.mcp_session_manager = MCPSession(storage)
        app.state.connection_manager = ConnectionManager()
        app.state.start_time = time.time()

        # Start background tasks
        app.state.cleanup_task = asyncio.create_task(cleanup_inactive_agents(app))
        app.state.heartbeat_task = asyncio.create_task(send_heartbeats(app))

        logger.info("AIRA Hub started successfully")
        yield

    except Exception as e:
        logger.error(f"Error starting AIRA Hub: {str(e)}")
        # Yield to allow FastAPI to handle the error properly
        yield
        return

    # Shutdown - clean up resources
    logger.info("Shutting down AIRA Hub...")

    # Cancel background tasks
    if hasattr(app.state, 'cleanup_task'):
        app.state.cleanup_task.cancel()
    if hasattr(app.state, 'heartbeat_task'):
        app.state.heartbeat_task.cancel()

    # Wait for tasks to complete cancellation
    try:
        if hasattr(app.state, 'cleanup_task'):
            await app.state.cleanup_task
        if hasattr(app.state, 'heartbeat_task'):
            await app.state.heartbeat_task
    except asyncio.CancelledError:
        pass

    # Close database connection
    if hasattr(app.state, 'storage'):
        await app.state.storage.close()

    logger.info("AIRA Hub stopped successfully")


# ==================== Background Tasks ====================

async def cleanup_inactive_agents(app: FastAPI):
    """Background task to clean up inactive agents"""
    try:
        while True:
            await asyncio.sleep(DEFAULT_CLEANUP_INTERVAL)
            now = time.time()

            # Get all agents
            agents = await app.state.storage.list_agents()
            inactive_count = 0

            # Check for inactive agents
            for agent in agents:
                if (now - agent.last_seen) > DEFAULT_HEARTBEAT_TIMEOUT:
                    if agent.status != AgentStatus.OFFLINE:
                        agent.status = AgentStatus.OFFLINE
                        await app.state.storage.save_agent(agent)
                        inactive_count += 1

                        # Notify connected clients about the status change
                        await app.state.connection_manager.broadcast_event("agent_status", {
                            "agent_id": agent.agent_id,
                            "status": AgentStatus.OFFLINE
                        })

            # Clean up stale MCP sessions
            session_count = app.state.mcp_session_manager.cleanup_stale_sessions()

            if inactive_count > 0 or session_count > 0:
                logger.info(f"Marked {inactive_count} agents as offline, cleaned up {session_count} stale sessions")

    except asyncio.CancelledError:
        logger.info("Cleanup task cancelled")
    except Exception as e:
        logger.error(f"Error in cleanup task: {str(e)}")


async def send_heartbeats(app: FastAPI):
    """Background task to send heartbeats to connected clients"""
    try:
        while True:
            await asyncio.sleep(DEFAULT_HEARTBEAT_INTERVAL)

            # Get active connections
            connections = app.state.connection_manager.get_all_connections()

            # Send heartbeat to each connection
            for client_id in connections:
                await app.state.connection_manager.send_event(
                    client_id,
                    "heartbeat",
                    {"timestamp": time.time()}
                )

    except asyncio.CancelledError:
        logger.info("Heartbeat task cancelled")
    except Exception as e:
        logger.error(f"Error in heartbeat task: {str(e)}")


# ==================== Application Creation ====================

# Create the FastAPI app
app = FastAPI(
    title="AIRA Hub",
    description="A decentralized registry for MCP and A2A tools",
    version="1.0.0",
    lifespan=lifespan
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # All origins allowed for P2P
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.options("/mcp/messages", include_in_schema=False)
async def options_mcp_messages():
    return Response(
        status_code=200,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "86400",
        }
    )

# Request logging middleware
@app.middleware("http")
async def add_cors_headers(request: Request, call_next):
    response = await call_next(request)

    # Add CORS headers to all responses
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"

    # Handle preflight requests
    if request.method == "OPTIONS":
        return Response(
            status_code=200,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
                "Access-Control-Max-Age": "86400",
            }
        )

    return response
async def log_requests(request: Request, call_next):
    """Middleware to log requests"""
    start_time = time.time()
    path = request.url.path
    method = request.method

    # Skip logging for certain paths
    skip_logging = path.endswith("/heartbeat") or path == "/health"

    if not skip_logging:
        logger.debug(f"Request: {method} {path}")

    response = await call_next(request)

    # Log request duration for non-skipped paths
    if not skip_logging:
        duration = time.time() - start_time
        status_code = response.status_code
        logger.debug(f"Response: {method} {path} - {status_code} ({duration:.4f}s)")

    return response


# ==================== API Endpoints ====================

# ----- Agent Registration and Management -----

@app.post("/register", status_code=201, tags=["Agents"])
async def register_agent(
        request: Request,
        agent: AgentRegistration,
        background_tasks: BackgroundTasks
):
    """
    Register an agent with the AIRA hub

    This endpoint allows agents to register themselves with the hub,
    making them discoverable by other agents and clients.
    """
    storage = request.app.state.storage

    # Check if agent with this URL already exists
    existing_agent = await storage.get_agent_by_url(agent.url)
    if existing_agent:
        # Update the existing agent's information but keep its ID
        agent.agent_id = existing_agent.agent_id
        agent.created_at = existing_agent.created_at

        # Preserve metrics if present
        if existing_agent.metrics:
            agent.metrics = existing_agent.metrics

    # Set status and timestamps
    agent.status = AgentStatus.ONLINE
    agent.last_seen = time.time()

    # Initialize metrics if not present
    if not agent.metrics:
        agent.metrics = AgentMetrics(uptime=0)

    # Store the agent
    await storage.save_agent(agent)

    # Notify connected clients about the new agent
    background_tasks.add_task(
        request.app.state.connection_manager.broadcast_event,
        "agent_registered",
        {
            "agent_id": agent.agent_id,
            "name": agent.name,
            "url": agent.url
        }
    )

    logger.info(f"Agent registered/updated: {agent.name} ({agent.url})")

    return {
        "status": "registered",
        "agent_id": agent.agent_id,
        "url": agent.url
    }


@app.post("/heartbeat/{agent_id}", tags=["Agents"])
async def heartbeat(
        request: Request,
        agent_id: str
):
    """
    Update agent heartbeat

    This endpoint allows agents to send heartbeats to indicate they are still active.
    """
    storage = request.app.state.storage

    # Get existing agent
    agent = await storage.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Update heartbeat timestamp
    now = time.time()
    await storage.update_agent_heartbeat(agent_id, now)

    # Update agent status if needed
    status_changed = False
    if agent.status != AgentStatus.ONLINE:
        agent.status = AgentStatus.ONLINE
        await storage.save_agent(agent)
        status_changed = True

    # Notify clients if status changed
    if status_changed:
        await request.app.state.connection_manager.broadcast_event(
            "agent_status",
            {
                "agent_id": agent_id,
                "status": AgentStatus.ONLINE
            }
        )

    return {"status": "ok"}


@app.get("/agents", tags=["Discovery"])
async def list_agents(
        request: Request,
        status: Optional[str] = None,
        category: Optional[str] = None,
        tag: Optional[str] = None,
        capability: Optional[str] = None,
        offset: int = 0,
        limit: int = 100
):
    """
    List registered agents

    This endpoint returns a list of registered agents with optional filtering.
    """
    storage = request.app.state.storage

    # Build query
    query = {}
    if status:
        query["status"] = status
    if category:
        query["category"] = category
    if tag:
        query["tags"] = tag
    if capability:
        query["aira_capabilities"] = capability

    # Get total count
    total = await storage.count_agents(query)

    # Get agents with pagination
    agents = await storage.search_agents(query, offset, limit)

    # Format response
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "agents": agents
    }


@app.get("/agents/{agent_id}", tags=["Discovery"])
async def get_agent(
        request: Request,
        agent_id: str
):
    """
    Get details for a specific agent

    This endpoint returns detailed information about a specific agent.
    """
    storage = request.app.state.storage

    # Get the agent
    agent = await storage.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    return agent


@app.delete("/agents/{agent_id}", tags=["Agents"])
async def unregister_agent(
        request: Request,
        agent_id: str,
        background_tasks: BackgroundTasks
):
    """
    Unregister an agent

    This endpoint allows agents to unregister themselves from the hub.
    """
    storage = request.app.state.storage

    # Get the agent
    agent = await storage.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Delete the agent
    await storage.delete_agent(agent_id)

    # Notify connected clients
    background_tasks.add_task(
        request.app.state.connection_manager.broadcast_event,
        "agent_unregistered",
        {
            "agent_id": agent_id,
            "name": agent.name,
            "url": agent.url
        }
    )

    logger.info(f"Agent unregistered: {agent.name} ({agent.url})")

    return {"status": "unregistered", "agent_id": agent_id}


# ----- Tool Management and Discovery -----

@app.get("/tools", tags=["Tools"])
async def list_tools(
        request: Request,
        agent_id: Optional[str] = None
):
    """
    List available tools

    This endpoint returns a list of all available tools, optionally filtered by agent.
    """
    storage = request.app.state.storage

    if agent_id:
        # Get tools from a specific agent
        agent = await storage.get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")

        tools = []
        for tool in agent.mcp_tools:
            tools.append({
                "name": tool.name,
                "description": tool.description,
                "agent": agent.name,
                "agent_id": agent.agent_id,
                "inputSchema": tool.inputSchema
            })
    else:
        # Get tools from all agents
        tools = await storage.list_tools()

    return {"tools": tools}


@app.get("/tags", tags=["Discovery"])
async def list_tags(
        request: Request,
        type: str = "agent"
):
    """
    List all tags

    This endpoint returns a list of all unique tags used by agents, skills, or tools.
    """
    storage = request.app.state.storage

    # Get all agents
    agents = await storage.list_agents()

    # Collect tags based on type
    tags = set()
    if type == "agent":
        for agent in agents:
            tags.update(agent.tags)
    elif type == "skill":
        for agent in agents:
            for skill in agent.a2a_skills:
                tags.update(skill.tags)
    elif type == "tool":
        for agent in agents:
            for tool in agent.mcp_tools:
                if hasattr(tool, 'tags') and tool.tags:
                    tags.update(tool.tags)
    else:
        raise HTTPException(status_code=400, detail="Invalid tag type. Must be 'agent', 'skill', or 'tool'")

    return {"tags": sorted(list(tags))}


@app.get("/categories", tags=["Discovery"])
async def list_categories(
        request: Request
):
    """
    List all categories

    This endpoint returns a list of all unique categories used by agents.
    """
    storage = request.app.state.storage

    # Get all agents
    agents = await storage.list_agents()

    # Collect categories
    categories = set()
    for agent in agents:
        if agent.category:
            categories.add(agent.category)

    return {"categories": sorted(list(categories))}


# ----- Status and Health -----

@app.get("/status", tags=["System"])
async def system_status(
        request: Request
):
    """
    Get system status

    This endpoint returns the current status of the AIRA hub.
    """
    storage = request.app.state.storage

    # Get all agents
    agents = await storage.list_agents()

    # Count online agents
    now = time.time()
    online_agents = [a for a in agents if (now - a.last_seen) <= DEFAULT_HEARTBEAT_TIMEOUT]

    # Count tools
    tools = await storage.list_tools()

    return {
        "status": "healthy",
        "uptime": time.time() - request.app.state.start_time,
        "registered_agents": len(agents),
        "online_agents": len(online_agents),
        "available_tools": len(tools),
        "connected_clients": len(request.app.state.connection_manager.get_all_connections()),
        "active_sessions": len(request.app.state.mcp_session_manager.active_sessions),
        "version": "1.0.0"
    }


@app.get("/health", include_in_schema=False)
async def health_check():
    """
    Health check endpoint

    This endpoint returns a simple health check for load balancers and monitoring.
    """
    return {"status": "ok"}


# ----- MCP Protocol Endpoints -----

@app.get("/mcp/sse", tags=["MCP"])
async def mcp_sse_endpoint(request: Request):
    """
    Server-Sent Events endpoint for MCP

    This endpoint establishes an SSE connection for MCP clients.
    It supports the MCP protocol for LLM clients like Claude.
    """
    client_id = str(uuid.uuid4())
    session_id = request.app.state.mcp_session_manager.create_session(client_id)

    logger.info(f"Created new session {session_id} for client {client_id}")

    async def event_generator():
        # First, send the endpoint event with the messages URL
        # This is required by the MCP protocol
        yield f"event: endpoint\ndata: /mcp/messages\n\n"

        # Register this connection
        async def send_event(event_name, data):
            json_data = json.dumps(data)
            return f"event: {event_name}\ndata: {json_data}\n\n"

        request.app.state.connection_manager.register_connection(client_id, send_event)

        try:
            # Send the initial set of tools
            tools = await request.app.state.storage.list_tools()
            mcp_tools = []

            for tool in tools:
                mcp_tools.append({
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "inputSchema": tool.get("inputSchema", {})
                })

            yield await send_event("tools", {"tools": mcp_tools})

            # Then keep the connection alive and process messages
            while True:
                # Send heartbeat every 30 seconds
                for _ in range(30):
                    # Check for messages in the client's queue
                    message = await request.app.state.connection_manager.get_from_queue(client_id, timeout=1.0)

                    if message:
                        # Process and send the message
                        event_type = message.get("event", "message")
                        yield await send_event(event_type, message.get("data", {}))

                    # Check if client is still connected
                    if not request.app.state.connection_manager.is_connected(client_id):
                        logger.info(f"Client {client_id} disconnected")
                        return

                # Send heartbeat
                yield await send_event("heartbeat", {"timestamp": time.time()})

                # Update session activity
                request.app.state.mcp_session_manager.update_session_activity(session_id)

        except Exception as e:
            logger.error(f"Error in SSE connection: {str(e)}")
        finally:
            # Clean up when the connection is closed
            request.app.state.connection_manager.unregister_connection(client_id)
            logger.info(f"SSE connection closed for client {client_id}")

    return EventSourceResponse(event_generator())


@app.post("/mcp/messages", tags=["MCP"])
async def mcp_messages_endpoint(request: Request):
    logger.info(f"Request to /mcp/messages: method={request.method}, content-type={request.headers.get('content-type')}")
    logger.info(f"Query params: {request.query_params}")
    try:
        # Read request body as bytes first
        body_bytes = await request.body()

        # Log for debugging
        logger.debug(f"Request body received: {body_bytes!r}")

        if not body_bytes:
            return JSONResponse(
                status_code=400,
                content={"jsonrpc": "2.0", "error": {"code": -32600, "message": "Empty request body"}}
            )

        # Try to parse JSON
        try:
            body = json.loads(body_bytes)
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error: {str(e)}, Body: {body_bytes!r}")
            return JSONResponse(
                status_code=400,
                content={"jsonrpc": "2.0", "error": {"code": -32700, "message": f"Parse error: {str(e)}"}}
            )

        # Get session ID from query parameters
        session_id = request.query_params.get("sessionId")
        if not session_id:
            return JSONResponse(
                status_code=400,
                content={"jsonrpc": "2.0", "error": {"code": -32602, "message": "Missing sessionId parameter"}}
            )

        # Get session
        session = request.app.state.mcp_session_manager.get_session(session_id)
        if not session:
            return JSONResponse(
                status_code=404,
                content={"jsonrpc": "2.0", "error": {"code": -32001, "message": "Session not found"}}
            )

        # Update session activity
        request.app.state.mcp_session_manager.update_session_activity(session_id)

        # Process the request
        if isinstance(body, dict):
            return await process_mcp_request(request, body, session)
        elif isinstance(body, list):
            # Handle batched requests
            responses = []
            for req in body:
                resp = await process_mcp_request(request, req, session)
                responses.append(resp)
            return responses
        else:
            return JSONResponse(
                status_code=400,
                content={"jsonrpc": "2.0", "error": {"code": -32600, "message": "Invalid request format"}}
            )

    except Exception as e:
        logger.error(f"Error processing MCP message: {str(e)}")
        return JSONResponse(
            status_code=500,
            content={"jsonrpc": "2.0", "error": {"code": -32603, "message": f"Internal error: {str(e)}"}}
        )


async def process_mcp_request(request: Request, body: dict, session: dict):
    """Process an individual MCP request"""

    # Extract request details
    method = body.get("method")
    request_id = body.get("id")
    params = body.get("params", {})

    if not method:
        return JSONResponse(
            content={"jsonrpc": "2.0", "id": request_id, "error": {"code": -32600, "message": "Method is required"}}
        )

    # Handle different methods
    if method == "initialize":
        # Handle initialization request
        return handle_initialize(request_id, params)

    elif method == "notifications/initialized":
        # Handle initialized notification
        return JSONResponse(status_code=202, content={})

    elif method == "tools/list":
        # List available tools
        return await handle_tools_list(request, request_id)

    elif method == "tools/call":
        # Call a tool
        return await handle_tools_call(request, request_id, params)

    elif method == "resources/list":
        # List available resources (not implemented yet)
        return JSONResponse(
            content={"jsonrpc": "2.0", "id": request_id, "result": {"resources": []}}
        )

    elif method == "prompts/list":
        # List available prompts (not implemented yet)
        return JSONResponse(
            content={"jsonrpc": "2.0", "id": request_id, "result": {"prompts": []}}
        )

    else:
        # Unknown method
        return JSONResponse(
            content={"jsonrpc": "2.0", "id": request_id,
                     "error": {"code": -32601, "message": f"Method not found: {method}"}}
        )


def handle_initialize(request_id, params):
    """Handle MCP initialize request"""

    # Extract client info
    protocol_version = params.get("protocolVersion", "2024-11-05")
    client_capabilities = params.get("capabilities", {})
    client_info = params.get("clientInfo", {})

    logger.info(f"Client initialized: {client_info.get('name', 'Unknown')} {client_info.get('version', '')}")

    # Return server capabilities
    return JSONResponse(content={
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "protocolVersion": protocol_version,
            "capabilities": {
                "tools": {
                    "listChanged": True
                },
                "logging": {}
            },
            "serverInfo": {
                "name": "AIRA Hub",
                "version": "1.0.0"
            }
        }
    })


async def handle_tools_list(request, request_id):
    """Handle MCP tools/list request"""

    # Get all tools from all agents
    tools_data = await request.app.state.storage.list_tools()

    # Convert to MCP format
    mcp_tools = []
    for tool in tools_data:
        mcp_tools.append({
            "name": tool["name"],
            "description": tool.get("description", ""),
            "inputSchema": tool.get("inputSchema", {})
        })

    return JSONResponse(content={
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "tools": mcp_tools
        }
    })


async def handle_tools_call(request, request_id, params):
    """Handle MCP tools/call request"""

    # Extract tool name and arguments
    tool_name = params.get("name")
    arguments = params.get("arguments", {})

    if not tool_name:
        return JSONResponse(content={
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32602, "message": "Tool name is required"}
        })

    # Find the agent that provides this tool
    agent = await request.app.state.storage.get_agent_by_tool(tool_name)

    if not agent:
        return JSONResponse(content={
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32602, "message": f"Tool not found: {tool_name}"}
        })

    # Check if agent is online
    if agent.status != AgentStatus.ONLINE:
        return JSONResponse(content={
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32602, "message": f"Agent is offline: {agent.name}"}
        })

    # Verify that the agent has an MCP endpoint URL
    if not agent.mcp_url:
        return JSONResponse(content={
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32602, "message": f"Agent does not support MCP: {agent.name}"}
        })

    try:
        # Forward the request to the agent
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Create MCP request
            mcp_request = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": arguments
                }
            }

            # Send request to agent
            response = await client.post(
                agent.mcp_url,
                json=mcp_request,
                headers={"Content-Type": "application/json"}
            )

            # Check response
            if response.status_code != 200:
                return JSONResponse(content={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32603,
                        "message": f"Agent returned error: {response.status_code}"
                    }
                })

            # Return the agent's response
            return JSONResponse(content=response.json())

    except Exception as e:
        logger.error(f"Error calling tool {tool_name} from agent {agent.name}: {str(e)}")
        return JSONResponse(content={
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32603,
                "message": f"Error calling tool: {str(e)}"
            }
        })


@app.post("/connect/stream", tags=["MCP"])
async def connect_stream_endpoint(
        request: Request,
        agent_url: str,
        name: str,
        aira_capabilities: Optional[str] = None
):
    """
    Connect to an agent's SSE stream

    This endpoint establishes an SSE connection to an agent and forwards events.
    """
    # Parse capabilities
    capabilities = []
    if aira_capabilities:
        capabilities = aira_capabilities.split(",")

    # Create a unique ID for this connection
    connection_id = str(uuid.uuid4())

    async def event_generator():
        # Set up httpx client to connect to the agent
        async with httpx.AsyncClient() as client:
            try:
                # Open SSE connection to agent
                async with client.stream("GET", agent_url, headers={"Accept": "text/event-stream"}) as response:
                    # Check response
                    if response.status_code != 200:
                        yield f"event: error\ndata: {json.dumps({'error': f'Agent returned error: {response.status_code}'})}\n\n"
                        return

                    # Register agent if not already registered
                    existing_agent = await request.app.state.storage.get_agent_by_url(agent_url)

                    if not existing_agent:
                        # Create new agent registration
                        agent = AgentRegistration(
                            url=agent_url,
                            name=name,
                            aira_capabilities=capabilities,
                            mcp_sse_url=agent_url
                        )
                        await request.app.state.storage.save_agent(agent)
                        agent_id = agent.agent_id
                    else:
                        agent_id = existing_agent.agent_id

                    # Update heartbeat
                    await request.app.state.storage.update_agent_heartbeat(agent_id, time.time())

                    # Forward SSE events
                    async for line in response.aiter_lines():
                        yield line + "\n"

                        # Extract endpoint data for parsing
                        if line.startswith("event: endpoint") and "data:" in line:
                            # Next line contains endpoint data
                            async for data_line in response.aiter_lines():
                                if data_line.startswith("data:"):
                                    endpoint_url = data_line.replace("data:", "").strip()

                                    # Update agent with MCP URL
                                    agent = await request.app.state.storage.get_agent(agent_id)
                                    if agent:
                                        # Check if URL is relative or absolute
                                        if endpoint_url.startswith("http"):
                                            agent.mcp_url = endpoint_url
                                        else:
                                            # Construct absolute URL
                                            base_url = str(agent_url)
                                            if base_url.endswith("/"):
                                                base_url = base_url[:-1]

                                            if endpoint_url.startswith("/"):
                                                agent.mcp_url = base_url + endpoint_url
                                            else:
                                                agent.mcp_url = base_url + "/" + endpoint_url

                                        await request.app.state.storage.save_agent(agent)

                                    yield data_line + "\n"
                                    break

            except Exception as e:
                logger.error(f"Error in SSE connection to agent {agent_url}: {str(e)}")
                yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

    return EventSourceResponse(event_generator())


@app.post("/connect/stream/init", tags=["MCP"])
async def connect_stream_init(
        request: Request,
        background_tasks: BackgroundTasks
):
    """
    Initialize agent stream connection

    This endpoint is used by agents to initialize their connection with the hub
    and provide details about their tools and capabilities.
    """
    try:
        # Parse request body
        data = await request.json()

        # Extract fields
        url = data.get("url")
        mcp_tools = data.get("mcp_tools", [])
        mcp_capabilities = data.get("mcp_capabilities", {})

        if not url:
            return JSONResponse(status_code=400, content={"error": "URL is required"})

        # Get existing agent or create new one
        agent = await request.app.state.storage.get_agent_by_url(url)

        if not agent:
            # Create new agent
            agent = AgentRegistration(
                url=url,
                name=url.split("//")[-1].split("/")[0],  # Extract domain as name
                aira_capabilities=["mcp"],
                mcp_url=url
            )

        # Update tools
        agent.mcp_tools = [MCPTool(**tool) for tool in mcp_tools]

        # Save agent
        await request.app.state.storage.save_agent(agent)

        # Notify clients about tool updates
        background_tasks.add_task(
            request.app.state.connection_manager.broadcast_event,
            "tools_updated",
            {
                "agent_id": agent.agent_id,
                "tools": mcp_tools
            }
        )

        return JSONResponse(content={"status": "ok", "agent_id": agent.agent_id})

    except Exception as e:
        logger.error(f"Error in stream init: {str(e)}")
        return JSONResponse(status_code=500, content={"error": str(e)})


# ----- A2A Protocol Endpoints -----

@app.post("/a2a/discover", tags=["A2A"])
async def discover_agents(
        request: Request,
        query: DiscoverQuery
):
    """
    Discover agents based on criteria using A2A protocol

    This endpoint allows clients to discover agents based on various criteria.
    """
    storage = request.app.state.storage

    # Build query
    query_dict = {}

    if query.status:
        query_dict["status"] = query.status

    if query.category:
        query_dict["category"] = query.category

    if query.agent_tags:
        query_dict["tags"] = {"$in": query.agent_tags}

    if query.capabilities:
        query_dict["aira_capabilities"] = {"$in": query.capabilities}

    # Get total count
    total = await storage.count_agents(query_dict)

    # Get agents with pagination
    agents = await storage.search_agents(query_dict, query.offset, query.limit)

    # Filter by skill criteria if needed
    if query.skill_id or query.skill_tags:
        filtered_agents = []
        for agent in agents:
            if query.skill_id and any(s.id == query.skill_id for s in agent.a2a_skills):
                filtered_agents.append(agent)
                continue

            if query.skill_tags and any(
                    any(tag in s.tags for tag in query.skill_tags)
                    for s in agent.a2a_skills
            ):
                filtered_agents.append(agent)
                continue

        agents = filtered_agents

    # Format response
    return {
        "total": total,
        "offset": query.offset,
        "limit": query.limit,
        "agents": agents
    }


@app.get("/a2a/skills", tags=["A2A"])
async def list_skills(
        request: Request,
        agent_id: Optional[str] = None,
        tag: Optional[str] = None
):
    """
    List available A2A skills

    This endpoint returns a list of all available A2A skills, optionally filtered by agent or tag.
    """
    storage = request.app.state.storage

    skills = []

    if agent_id:
        # Get skills from a specific agent
        agent = await storage.get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")

        for skill in agent.a2a_skills:
            skills.append({
                "id": skill.id,
                "name": skill.name,
                "description": skill.description,
                "agent": agent.name,
                "agent_id": agent.agent_id,
                "tags": skill.tags,
                "parameters": skill.parameters
            })
    else:
        # Get skills from all agents
        agents = await storage.list_agents()

        for agent in agents:
            for skill in agent.a2a_skills:
                skills.append({
                    "id": skill.id,
                    "name": skill.name,
                    "description": skill.description,
                    "agent": agent.name,
                    "agent_id": agent.agent_id,
                    "tags": skill.tags,
                    "parameters": skill.parameters
                })

    # Filter by tag if provided
    if tag:
        skills = [s for s in skills if tag in s.get("tags", [])]

    return {"skills": skills}


@app.post("/a2a/tasks/send", tags=["A2A"])
async def a2a_send_task(request: Request):
    """
    Send a task to an agent using A2A protocol

    This endpoint allows clients to send tasks to agents using the A2A protocol.
    """
    try:
        # Parse request body
        data = await request.json()

        agent_id = data.get("agent_id")
        skill_id = data.get("skill_id")
        task_content = data.get("content")

        if not agent_id:
            return JSONResponse(status_code=400, content={"error": "agent_id is required"})

        if not skill_id:
            return JSONResponse(status_code=400, content={"error": "skill_id is required"})

        if not task_content:
            return JSONResponse(status_code=400, content={"error": "content is required"})

        # Get the agent
        agent = await request.app.state.storage.get_agent(agent_id)
        if not agent:
            return JSONResponse(status_code=404, content={"error": "Agent not found"})

        # Check if agent is online
        if agent.status != AgentStatus.ONLINE:
            return JSONResponse(status_code=400, content={"error": f"Agent is offline: {agent.name}"})

        # Check if agent has A2A capability
        if "a2a" not in agent.aira_capabilities:
            return JSONResponse(status_code=400, content={"error": f"Agent does not support A2A: {agent.name}"})

        # Verify the skill exists
        skill_exists = any(skill.id == skill_id for skill in agent.a2a_skills)
        if not skill_exists:
            return JSONResponse(status_code=404, content={"error": f"Skill not found: {skill_id}"})

        # Create a task ID
        task_id = str(uuid.uuid4())

        # We're proxying the task here - in a real implementation, we would
        # forward the request to the agent's A2A endpoint
        # For now, we'll just return a mock response

        return JSONResponse(content={
            "task_id": task_id,
            "status": "submitted",
            "message": f"Task sent to agent {agent.name} for skill {skill_id}"
        })

    except Exception as e:
        logger.error(f"Error sending A2A task: {str(e)}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/a2a/tasks/{task_id}", tags=["A2A"])
async def a2a_get_task(
        request: Request,
        task_id: str
):
    """
    Get A2A task status

    This endpoint allows clients to check the status of an A2A task.
    """
    # In a real implementation, we would look up the task in a database
    # and return its current status. For now, we'll just return a mock response.

    return JSONResponse(content={
        "task_id": task_id,
        "status": "completed",
        "result": {
            "type": "text",
            "text": "Task completed successfully"
        }
    })


# ----- Analytics Endpoints -----

@app.get("/analytics/summary", tags=["Analytics"])
async def analytics_summary(request: Request):
    """
    Get analytics summary

    This endpoint returns a summary of AIRA hub analytics.
    """
    storage = request.app.state.storage

    # Get all agents
    agents = await storage.list_agents()

    # Count agents by status
    status_counts = {}
    for status in AgentStatus:
        status_counts[status] = len([a for a in agents if a.status == status])

    # Count agents by capability
    capability_counts = {}
    for agent in agents:
        for cap in agent.aira_capabilities:
            capability_counts[cap] = capability_counts.get(cap, 0) + 1

    # Count tools and skills
    total_tools = sum(len(agent.mcp_tools) for agent in agents)
    total_skills = sum(len(agent.a2a_skills) for agent in agents)

    # Get top tags
    tags = {}
    for agent in agents:
        for tag in agent.tags:
            tags[tag] = tags.get(tag, 0) + 1

    # Sort and limit to top 10
    top_tags = sorted(tags.items(), key=lambda x: x[1], reverse=True)[:10]

    return {
        "total_agents": len(agents),
        "status_counts": status_counts,
        "capability_counts": capability_counts,
        "total_tools": total_tools,
        "total_skills": total_skills,
        "top_tags": dict(top_tags),
        "agents_added_today": len([a for a in agents if (time.time() - a.created_at) < 86400])
    }


@app.get("/analytics/activity", tags=["Analytics"])
async def analytics_activity(
        request: Request,
        days: int = 7
):
    """
    Get activity analytics

    This endpoint returns activity data for the specified number of days.
    """
    # This would typically query from a database of activity logs
    # For now, we'll just return mock data

    now = time.time()
    day_seconds = 86400

    activity_data = []
    for i in range(days):
        day_timestamp = now - (i * day_seconds)
        day_date = datetime.fromtimestamp(day_timestamp).strftime("%Y-%m-%d")

        activity_data.append({
            "date": day_date,
            "registrations": int(10 * (1 + 0.5 * (days - i) / days)),
            "tool_calls": int(100 * (1 + 0.3 * (days - i) / days)),
            "active_agents": int(20 * (1 + 0.1 * (days - i) / days))
        })

    # Reverse to get chronological order
    activity_data.reverse()

    return {"activity": activity_data}


# ----- Admin Endpoints -----

@app.post("/admin/sync_agents", tags=["Admin"])
async def sync_agents(request: Request):
    """
    Sync agents with remote registries

    This endpoint allows administrators to sync agents with other AIRA hubs.
    """
    try:
        # Parse request body
        data = await request.json()

        hub_urls = data.get("hub_urls", [])
        if not hub_urls:
            return JSONResponse(status_code=400, content={"error": "hub_urls is required"})

        results = {}

        # Sync with each hub
        for hub_url in hub_urls:
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    # Get agents from remote hub
                    response = await client.get(f"{hub_url}/agents")

                    if response.status_code != 200:
                        results[hub_url] = {
                            "status": "error",
                            "message": f"Failed to get agents: {response.status_code}"
                        }
                        continue

                    remote_agents = response.json().get("agents", [])

                    # Register each agent locally
                    registered = 0
                    for agent_data in remote_agents:
                        try:
                            # Create agent registration
                            agent = AgentRegistration(**agent_data)

                            # Check if agent already exists
                            existing_agent = await request.app.state.storage.get_agent_by_url(agent.url)
                            if existing_agent:
                                # Skip if already registered
                                continue

                            # Save agent
                            await request.app.state.storage.save_agent(agent)
                            registered += 1
                        except Exception as e:
                            logger.error(f"Error registering agent {agent_data.get('name')}: {str(e)}")

                    results[hub_url] = {
                        "status": "success",
                        "registered": registered,
                        "total": len(remote_agents)
                    }

            except Exception as e:
                results[hub_url] = {
                    "status": "error",
                    "message": str(e)
                }

        return {"results": results}

    except Exception as e:
        logger.error(f"Error syncing agents: {str(e)}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/admin/cleanup", tags=["Admin"])
async def admin_cleanup(request: Request):
    """
    Clean up stale data

    This endpoint allows administrators to clean up stale agents and sessions.
    """
    try:
        # Parse request body
        data = await request.json()

        agent_threshold = data.get("agent_threshold", DEFAULT_HEARTBEAT_TIMEOUT * 3)  # Default: 3x normal timeout
        session_threshold = data.get("session_threshold", 3600)  # Default: 1 hour

        # Clean up stale agents
        storage = request.app.state.storage
        now = time.time()

        agents = await storage.list_agents()
        agent_count = 0

        for agent in agents:
            if (now - agent.last_seen) > agent_threshold:
                await storage.delete_agent(agent.agent_id)
                agent_count += 1

        # Clean up stale sessions
        session_count = request.app.state.mcp_session_manager.cleanup_stale_sessions(session_threshold)

        return {
            "status": "success",
            "agents_removed": agent_count,
            "sessions_removed": session_count
        }

    except Exception as e:
        logger.error(f"Error in admin cleanup: {str(e)}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/admin/broadcast", tags=["Admin"])
async def admin_broadcast(request: Request):
    """
    Broadcast a message to all connected clients

    This endpoint allows administrators to broadcast a message to all clients.
    """
    try:
        # Parse request body
        data = await request.json()

        message = data.get("message")
        event_type = data.get("event_type", "admin_message")

        if not message:
            return JSONResponse(status_code=400, content={"error": "message is required"})

        # Broadcast message
        await request.app.state.connection_manager.broadcast_event(
            event_type,
            {
                "message": message,
                "timestamp": time.time()
            }
        )

        return {"status": "success", "recipients": len(request.app.state.connection_manager.get_all_connections())}

    except Exception as e:
        logger.error(f"Error in admin broadcast: {str(e)}")
        return JSONResponse(status_code=500, content={"error": str(e)})


# ----- Web UI Endpoints -----

@app.get("/ui", tags=["UI"])
async def ui_dashboard(request: Request):
    """
    AIRA Hub Dashboard UI

    This endpoint renders the main dashboard UI for AIRA Hub.
    """
    # In a real implementation, we would render an HTML page here
    # For now, just redirect to documentation
    return JSONResponse(content={
        "message": "UI not implemented yet, try /docs for API documentation"
    })


# ----- Main Entry Point -----

if __name__ == "__main__":
    import uvicorn

    # Get port from environment or use default
    port = int(os.environ.get("PORT", 8015))

    # Run the application
    uvicorn.run(
        "aira_hub:app",
        host="0.0.0.0",
        port=port,
        reload=os.environ.get("DEBUG", "false").lower() == "true"
    )
