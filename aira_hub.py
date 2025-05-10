"""
AIRA Hub: A Decentralized Registry for MCP and A2A Tools

This implementation provides:
- Peer-to-peer agent discovery and registration
- MCP tools proxy and aggregation (via Streamable HTTP)
- MongoDB integration for persistent storage
- Support for both MCP and A2A protocols
"""

import asyncio
import json
import logging
import os
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Any, Union, Callable, Awaitable, AsyncGenerator, Literal, cast, TypeVar, \
    Generic
from enum import Enum
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException, Depends, status, Query
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator, model_validator, ConfigDict, ValidationError
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from bson import ObjectId
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.DEBUG if os.environ.get("DEBUG", "false").lower() == "true" else logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - [%(funcName)s:%(lineno)d] %(message)s',
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
HUB_TIMEOUT = 10   # Seconds for registering with AIRA Hub
MAX_TOOLS_PER_AGENT = 100
STREAM_SEPARATOR = b'\r\n'
MCP_TIMEOUT = 300.0 #
# MongoDB configuration
MONGODB_URL = os.getenv("MONGODB_URL")
if not MONGODB_URL:
    logger.critical("MONGODB_URL environment variable is not set. AIRA Hub cannot start.")
    exit(1)


# ----- PYDANTIC MODELS -----

class AgentStatus(str, Enum):
    """Status of an agent registered with AIRA Hub"""
    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"

class MCPError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"MCP Error {code}: {message}")


class ResourceType(str, Enum):
    """Types of resources that can be registered with AIRA Hub"""
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
    avg_response_time: float = 0.0
    uptime: float = 0.0
    last_updated: float = Field(default_factory=time.time)


class AgentRegistration(BaseModel):
    """Agent registration information"""
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "url": "https://weather-agent.example.com",
                "name": "Weather Agent",
                "description": "Provides weather forecasts",
                "mcp_tools": [{"name": "get_weather", "description": "Get current weather",
                               "inputSchema": {"type": "object", "properties": {"location": {"type": "string"}},
                                               "required": ["location"]}}],
                "aira_capabilities": ["mcp", "a2a"],
                "tags": ["weather"], "category": "utilities",
                "mcp_stream_url": "https://weather-agent.example.com/mcp"  # Agent's streamable endpoint
            }
        }
    )
    url: str
    name: str
    description: Optional[str] = None
    version: str = "1.0.0"
    mcp_tools: List[MCPTool] = []
    a2a_skills: List[A2ASkill] = []
    aira_capabilities: List[str] = []
    status: AgentStatus = AgentStatus.ONLINE
    last_seen: float = Field(default_factory=time.time)
    created_at: float = Field(default_factory=time.time)
    metrics: Optional[AgentMetrics] = None
    tags: List[str] = []
    category: Optional[str] = None
    provider: Optional[Dict[str, str]] = None
    mcp_url: Optional[str] = None  # For MCP HTTP/POST endpoint
    mcp_sse_url: Optional[str] = None  # For older SSE transport
    mcp_stream_url: Optional[str] = None  # For Streamable HTTP (preferred)
    stdio_command: Optional[List[str]] = None
    agent_id: str = Field(default_factory=lambda: str(uuid.uuid4()))

    @field_validator('url')
    @classmethod
    def url_must_be_valid(cls, v: str) -> str:
        # Allow http, https, OR our local convention
        if not v.startswith(('http://', 'https://', 'local://')):  # <-- ALLOW local:// again
            raise ValueError('URL must start with http://, https://, or local://')
        # Basic parsing check might still be useful, adapt as needed
        # For local://, netloc might be empty, path is key
        try:
            parsed = urllib.parse.urlparse(v)
            if not parsed.scheme:
                raise ValueError("URL must have a scheme")
            # Add specific checks if needed for local:// format
        except Exception as e:
            raise ValueError(f"URL parsing failed: {e}") from e
        return v

    @field_validator('mcp_tools')
    @classmethod
    def max_tools_check(cls, v: List[MCPTool]) -> List[MCPTool]:
        if len(v) > MAX_TOOLS_PER_AGENT:
            raise ValueError(f'Maximum of {MAX_TOOLS_PER_AGENT} tools allowed')
        return v

    @field_validator('mcp_url', 'mcp_sse_url', 'mcp_stream_url', mode='before')
    @classmethod
    def validate_mcp_url_format(cls, v: Optional[str]) -> Optional[str]:
        if v:
            if not v.startswith(('http://', 'https://')):
                raise ValueError('MCP URL must start with http:// or https://')
        return v


class DiscoverQuery(BaseModel):
    """Query parameters for agent discovery"""
    skill_id: Optional[str] = None
    skill_tags: Optional[List[str]] = None
    tool_name: Optional[str] = None
    agent_tags: Optional[List[str]] = None
    category: Optional[str] = None
    status: Optional[AgentStatus] = None
    capabilities: Optional[List[str]] = None
    offset: int = Field(0, ge=0)
    limit: int = Field(100, ge=1, le=1000)


class MCPRequestModel(BaseModel):
    """MCP JSON-RPC request model"""
    jsonrpc: Literal["2.0"] = "2.0"
    id: Optional[Union[str, int]] = None
    method: str
    params: Optional[Union[Dict[str, Any], List[Any]]] = None


class MCPResponseModel(BaseModel):
    """MCP JSON-RPC response model"""
    jsonrpc: Literal["2.0"] = "2.0"
    id: Optional[Union[str, int]] = None
    result: Optional[Any] = None
    error: Optional[Dict[str, Any]] = None


class A2ATaskState(str, Enum):
    """Possible states for an A2A task"""
    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class A2ATaskStatusUpdate(BaseModel):
    """Status update for an A2A task"""
    state: A2ATaskState
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    message: Optional[str] = None


class A2ATask(BaseModel):
    """Representation of an A2A task"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent_id: str
    skill_id: str
    session_id: Optional[str] = None
    original_message: Dict[str, Any]
    current_status: A2ATaskStatusUpdate
    history: List[Dict[str, Any]] = []
    artifacts: List[Dict[str, Any]] = []
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    model_config = ConfigDict(use_enum_values=True)


# ----- DATABASE ACCESS LAYER -----

class MongoDBStorage:
    """MongoDB-based storage backend for AIRA Hub"""

    def __init__(self, connection_string: str):
        """Initialize the MongoDB connection"""
        self.connection_string = connection_string
        self.mongo_db_client: Optional[AsyncIOMotorClient] = None
        self.db: Optional[AsyncIOMotorDatabase] = None
        self.tool_cache: Dict[str, str] = {}  # Maps tool_name -> agent_id for fast lookups

    async def init(self) -> None:
        """Initialize the MongoDB connection and create indexes"""
        if not self.connection_string:
            logger.error("MONGODB_URL is not set. Persistence is disabled.")
            raise ValueError("MONGODB_URL environment variable not set")

        try:
            logger.info(f"Attempting to connect to MongoDB...")
            self.mongo_db_client = AsyncIOMotorClient(self.connection_string, serverSelectionTimeoutMS=5000)
            await self.mongo_db_client.admin.command('ping')
            logger.info("MongoDB ping successful.")

            self.db = self.mongo_db_client.aira_hub
            if self.db is None:
                raise ConnectionError("Failed to get database object 'aira_hub'")

            # Create indexes in parallel for efficiency
            index_futures = [
                # Agent indexes
                self.db.agents.create_index("url", unique=True),
                self.db.agents.create_index("agent_id", unique=True),
                self.db.agents.create_index("last_seen"),
                self.db.agents.create_index("status"),
                self.db.agents.create_index("tags"),
                self.db.agents.create_index("aira_capabilities"),
                self.db.agents.create_index("category"),
                self.db.agents.create_index("mcp_tools.name"),

                # A2A task indexes
                self.db.tasks.create_index("agent_id"),
                self.db.tasks.create_index("skill_id"),
                self.db.tasks.create_index("current_status.state"),
                self.db.tasks.create_index("created_at"),
            ]
            await asyncio.gather(*index_futures)
            logger.info("MongoDB indexes created successfully")

            # Initialize tool cache for fast lookups
            await self.refresh_tool_cache()
            logger.info(f"Connected to MongoDB database: aira_hub")
        except Exception as e:
            logger.error(f"Error connecting/initializing MongoDB: {str(e)}", exc_info=True)
            self.db = None
            self.mongo_db_client = None
            raise e

    async def close(self) -> None:
        """Close the MongoDB connection"""
        if self.mongo_db_client:
            self.mongo_db_client.close()
            logger.info("MongoDB connection closed")
            self.mongo_db_client = None
            self.db = None

    async def refresh_tool_cache(self) -> None:
        """Refresh the in-memory tool cache from the database"""
        self.tool_cache = {}
        if self.db is None:
            logger.warning("Database not initialized, cannot refresh tool cache.")
            return

        try:
            cursor = self.db.agents.find(
                {"status": AgentStatus.ONLINE.value, "mcp_tools": {"$exists": True, "$ne": []}},
                {"agent_id": 1, "mcp_tools.name": 1}
            )

            async for agent_doc in cursor:
                agent_id_str = str(agent_doc.get("agent_id"))
                if not agent_id_str:
                    continue

                for tool in agent_doc.get("mcp_tools", []):
                    tool_name = tool.get("name")
                    if tool_name:
                        self.tool_cache[tool_name] = agent_id_str

            logger.info(f"Tool cache refreshed: {len(self.tool_cache)} tools")
        except Exception as e:
            logger.error(f"Error refreshing tool cache: {e}", exc_info=True)

    async def save_agent(self, agent: AgentRegistration) -> bool:
        """Save or update an agent in the database"""
        if self.db is None:
            logger.error("Database not initialized, cannot save agent.")
            return False

        agent_dict = agent.model_dump(exclude_unset=True)
        agent_id_str = str(agent.agent_id)

        # Update tool cache
        # First remove any tools from this agent that might be in the cache
        tools_to_remove = [k for k, v in self.tool_cache.items() if v == agent_id_str]
        for tool_name in tools_to_remove:
            if tool_name in self.tool_cache:
                del self.tool_cache[tool_name]

        # Add current tools if agent is online
        if agent.status == AgentStatus.ONLINE:
            for tool in agent.mcp_tools:
                if hasattr(tool, 'name') and tool.name:
                    self.tool_cache[tool.name] = agent_id_str

        try:
            result = await self.db.agents.update_one(
                {"agent_id": agent_id_str},
                {"$set": agent_dict},
                upsert=True
            )

            if result.upserted_id:
                logger.info(f"New agent created: {agent.name} ({agent_id_str})")
            elif result.modified_count > 0:
                logger.info(f"Agent updated: {agent.name} ({agent_id_str})")

            return True
        except Exception as e:
            logger.error(f"Error saving agent {agent.name}: {e}", exc_info=True)
            return False

    async def get_agent(self, agent_id: str) -> Optional[AgentRegistration]:
        """Retrieve an agent by ID"""
        if self.db is None:
            return None

        try:
            agent_dict = await self.db.agents.find_one({"agent_id": agent_id})
            if agent_dict:
                agent_dict.pop("_id", None)  # Remove MongoDB _id field
                return AgentRegistration(**agent_dict)
            return None
        except Exception as e:
            logger.error(f"Error getting agent {agent_id}: {e}", exc_info=True)
            return None

    async def get_agent_by_url(self, url: str) -> Optional[AgentRegistration]:
        """Retrieve an agent by URL"""
        if self.db is None:
            return None

        try:
            agent_dict = await self.db.agents.find_one({"url": url})
            if agent_dict:
                agent_dict.pop("_id", None)
                return AgentRegistration(**agent_dict)
            return None
        except Exception as e:
            logger.error(f"Error getting agent by URL {url}: {e}", exc_info=True)
            return None

    async def get_agent_by_tool(self, tool_name: str) -> Optional[AgentRegistration]:
        """Get an agent that provides a specific tool"""
        if self.db is None:
            return None

        try:
            # Check cache first for fast lookup
            agent_id_from_cache = self.tool_cache.get(tool_name)
            if agent_id_from_cache:
                agent = await self.get_agent(agent_id_from_cache)
                if agent and agent.status == AgentStatus.ONLINE:
                    return agent
                else:
                    # Remove stale cache entry
                    if tool_name in self.tool_cache:
                        del self.tool_cache[tool_name]
                    logger.debug(f"Removed stale cache entry for tool: {tool_name}")

            # Query the database if not in cache or cache is stale
            agent_dict = await self.db.agents.find_one(
                {"mcp_tools.name": tool_name, "status": AgentStatus.ONLINE.value}
            )

            if agent_dict:
                agent_dict.pop("_id", None)
                agent_id_str = str(agent_dict["agent_id"])
                self.tool_cache[tool_name] = agent_id_str  # Update cache

                return AgentRegistration(**agent_dict)

            return None
        except Exception as e:
            logger.error(f"Error getting agent by tool {tool_name}: {e}", exc_info=True)
            return None

    async def list_agents(self) -> List[AgentRegistration]:
        """List all registered agents"""
        agents = []
        if self.db is None:
            return []

        try:
            cursor = self.db.agents.find()
            async for agent_dict in cursor:
                agent_dict.pop("_id", None)
                try:
                    agents.append(AgentRegistration(**agent_dict))
                except Exception as p_err:
                    logger.warning(
                        f"Skipping agent due to validation error: {agent_dict.get('agent_id')}. Error: {p_err}")

            return agents
        except Exception as e:
            logger.error(f"Error listing agents: {e}", exc_info=True)
            return []

    async def list_tools(self) -> List[Dict[str, Any]]:
        """List all tools from all online agents"""
        tools = []
        if self.db is None:
            return []

        try:
            cursor = self.db.agents.find(
                {"status": AgentStatus.ONLINE.value, "mcp_tools": {"$exists": True, "$ne": []}},
                {"mcp_tools": 1, "name": 1, "agent_id": 1}
            )

            async for agent_doc in cursor:
                agent_name = agent_doc.get("name", "Unknown")
                agent_id_str = str(agent_doc.get("agent_id", ""))

                for tool_data in agent_doc.get("mcp_tools", []):
                    tool_dict = {
                        "name": tool_data.get("name"),
                        "description": tool_data.get("description"),
                        "agent": agent_name,
                        "agent_id": agent_id_str,
                        "inputSchema": tool_data.get("inputSchema", {}),
                        "annotations": tool_data.get("annotations")
                    }

                    if tool_dict["name"] and tool_dict["inputSchema"] is not None:
                        tools.append(tool_dict)
                    else:
                        logger.warning(f"Skipping malformed tool from agent {agent_id_str}: {tool_data}")

            return tools
        except Exception as e:
            logger.error(f"Error listing tools: {e}", exc_info=True)
            return []

    async def delete_agent(self, agent_id: str) -> bool:
        """Delete an agent by ID"""
        if self.db is None:
            return False

        try:
            agent = await self.get_agent(agent_id)
            if agent:
                agent_id_str = str(agent.agent_id)

                # Remove tools from cache
                tools_to_remove = [k for k, v in self.tool_cache.items() if v == agent_id_str]
                for tool_name in tools_to_remove:
                    if tool_name in self.tool_cache:
                        del self.tool_cache[tool_name]

                # Delete from database
                result = await self.db.agents.delete_one({"agent_id": agent_id})
                if result.deleted_count > 0:
                    logger.info(f"Agent deleted: {agent.name} ({agent_id})")
                    return True

            return False
        except Exception as e:
            logger.error(f"Error deleting agent {agent_id}: {e}", exc_info=True)
            return False

    async def update_agent_heartbeat(self, agent_id: str, timestamp: float) -> bool:
        """Update agent heartbeat timestamp"""
        if self.db is None:
            return False

        try:
            result = await self.db.agents.update_one(
                {"agent_id": agent_id},
                {"$set": {"last_seen": timestamp}}
            )

            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Error updating heartbeat for agent {agent_id}: {e}", exc_info=True)
            return False

    async def count_agents(self, query: Dict[str, Any] = None) -> int:
        """Count agents matching a query"""
        if self.db is None:
            return 0

        try:
            return await self.db.agents.count_documents(query or {})
        except Exception as e:
            logger.error(f"Error counting agents: {e}", exc_info=True)
            return 0

    async def search_agents(self, query: Dict[str, Any] = None, skip: int = 0, limit: int = 100) -> List[
        AgentRegistration]:
        """Search agents with pagination"""
        agents = []
        if self.db is None:
            return []

        try:
            cursor = self.db.agents.find(query or {}).skip(skip).limit(limit)
            async for agent_dict in cursor:
                agent_dict.pop("_id", None)
                try:
                    agents.append(AgentRegistration(**agent_dict))
                except Exception as p_err:
                    logger.warning(
                        f"Skipping agent in search due to validation error: {agent_dict.get('agent_id')}. Error: {p_err}")

            return agents
        except Exception as e:
            logger.error(f"Error searching agents: {e}", exc_info=True)
            return []

    async def save_a2a_task(self, task: A2ATask) -> bool:
        """Save an A2A task"""
        if self.db is None:
            logger.error("Database not initialized, cannot save A2A task.")
            return False

        try:
            task_dict = task.model_dump(exclude_unset=True)
            task_dict["updated_at"] = datetime.now(timezone.utc)

            await self.db.tasks.update_one(
                {"id": task.id},
                {"$set": task_dict},
                upsert=True
            )

            logger.info(f"A2A Task {task.id} saved/updated.")
            return True
        except Exception as e:
            logger.error(f"Error saving A2A task {task.id}: {e}", exc_info=True)
            return False

    async def get_a2a_task(self, task_id: str) -> Optional[A2ATask]:
        """Get an A2A task by ID"""
        if self.db is None:
            return None

        try:
            task_dict = await self.db.tasks.find_one({"id": task_id})
            if task_dict:
                task_dict.pop("_id", None)
                return A2ATask(**task_dict)

            return None
        except Exception as e:
            logger.error(f"Error retrieving A2A task {task_id}: {e}", exc_info=True)
            return None


# ----- CONNECTION MANAGEMENT -----

class MCPSession:
    """Manages MCP sessions and connection state"""

    def __init__(self, storage_param: Optional[MongoDBStorage]):
        self.active_sessions: Dict[str, Dict[str, Any]] = {}
        self.client_connections: Dict[str, List[str]] = {}
        self.storage = storage_param

    def create_session(self, client_id: str) -> str:
        """Create a new MCP session"""
        session_id = str(uuid.uuid4())
        self.active_sessions[session_id] = {
            "client_id": client_id,
            "created_at": time.time(),
            "last_activity": time.time(),
            "state": {}
        }

        self.client_connections.setdefault(client_id, []).append(session_id)
        logger.debug(f"Created MCP session {session_id} for client {client_id}")

        return session_id

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get session by ID"""
        session = self.active_sessions.get(session_id)
        if session:
            session["last_activity"] = time.time()

        return session

    def update_session_activity(self, session_id: str) -> None:
        """Update session last activity timestamp"""
        if session_id in self.active_sessions:
            self.active_sessions[session_id]["last_activity"] = time.time()
            logger.debug(f"Updated session {session_id} activity")

    def close_session(self, session_id: str) -> None:
        """Close a session"""
        if session_id in self.active_sessions:
            session = self.active_sessions.pop(session_id)
            client_id = session.get("client_id")

            if client_id in self.client_connections:
                if session_id in self.client_connections[client_id]:
                    self.client_connections[client_id].remove(session_id)

                if not self.client_connections[client_id]:
                    del self.client_connections[client_id]

            logger.info(f"Closed MCP session {session_id}")

    def get_client_sessions(self, client_id: str) -> List[str]:
        """Get all sessions for a client"""
        return self.client_connections.get(client_id, [])

    def cleanup_stale_sessions(self, max_age: int = 3600) -> int:
        """Clean up stale sessions older than max_age seconds"""
        now = time.time()
        to_remove = [sid for sid, s in self.active_sessions.items()
                     if now - s.get("last_activity", 0) > max_age]

        count = 0
        for session_id in to_remove:
            self.close_session(session_id)
            count += 1

        if count > 0:
            logger.info(f"Cleaned up {count} stale MCP sessions.")

        return count


class ConnectionManager:
    """Manages connections to clients (e.g., UI, monitoring dashboards)"""

    def __init__(self):
        self.active_connections: Dict[str, Dict[str, Any]] = {}

    def register_connection(self, client_id: str, send_func: Callable[[str, Any], Awaitable[None]]) -> None:
        """Register a new connection"""
        self.active_connections[client_id] = {
            "connected_at": time.time(),
            "last_activity": time.time(),
            "send_func": send_func
        }
        logger.info(f"Registered client UI connection: {client_id}")

    def unregister_connection(self, client_id: str) -> None:
        """Unregister a connection"""
        if client_id in self.active_connections:
            del self.active_connections[client_id]
            logger.info(f"Unregistered client UI connection: {client_id}")

    async def send_event(self, client_id: str, event: str, data: Any) -> bool:
        """Send an event to a client"""
        conn_details = self.active_connections.get(client_id)
        if conn_details:
            conn_details["last_activity"] = time.time()
            try:
                await conn_details["send_func"](event, json.dumps(data))
                return True
            except Exception as e:
                logger.error(f"Error sending UI event '{event}' to {client_id}: {e}", exc_info=True)
                self.unregister_connection(client_id)

        return False

    async def broadcast_event(self, event: str, data: Any) -> None:
        """Broadcast an event to all connected clients"""
        await asyncio.gather(*[
            self.send_event(cid, event, data)
            for cid in list(self.active_connections.keys())
        ])

    def get_all_connections(self) -> List[str]:
        """Get all active connection IDs"""
        return list(self.active_connections.keys())


# ----- APPLICATION LIFECYCLE -----

storage: Optional[MongoDBStorage] = None
mongo_client: Optional[AsyncIOMotorClient] = None
db: Optional[AsyncIOMotorDatabase] = None


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    """Lifespan manager for the FastAPI application"""
    global storage, mongo_client, db
    logger.info("AIRA Hub lifespan startup...")

    temp_storage = MongoDBStorage(MONGODB_URL)
    app_instance.state.storage_failed = False

    try:
        await temp_storage.init()
        storage = temp_storage
        mongo_client = temp_storage.mongo_db_client
        db = temp_storage.db

        # Attach to app state
        app_instance.state.storage = storage
        app_instance.state.mongo_client = mongo_client
        app_instance.state.db = db
        app_instance.state.connection_manager = ConnectionManager()
        app_instance.state.mcp_session_manager = MCPSession(storage)
        app_instance.state.start_time = time.time()
        app_instance.state.active_mcp_streams = {}  # For tracking active MCP streams

        logger.info("AIRA Hub services initialized successfully.")

        # Start background tasks (e.g., agent cleanup)
        # cleanup_task = asyncio.create_task(periodic_cleanup(app_instance.state))

        yield  # Application runs here

        logger.info("AIRA Hub lifespan shutdown...")
        # await cleanup_task.cancel() # Ensure background task is stopped
        if storage:
            await storage.close()
        logger.info("AIRA Hub shutdown complete.")

    except Exception as e:
        logger.critical(f"AIRA Hub critical startup error: {e}", exc_info=True)
        app_instance.state.storage_failed = True

        yield


# ----- MCP STREAM HANDLER -----

# Ensure these imports are at the top of your aira_hub.py file
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Union, AsyncGenerator, Literal, Set  # Ensure Set is imported

import httpx
from fastapi import HTTPException, status  # Assuming FastAPI is used, for status codes
from pydantic import ValidationError  # Assuming Pydantic is used for models


# Your existing MCPRequestModel, MCPResponseModel, MongoDBStorage, etc. should be defined above
# For example:
# class MCPRequestModel(BaseModel): ...
# class MCPResponseModel(BaseModel): ...
# class MongoDBStorage: ...
# logger = logging.getLogger("aira_hub") # Assuming logger is set up
# STREAM_SEPARATOR = b'\r\n' # Assuming this is defined

async def mcp_stream_handler(
        stream_id: str,
        initial_messages: List[Dict[str, Any]],
        request_stream: AsyncGenerator[bytes, None],
        send_queue: asyncio.Queue,  # For sending MCPResponseModel instances back to the client
        storage_inst: MongoDBStorage,
        app_state: Any
):
    """Handles the bi-directional MCP Streamable HTTP communication."""
    logger.info(f"MCP Stream {stream_id}: Handler initiated.")
    pending_tasks: Dict[str, asyncio.Task] = {}
    background_notifications: List[asyncio.Task] = []

    expected_response_ids: Set[Union[str, int]] = set()

    reader_task_finished_event = asyncio.Event()
    outstanding_responses_event = asyncio.Event()
    outstanding_responses_event.set()  # Start as set (no outstanding responses initially)

    # This client is used for all outbound calls to agents within this stream
    async with httpx.AsyncClient(timeout=MCP_TIMEOUT, verify=True) as agent_http_client:

        async def check_and_manage_outstanding_event():
            nonlocal outstanding_responses_event
            all_done = reader_task_finished_event.is_set() and not pending_tasks and not expected_response_ids
            if all_done:
                if not outstanding_responses_event.is_set():
                    logger.debug(f"MCP Stream {stream_id}: All conditions met. Setting outstanding_responses_event.")
                    outstanding_responses_event.set()
            else:
                if outstanding_responses_event.is_set():
                    logger.debug(
                        f"MCP Stream {stream_id}: Conditions NOT fully met (ReaderDone={reader_task_finished_event.is_set()}, PendingTasks={len(pending_tasks)}, ExpectedIDs={len(expected_response_ids)}). Clearing outstanding_responses_event.")
                    outstanding_responses_event.clear()

        async def forward_and_process_agent_response(
                client_mcp_req_id: Union[str, int],
                original_client_method: str,
                agent_target_url: str,
                payload_to_agent: Dict[str, Any],
                agent_name_for_log: str,
                is_a2a_bridged: bool
        ):
            logger.info(
                f"MCP Stream {stream_id}: ENTERING forward_and_process for MCP ID {client_mcp_req_id} to '{agent_name_for_log}' at {agent_target_url}"
            )
            response_text_debug = ""
            response_queued_successfully = False  # Track if a response was put on the queue

            custom_headers = {
                "User-Agent": "AIRA-Hub-MCP-Proxy/1.0",  # Be a good citizen
                "Accept": "application/json, application/json-seq, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Connection": "keep-alive"
            }

            try:
                logger.info(
                    f"MCP Stream {stream_id}: ATTEMPTING POST to agent '{agent_name_for_log}' at {agent_target_url} for MCP ID {client_mcp_req_id}."
                )
                logger.debug(f"MCP Stream {stream_id}: Payload to agent: {json.dumps(payload_to_agent)}")

                response_from_agent_http = await agent_http_client.post(
                    agent_target_url,
                    json=payload_to_agent,
                    headers=custom_headers,
                    timeout=1800.0  # Consider making this configurable
                )
                response_text_debug = response_from_agent_http.text
                logger.info(
                    f"MCP Stream {stream_id}: Agent '{agent_name_for_log}' HTTP status {response_from_agent_http.status_code} for MCP ID {client_mcp_req_id}."
                )
                logger.debug(
                    f"MCP Stream {stream_id}: Agent '{agent_name_for_log}' raw response for MCP ID {client_mcp_req_id}: {response_text_debug[:500]}..."
                )
                response_from_agent_http.raise_for_status()  # Important: raises for 4xx/5xx

                agent_response_data_dict = response_from_agent_http.json()
                mcp_result_payload_for_client: Optional[List[Dict[str, Any]]] = None
                final_mcp_object_to_queue: Optional[MCPResponseModel] = None

                if is_a2a_bridged:
                    logger.debug(
                        f"MCP Stream {stream_id}: Translating A2A response from '{agent_name_for_log}' for MCP ID {client_mcp_req_id}. A2A: {json.dumps(agent_response_data_dict)}")
                    if "result" in agent_response_data_dict and isinstance(agent_response_data_dict["result"], dict):
                        a2a_task_result = agent_response_data_dict["result"]
                        extracted_text_content: Optional[str] = None
                        if "artifacts" in a2a_task_result and isinstance(a2a_task_result["artifacts"], list) and \
                                a2a_task_result["artifacts"]:
                            first_artifact = a2a_task_result["artifacts"][0]
                            if isinstance(first_artifact, dict) and "parts" in first_artifact and isinstance(
                                    first_artifact["parts"], list) and first_artifact["parts"]:
                                first_part = first_artifact["parts"][0]
                                if isinstance(first_part, dict) and first_part.get("type") == "text":
                                    extracted_text_content = first_part.get("text")
                        if extracted_text_content is not None:
                            mcp_result_payload_for_client = [{"type": "text", "text": extracted_text_content}]
                        else:  # Fallback if no primary text artifact
                            status_state = a2a_task_result.get("status", {}).get("state", "unknown_a2a_state")
                            status_message_obj = a2a_task_result.get("status", {}).get("message", {})
                            status_message_parts = status_message_obj.get("parts", []) if isinstance(status_message_obj,
                                                                                                     dict) else []
                            a2a_status_text = None
                            if status_message_parts and status_message_parts[0].get("type") == "text": a2a_status_text = \
                            status_message_parts[0].get("text")
                            fallback_msg = f"A2A Task Status: {status_state}." + (
                                f" Message: {a2a_status_text}" if a2a_status_text else " No primary text artifact.")
                            logger.warning(
                                f"MCP Stream {stream_id}: No primary text artifact in A2A for MCP ID {client_mcp_req_id}. Fallback: {fallback_msg}")
                            mcp_result_payload_for_client = [{"type": "text", "text": fallback_msg}]
                        final_mcp_object_to_queue = MCPResponseModel(id=client_mcp_req_id,
                                                                     result=mcp_result_payload_for_client)
                    elif "error" in agent_response_data_dict:  # A2A agent returned a JSON-RPC error
                        a2a_error = agent_response_data_dict['error']
                        logger.error(
                            f"MCP Stream {stream_id}: A2A Agent '{agent_name_for_log}' (MCP ID {client_mcp_req_id}) returned A2A error: {a2a_error}")
                        final_mcp_object_to_queue = MCPResponseModel(id=client_mcp_req_id,
                                                                     error={"code": a2a_error.get("code", -32006),
                                                                            "message": f"A2A Agent Error: {a2a_error.get('message', 'Unknown A2A error')}",
                                                                            "data": a2a_error.get("data")})
                    else:  # Unexpected A2A response structure
                        logger.error(
                            f"MCP Stream {stream_id}: Unexpected A2A response structure from '{agent_name_for_log}' for MCP ID {client_mcp_req_id}: {agent_response_data_dict}")
                        final_mcp_object_to_queue = MCPResponseModel(id=client_mcp_req_id, error={"code": -32005,
                                                                                                  "message": "Hub error: Could not parse A2A agent's response"})

                else:  # Direct MCP agent call
                    logger.debug(
                        f"MCP Stream {stream_id}: Processing direct MCP response from '{agent_name_for_log}' for MCP ID {client_mcp_req_id}.")
                    if isinstance(agent_response_data_dict, dict) and agent_response_data_dict.get("jsonrpc") == "2.0":
                        try:
                            mcp_resp_from_agent = MCPResponseModel(**agent_response_data_dict)
                            # Crucially, the ID in mcp_resp_from_agent might be the agent's internal ID, not client_mcp_req_id.
                            # We must respond to the client with client_mcp_req_id.
                            final_mcp_object_to_queue = MCPResponseModel(id=client_mcp_req_id,
                                                                         result=mcp_resp_from_agent.result,
                                                                         error=mcp_resp_from_agent.error)
                            if mcp_resp_from_agent.error:
                                logger.warning(
                                    f"MCP Stream {stream_id}: Direct MCP agent '{agent_name_for_log}' returned error for its ID {mcp_resp_from_agent.id} (Client MCP ID {client_mcp_req_id}): {mcp_resp_from_agent.error}")
                        except ValidationError as e_val_agent_resp:
                            logger.error(
                                f"MCP Stream {stream_id}: Invalid MCPResponseModel from direct MCP agent '{agent_name_for_log}' for MCP ID {client_mcp_req_id}: {e_val_agent_resp}")
                            final_mcp_object_to_queue = MCPResponseModel(id=client_mcp_req_id, error={"code": -32005,
                                                                                                      "message": "Invalid response from target MCP agent",
                                                                                                      "data": str(
                                                                                                          e_val_agent_resp)})
                    else:  # Non-JSONRPC response, or not a list as expected by some MCP tool call results
                        logger.warning(
                            f"MCP Stream {stream_id}: Direct MCP agent '{agent_name_for_log}' returned non-JSONRPC or unexpected format for MCP ID {client_mcp_req_id}. Result: {agent_response_data_dict}")
                        # MCP spec implies tools/call result should be a list of content parts.
                        # If it's not, we should attempt to wrap it or error.
                        # Forcing it into a text part for now.
                        wrapped_result = [{"type": "text", "text": json.dumps(
                            agent_response_data_dict) if agent_response_data_dict is not None else "Agent returned non-standard data."}]
                        final_mcp_object_to_queue = MCPResponseModel(id=client_mcp_req_id, result=wrapped_result)

                if final_mcp_object_to_queue:
                    if original_client_method == "tools/call" and \
                            final_mcp_object_to_queue.result and \
                            isinstance(final_mcp_object_to_queue.result, list) and \
                            not final_mcp_object_to_queue.error:
                        logger.debug(
                            f"MCP Stream {stream_id}: Wrapping 'tools/call' result for MCP ID {client_mcp_req_id}.")
                        actual_mcp_result_object = {
                            "content": final_mcp_object_to_queue.result
                        }
                        final_mcp_object_to_queue.result = actual_mcp_result_object

                    await send_queue.put(final_mcp_object_to_queue)
                    response_queued_successfully = True
                    logger.info(
                        f"MCP Stream {stream_id}: Successfully queued MCPResponse for MCP ID {client_mcp_req_id} to client. Final structure: {final_mcp_object_to_queue.model_dump_json(exclude_none=True, indent=2)}")



            except httpx.HTTPStatusError as e_http:  # For 4xx/5xx responses

                logger.error(
                    f"MCP Stream {stream_id}: Agent HTTPStatusError for MCP ID {client_mcp_req_id} from '{agent_name_for_log}': {e_http.response.status_code} - Resp: {response_text_debug[:500]}",
                    exc_info=True)

                await send_queue.put(MCPResponseModel(id=client_mcp_req_id, error={"code": -32003,
                                                                                   "message": f"Agent error: {e_http.response.status_code}",
                                                                                   "data": response_text_debug[:500]}))

                response_queued_successfully = True

                # Catch specific timeouts first

            except httpx.ConnectTimeout as e_connect_timeout:

                logger.error(
                    f"MCP Stream {stream_id}: ConnectTimeout calling agent '{agent_name_for_log}' for MCP ID {client_mcp_req_id}: {e_connect_timeout}",
                    exc_info=True)

                await send_queue.put(MCPResponseModel(id=client_mcp_req_id, error={"code": -32004,
                                                                                   "message": f"Agent connection timed out: {type(e_connect_timeout).__name__}"}))

                response_queued_successfully = True

            except httpx.ReadTimeout as e_read_timeout:

                logger.error(
                    f"MCP Stream {stream_id}: ReadTimeout from agent '{agent_name_for_log}' for MCP ID {client_mcp_req_id}: {e_read_timeout}",
                    exc_info=True)

                await send_queue.put(MCPResponseModel(id=client_mcp_req_id, error={"code": -32004,
                                                                                   "message": f"Agent read timed out: {type(e_read_timeout).__name__}"}))

                response_queued_successfully = True

            except httpx.Timeout as e_general_timeout:  # Catch any other httpx.Timeout

                logger.error(
                    f"MCP Stream {stream_id}: General Timeout with agent '{agent_name_for_log}' for MCP ID {client_mcp_req_id}: {e_general_timeout}",
                    exc_info=True)

                await send_queue.put(MCPResponseModel(id=client_mcp_req_id, error={"code": -32004,
                                                                                   "message": f"Agent operation timed out: {type(e_general_timeout).__name__}"}))

                response_queued_successfully = True

            except httpx.RequestError as e_req:  # Catch other network errors (like NameResolutionError, etc.)

                logger.error(
                    f"MCP Stream {stream_id}: Agent httpx.RequestError for MCP ID {client_mcp_req_id} to '{agent_name_for_log}': {type(e_req).__name__} - {e_req}",
                    exc_info=True)

                await send_queue.put(MCPResponseModel(id=client_mcp_req_id, error={"code": -32004,
                                                                                   "message": f"Agent communication error: {type(e_req).__name__}"}))

                response_queued_successfully = True

            except json.JSONDecodeError as e_json_dec:
                logger.error(
                    f"MCP Stream {stream_id}: Agent JSONDecodeError for MCP ID {client_mcp_req_id} from '{agent_name_for_log}'. Resp: {response_text_debug[:500]}",
                    exc_info=True)
                await send_queue.put(MCPResponseModel(id=client_mcp_req_id,
                                                      error={"code": -32005, "message": "Agent response parse error",
                                                             "data": str(e_json_dec)}))
                response_queued_successfully = True
            except Exception as e_fwd_generic:
                logger.error(
                    f"MCP Stream {stream_id}: GENERIC EXCEPTION in forward_and_process for MCP ID {client_mcp_req_id} to '{agent_name_for_log}': {type(e_fwd_generic).__name__} - {e_fwd_generic}",
                    exc_info=True)
                try:
                    await send_queue.put(MCPResponseModel(id=client_mcp_req_id, error={"code": -32000,
                                                                                       "message": "Hub internal error during agent communication."}))
                    response_queued_successfully = True
                except Exception as e_send_q_err:  # Error putting error on queue
                    logger.error(
                        f"MCP Stream {stream_id}: FAILED to put generic error on send_queue for MCP ID {client_mcp_req_id}: {e_send_q_err}",
                        exc_info=True)
            finally:
                if response_queued_successfully and client_mcp_req_id is not None:
                    expected_response_ids.discard(client_mcp_req_id)

                task_id_str = str(client_mcp_req_id)  # Ensure it's a string for dict key
                if task_id_str in pending_tasks:
                    del pending_tasks[task_id_str]

                await check_and_manage_outstanding_event()
                logger.info(f"MCP Stream {stream_id}: EXITING forward_and_process for MCP ID {client_mcp_req_id}")

        async def process_mcp_request(req_data: Dict[str, Any]):
            nonlocal outstanding_responses_event  # Ensure we can modify the event from the outer scope
            try:
                mcp_req = MCPRequestModel(**req_data)
            except ValidationError as e_val:
                logger.warning(f"MCP Stream {stream_id}: Invalid MCP request: {req_data}, Error: {e_val}")
                error_id_val = req_data.get("id")  # Try to get ID even if invalid
                await send_queue.put(MCPResponseModel(id=error_id_val,
                                                      error={"code": -32600, "message": "Invalid Request",
                                                             "data": e_val.errors()}))
                if error_id_val is not None:  # If an error response was sent for an ID
                    expected_response_ids.discard(error_id_val)
                    await check_and_manage_outstanding_event()
                return

            logger.info(
                f"MCP Stream {stream_id}: RECV: {mcp_req.method} (ID: {mcp_req.id}) Params: {json.dumps(mcp_req.params)[:200]}...")

            if mcp_req.id is not None:
                expected_response_ids.add(mcp_req.id)
                await check_and_manage_outstanding_event()  # Call after adding

            response_sent_directly = False
            if mcp_req.method == "initialize":
                client_info = mcp_req.params.get("clientInfo", {}) if isinstance(mcp_req.params, dict) else {}
                logger.info(
                    f"MCP Stream {stream_id}: Initializing for {client_info.get('name', 'Unknown')} v{client_info.get('version', 'N/A')}")
                resp_params = {
                    "protocolVersion": mcp_req.params.get("protocolVersion", "2024-11-05") if isinstance(mcp_req.params,
                                                                                                         dict) else "2024-11-05",
                    "serverInfo": {"name": "AIRA Hub", "version": "1.0.0"},  # TODO: App version
                    "capabilities": {"toolProxy": True, "toolDiscovery": True, "resourceDiscovery": True}}
                await send_queue.put(MCPResponseModel(id=mcp_req.id, result=resp_params))
                response_sent_directly = True

            elif mcp_req.method == "notifications/initialized":
                logger.info(f"MCP Stream {stream_id}: Client acknowledged initialization.")
                # This is a notification, no response needed, no ID to track for response.
                # If it had an ID (it shouldn't according to spec), we would not discard it here.
                # It does not add to expected_response_ids if mcp_req.id is None.

            elif mcp_req.method == "tools/list" or mcp_req.method == "mcp.discoverTools":
                hub_tools = await storage_inst.list_tools()
                mcp_tools = []
                for ht in hub_tools:
                    td = {"name": ht.get("name"), "inputSchema": ht.get("inputSchema")}
                    if ht.get("description"): td["description"] = ht.get("description")
                    if ht.get("annotations"): td["annotations"] = ht.get("annotations")
                    mcp_tools.append(td)
                await send_queue.put(MCPResponseModel(id=mcp_req.id, result={"tools": mcp_tools}))
                response_sent_directly = True

            elif mcp_req.method == "resources/list":
                agents = await storage_inst.list_agents()
                mcp_res = []
                for ar in agents:
                    uri = f"aira-hub://agent/{ar.agent_id}"
                    # ... (your existing resource definition logic) ...
                    desc = f"AIRA Agent: {ar.name} ({ar.status.value}) - {ar.description or ''}"
                    meta = {"agent_id": ar.agent_id, "name": ar.name, "url": ar.url,
                            "status": ar.status.value, "capabilities": ar.aira_capabilities,
                            "tags": ar.tags, "category": ar.category, "version": ar.version,
                            "registered_at": datetime.fromtimestamp(ar.created_at,
                                                                    timezone.utc).isoformat() if ar.created_at else None,
                            "last_seen": datetime.fromtimestamp(ar.last_seen,
                                                                timezone.utc).isoformat() if ar.last_seen else None}
                    mcp_res.append({"uri": uri, "description": desc, "metadata": meta})
                await send_queue.put(MCPResponseModel(id=mcp_req.id, result={"resources": mcp_res}))
                response_sent_directly = True

            elif mcp_req.method == "prompts/list":
                await send_queue.put(
                    MCPResponseModel(id=mcp_req.id, result={"prompts": []}))  # Hub has no native prompts
                response_sent_directly = True

            elif mcp_req.method == "tools/call":
                if mcp_req.id is None:  # Notification, should not happen for tools/call
                    logger.warning(f"MCP Stream {stream_id}: Received tools/call notification (no ID). Ignoring.")
                    return

                if not isinstance(mcp_req.params, dict):
                    await send_queue.put(MCPResponseModel(id=mcp_req.id, error={"code": -32602,
                                                                                "message": "Invalid params for tools/call"}))
                    response_sent_directly = True  # Error is a response
                else:
                    tool_name = mcp_req.params.get("name")
                    tool_args = mcp_req.params.get("arguments", {})
                    if not tool_name or not isinstance(tool_name, str):
                        await send_queue.put(MCPResponseModel(id=mcp_req.id, error={"code": -32602,
                                                                                    "message": "Invalid params: 'name' string required"}))
                        response_sent_directly = True
                    else:
                        agent = await storage_inst.get_agent_by_tool(tool_name)
                        tool_def_from_db = None
                        if agent: tool_def_from_db = next((t for t in agent.mcp_tools if t.name == tool_name), None)

                        if not agent or not tool_def_from_db:
                            logger.warning(f"MCP Stream {stream_id}: Tool '{tool_name}' or provider not found/offline.")
                            await send_queue.put(MCPResponseModel(id=mcp_req.id, error={"code": -32601,
                                                                                        "message": f"Tool not found or agent unavailable: {tool_name}"}))
                            response_sent_directly = True
                        else:
                            annotations = tool_def_from_db.annotations or {}
                            bridge_type = annotations.get("aira_bridge_type")
                            downstream_payload: Dict[str, Any]
                            target_url: Optional[str] = None
                            is_a2a = False

                            if bridge_type == "a2a":
                                is_a2a = True
                                skill_id_a2a = annotations.get("aira_a2a_target_skill_id")
                                target_url = annotations.get(
                                    "aira_a2a_agent_url")  # This should be the A2A agent's base URL
                                if not skill_id_a2a or not target_url:
                                    logger.error(
                                        f"MCP Stream {stream_id}: Misconfigured A2A bridge for tool '{tool_name}'. Missing skill_id or agent_url in annotations.")
                                    await send_queue.put(MCPResponseModel(id=mcp_req.id, error={"code": -32002,
                                                                                                "message": "Hub A2A bridge misconfiguration"}))
                                    response_sent_directly = True
                                else:
                                    a2a_data = {"skill_id": skill_id_a2a}
                                    if "user_input" in tool_args: a2a_data["user_input"] = tool_args["user_input"]
                                    if "a2a_task_id_override" in tool_args: a2a_data["a2a_task_id_override"] = \
                                    tool_args["a2a_task_id_override"]
                                    downstream_payload = {
                                        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "tasks/send",  # A2A method
                                        "params": {"id": str(mcp_req.id),  # A2A task ID, can use MCP req ID for tracing
                                                   "message": {"role": "user",
                                                               "parts": [{"type": "data", "data": a2a_data}]}}
                                    }
                            elif agent.stdio_command:
                                logger.warning(
                                    f"MCP Stream {stream_id}: Tool '{tool_name}' on stdio agent '{agent.name}'. Hub cannot proxy.")
                                err_data = {"message": f"Tool '{tool_name}' is on local stdio agent.",
                                            "agent_name": agent.name, "command": agent.stdio_command}
                                await send_queue.put(MCPResponseModel(id=mcp_req.id, error={"code": -32010,
                                                                                            "message": "Local stdio execution required",
                                                                                            "data": err_data}))
                                response_sent_directly = True
                            else:  # Direct MCP proxy
                                is_a2a = False
                                target_url = agent.mcp_url or agent.mcp_stream_url  # Prefer stream_url if available and it's POSTable
                                if not target_url:
                                    logger.error(
                                        f"MCP Stream {stream_id}: Agent {agent.name} for tool '{tool_name}' has no mcp_url/mcp_stream_url.")
                                    await send_queue.put(MCPResponseModel(id=mcp_req.id, error={"code": -32001,
                                                                                                "message": "Agent endpoint misconfiguration"}))
                                    response_sent_directly = True
                                else:
                                    downstream_payload = MCPRequestModel(method="tools/call", params={"name": tool_name,
                                                                                                      "arguments": tool_args},
                                                                         id=str(uuid.uuid4())).model_dump(
                                        exclude_none=True)

                            if not response_sent_directly:  # If no direct error was sent, proceed to create task
                                if not target_url:  # Should have been caught, but as a safeguard
                                    logger.error(
                                        f"MCP Stream {stream_id}: Target URL for '{tool_name}' is None after logic. This is a bug.")
                                    await send_queue.put(MCPResponseModel(id=mcp_req.id, error={"code": -32000,
                                                                                                "message": "Internal Hub Error: Target URL undefined"}))
                                    response_sent_directly = True
                                else:
                                    fwd_task = asyncio.create_task(
                                        forward_and_process_agent_response(
                                            client_mcp_req_id=mcp_req.id, agent_target_url=target_url,
                                            original_client_method=mcp_req.method,
                                            payload_to_agent=downstream_payload, agent_name_for_log=agent.name,
                                            is_a2a_bridged=is_a2a
                                        )
                                    )
                                    pending_tasks[str(mcp_req.id)] = fwd_task
                                    # No direct response here, task will handle it. So response_sent_directly remains False.
            else:  # Non-standard MCP method, treat as direct passthrough if agent found
                logger.warning(
                    f"MCP Stream {stream_id}: Non-standard method '{mcp_req.method}'. Attempting direct proxy if agent found for this as a tool name.")
                agent_direct = await storage_inst.get_agent_by_tool(mcp_req.method)  # Try to find agent by method name

                if not agent_direct:
                    logger.warning(
                        f"MCP Stream {stream_id}: Agent for direct method/tool '{mcp_req.method}' not found.")
                    if mcp_req.id is not None:  # If it was a request
                        await send_queue.put(MCPResponseModel(id=mcp_req.id, error={"code": -32601,
                                                                                    "message": f"Method not found: {mcp_req.method}"}))
                        response_sent_directly = True
                    # If it was a notification (mcp_req.id is None), we just drop it.
                else:
                    target_url_direct = agent_direct.mcp_url or agent_direct.mcp_stream_url
                    if not target_url_direct:
                        logger.error(
                            f"MCP Stream {stream_id}: Agent {agent_direct.name} for direct method '{mcp_req.method}' has no MCP endpoint.")
                        if mcp_req.id is not None:
                            await send_queue.put(MCPResponseModel(id=mcp_req.id, error={"code": -32001,
                                                                                        "message": "Agent endpoint misconfiguration for direct call"}))
                            response_sent_directly = True
                    else:
                        payload_direct = mcp_req.model_dump(exclude_none=True)
                        if mcp_req.id is not None:  # It's a request expecting a response
                            fwd_direct_task = asyncio.create_task(
                                forward_and_process_agent_response(
                                    client_mcp_req_id=mcp_req.id, agent_target_url=target_url_direct,
                                    payload_to_agent=payload_direct, agent_name_for_log=agent_direct.name,
                                    is_a2a_bridged=False  # Assuming non-standard methods are not A2A bridged
                                )
                            )
                            pending_tasks[str(mcp_req.id)] = fwd_direct_task
                            # No direct response here
                        else:  # It's a notification
                            logger.info(
                                f"MCP Stream {stream_id}: Forwarding direct notification '{mcp_req.method}' to agent {agent_direct.name}")
                            bg_notify_task = asyncio.create_task(
                                agent_http_client.post(target_url_direct, json=payload_direct))
                            background_notifications.append(bg_notify_task)
                            # No direct response for notifications

            if response_sent_directly and mcp_req.id is not None:
                expected_response_ids.discard(mcp_req.id)
                await check_and_manage_outstanding_event()

        async def read_from_client():
            buffer = b""
            try:
                async for chunk in request_stream:
                    if not chunk: continue
                    logger.debug(f"MCP Stream {stream_id}: Raw chunk from client: {chunk!r}")
                    buffer += chunk
                    while STREAM_SEPARATOR in buffer:
                        message_bytes, buffer = buffer.split(STREAM_SEPARATOR, 1)
                        if message_bytes.strip():
                            try:
                                req_data_outer = json.loads(message_bytes.decode('utf-8'))
                                req_list_to_process = []
                                if isinstance(req_data_outer, list):  # Batch request
                                    req_list_to_process.extend(d for d in req_data_outer if isinstance(d, dict))
                                elif isinstance(req_data_outer, dict):  # Single request
                                    req_list_to_process.append(req_data_outer)
                                else:  # Invalid top-level JSON type
                                    logger.warning(
                                        f"MCP Stream {stream_id}: Invalid JSON type from client: {type(req_data_outer)}")
                                    await send_queue.put(MCPResponseModel(id=None, error={"code": -32700,
                                                                                          "message": "Invalid request format (not object or array)"}))
                                    continue  # Next chunk/message

                                for req_data_item in req_list_to_process:
                                    await process_mcp_request(req_data_item)

                            except json.JSONDecodeError:
                                logger.error(
                                    f"MCP Stream {stream_id}: JSON decode error from client: {message_bytes!r}",
                                    exc_info=True)
                                await send_queue.put(
                                    MCPResponseModel(id=None, error={"code": -32700, "message": "Parse error"}))
                            except Exception as e_p:  # Catchall for errors during process_mcp_request or other issues
                                logger.error(
                                    f"MCP Stream {stream_id}: Error processing client message: {message_bytes!r}, Error: {e_p}",
                                    exc_info=True)
                                error_id_p = None
                                try:
                                    error_id_p = json.loads(message_bytes.decode('utf-8')).get("id")
                                except:
                                    pass
                                await send_queue.put(MCPResponseModel(id=error_id_p, error={"code": -32603,
                                                                                            "message": "Internal error processing message"}))
                logger.info(f"MCP Stream {stream_id}: Client request stream finished.")
            except asyncio.CancelledError:
                logger.info(f"MCP Stream {stream_id}: Client reader task cancelled.")
                raise  # Important to re-raise
            except Exception as e_read_outer:  # Catch errors from request_stream itself
                logger.error(f"MCP Stream {stream_id}: Error reading from client stream: {e_read_outer}", exc_info=True)
                try:
                    await send_queue.put(
                        MCPResponseModel(id=None, error={"code": -32000, "message": "Hub stream read error"}))
                except:
                    pass  # Avoid error in error handling
            finally:
                logger.info(f"MCP Stream {stream_id}: Client reader task finally block entered.")

        async def read_from_client_wrapper():
            try:
                await read_from_client()
            except asyncio.CancelledError:
                logger.info(f"MCP Stream {stream_id}: read_from_client_wrapper: Reader task cancelled.")
                raise
            except Exception as e:  # Should be caught by read_from_client's own try/except
                logger.error(f"MCP Stream {stream_id}: read_from_client_wrapper: UNEXPECTED Error in reader: {e}",
                             exc_info=True)
            finally:
                logger.info(f"MCP Stream {stream_id}: read_from_client_wrapper: Reader finished. Signalling.")
                reader_task_finished_event.set()
                await check_and_manage_outstanding_event()

        reader_main_task = asyncio.create_task(read_from_client_wrapper())

        try:
            if initial_messages:
                logger.debug(f"MCP Stream {stream_id}: Processing {len(initial_messages)} initial POSTed messages.")
                initial_ids_count = 0
                for msg_data in initial_messages:
                    if isinstance(msg_data, dict) and msg_data.get("jsonrpc") == "2.0":
                        if msg_data.get("id") is not None:  # If it's a request expecting a response
                            initial_ids_count += 1
                            # `process_mcp_request` will add it to expected_response_ids
                        await process_mcp_request(msg_data)
                    elif isinstance(msg_data, dict) and msg_data.get(
                            "error"):  # Hub itself generated error parsing initial body
                        await send_queue.put(MCPResponseModel(**msg_data))
                        if msg_data.get("id") is not None:  # This error also counts as a response
                            expected_response_ids.discard(msg_data.get("id"))
                            await check_and_manage_outstanding_event()
                    else:  # Malformed initial message not caught by earlier JSON parsing
                        logger.warning(
                            f"MCP Stream {stream_id}: Invalid initial non-JSONRPC message in POST: {msg_data}")
                        error_id_initial = msg_data.get("id") if isinstance(msg_data, dict) else None
                        await send_queue.put(MCPResponseModel(id=error_id_initial, error={"code": -32600,
                                                                                          "message": "Invalid initial request structure"}))
                        if error_id_initial is not None:
                            expected_response_ids.discard(error_id_initial)
                            await check_and_manage_outstanding_event()

                # Initial state of outstanding_responses_event depends on whether initial messages had IDs
                if not expected_response_ids:  # If no IDs were added from initial_messages
                    if not outstanding_responses_event.is_set(): outstanding_responses_event.set()
                else:  # If there were IDs, it should be cleared (process_mcp_request handles this)
                    if outstanding_responses_event.is_set(): outstanding_responses_event.clear()

            while not (reader_task_finished_event.is_set() and outstanding_responses_event.is_set()):
                active_tasks_for_wait = []
                if reader_main_task and not reader_main_task.done():
                    active_tasks_for_wait.append(reader_main_task)

                active_tasks_for_wait.extend(t for t in pending_tasks.values() if t and not t.done())
                active_tasks_for_wait.extend(t for t in background_notifications if t and not t.done())

                event_waits = [
                    asyncio.create_task(reader_task_finished_event.wait()),
                    asyncio.create_task(outstanding_responses_event.wait())
                ]
                tasks_to_wait_on_loop = active_tasks_for_wait + event_waits

                if not tasks_to_wait_on_loop:
                    logger.debug(f"MCP Stream {stream_id}: Main loop: No active tasks or event waits. Breaking.")
                    break

                logger.debug(
                    f"MCP Stream {stream_id}: Main loop waiting. ReaderDone={reader_task_finished_event.is_set()}, OutstandingDone={outstanding_responses_event.is_set()}, PendingTasks={len(pending_tasks)}, ExpectedIDs={len(expected_response_ids)}, WaitTasks={len(tasks_to_wait_on_loop)}")

                done_in_loop, _ = await asyncio.wait(
                    tasks_to_wait_on_loop,
                    timeout=1.0,  # Adjust timeout as needed, shorter for more responsive checks
                    return_when=asyncio.FIRST_COMPLETED
                )

                # Clean up completed background notification tasks
                for task_done in done_in_loop:
                    if task_done in background_notifications and task_done.done():
                        if task_done.exception():
                            logger.error(
                                f"MCP Stream {stream_id}: Background notification task failed: {task_done.exception()}",
                                exc_info=task_done.exception())
                        try:
                            background_notifications.remove(task_done)
                        except ValueError:
                            pass  # Already removed

                # Re-check conditions after wait, in case an event got set or task finished
                await check_and_manage_outstanding_event()

            logger.info(
                f"MCP Stream {stream_id}: Loop exited. ReaderDone={reader_task_finished_event.is_set()}, OutstandingDone={outstanding_responses_event.is_set()}."
            )

        except asyncio.CancelledError:
            logger.info(f"MCP Stream {stream_id}: Main handler task was cancelled.")
            if reader_main_task and not reader_main_task.done():
                reader_main_task.cancel()
        except Exception as e_handler_outer:
            logger.error(f"MCP Stream {stream_id}: Unhandled error in MCP stream main handler: {e_handler_outer}",
                         exc_info=True)
            if reader_main_task and not reader_main_task.done():
                reader_main_task.cancel()
            try:
                await send_queue.put(
                    MCPResponseModel(id=None, error={"code": -32000, "message": "Hub internal stream error"}))
            except Exception:
                pass
        finally:
            logger.info(
                f"MCP Stream {stream_id}: Main handler FINAL cleanup. Cancelling pending_tasks={len(pending_tasks)}, background_notifications={len(background_notifications)}, reader_main_task_done={reader_main_task.done() if reader_main_task else 'N/A'}")

            all_tasks_to_cancel_at_end = []
            if reader_main_task and not reader_main_task.done():
                reader_main_task.cancel()
                all_tasks_to_cancel_at_end.append(reader_main_task)

            for task_collection_at_end in [list(pending_tasks.values()), list(background_notifications)]:
                for task_obj_at_end in task_collection_at_end:
                    if task_obj_at_end and not task_obj_at_end.done():
                        task_obj_at_end.cancel()
                        all_tasks_to_cancel_at_end.append(task_obj_at_end)

            if all_tasks_to_cancel_at_end:
                logger.debug(
                    f"MCP Stream {stream_id}: Main handler finally: Gathering {len(all_tasks_to_cancel_at_end)} tasks for cancellation.")
                await asyncio.gather(*all_tasks_to_cancel_at_end, return_exceptions=True)
                logger.debug(f"MCP Stream {stream_id}: Main handler finally: Gathered cancelled tasks.")
            else:
                logger.debug(f"MCP Stream {stream_id}: Main handler finally: No tasks needed explicit cancellation.")

            try:
                await send_queue.put(None)  # Signal response_generator to end
                logger.debug(f"MCP Stream {stream_id}: Main handler finally: Put None sentinel on send_queue.")
            except Exception as e_final_q:
                logger.error(
                    f"MCP Stream {stream_id}: Error putting None sentinel on send_queue in finally: {e_final_q}")

            if app_state and hasattr(app_state, 'active_mcp_streams') and stream_id in app_state.active_mcp_streams:
                del app_state.active_mcp_streams[stream_id]
            logger.info(f"MCP Stream {stream_id}: Handler fully cleaned up and removed from active streams.")


# ----- FASTAPI APP -----

app = FastAPI(
    title="AIRA Hub",
    description="A decentralized registry for MCP and A2A tools",
    version="1.0.0",
    lifespan=lifespan
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS", "DELETE"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests_middleware(request: Request, call_next):
    """Middleware to log requests and add common headers"""
    start_time = time.time()
    path = request.url.path
    method = request.method
    client_host = request.client.host if request.client else "Unknown"

    logger.info(f"Request START: {method} {path} from {client_host}")

    try:
        response = await call_next(request)

        duration = time.time() - start_time
        status_code = response.status_code

        log_level = logging.INFO if status_code < 400 else logging.WARNING if status_code < 500 else logging.ERROR
        logger.log(log_level, f"Request END: {method} {path} - Status {status_code} ({duration:.4f}s)")

        return response
    except Exception as e:
        duration = time.time() - start_time
        logger.error(f"Request FAILED during processing: {method} {path} after {duration:.4f}s. Error: {e}",
                     exc_info=True)
        raise  # Re-raise to let FastAPI handle it


# ----- DEPENDENCIES -----

def get_storage_dependency(request: Request) -> MongoDBStorage:
    """Dependency to get storage from app state, with error handling"""
    logger.debug(f"Executing get_storage_dependency for {request.method} {request.url.path}")

    if getattr(request.app.state, 'storage_failed', False):
        logger.error("Storage dependency: Storage initialization failed")
        raise HTTPException(status_code=503, detail="Database unavailable (initialization failed)")

    storage_inst = getattr(request.app.state, 'storage', None)
    if not storage_inst or storage_inst.db is None:
        logger.error("Storage dependency: Storage not found or DB object is None")
        raise HTTPException(status_code=503, detail="Database unavailable")

    logger.debug("Storage dependency check successful")
    return storage_inst


# ----- ENDPOINTS -----

# --- Agent Registration ---

@app.post("/register", status_code=201, tags=["Agents"])
async def register_agent_endpoint(
        request_obj: Request,
        agent_payload: AgentRegistration,  # This is the incoming payload from the agent registering
        background_tasks: BackgroundTasks,
        storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    """
    Register an agent with the AIRA hub.
    If the agent has 'a2a' capability, its agent.json will be fetched
    and its A2A skills will be translated into MCP tools.
    """
    logger.info(f"Received registration request for agent: {agent_payload.name} at URL: {agent_payload.url}")

    # --- BEGIN A2A Skill Fetching and Translation ---
    if "a2a" in agent_payload.aira_capabilities and agent_payload.url:
        logger.info(f"Agent {agent_payload.name} has 'a2a' capability. Attempting to fetch its A2A Agent Card.")

        # Ensure the URL doesn't already end with the .well-known path
        base_url = agent_payload.url.rstrip('/')
        if base_url.endswith("/.well-known/agent.json"):  # Should not happen if agent_payload.url is base
            agent_card_url = base_url
        elif base_url.endswith("/.well-known"):
            agent_card_url = f"{base_url}/agent.json"
        else:
            agent_card_url = f"{base_url}/.well-known/agent.json"

        try:
            async with httpx.AsyncClient(timeout=HUB_TIMEOUT) as client:  # Use your HUB_TIMEOUT
                logger.debug(f"Fetching A2A Agent Card from: {agent_card_url}")
                response = await client.get(agent_card_url)
                response.raise_for_status()  # Raise an exception for bad status codes (4xx, 5xx)
                a2a_card_data = response.json()
                logger.info(f"Successfully fetched A2A Agent Card for {agent_payload.name} from {agent_card_url}")

                # We will replace agent_payload.mcp_tools with translated A2A skills.
                # If an agent is BOTH A2A and MCP native, you might want to merge,
                # but for now, A2A translation will define its MCP tools if A2A cap is present.
                translated_mcp_tools = []

                # Validate a2a_card_data (basic check for 'skills' list)
                if "skills" in a2a_card_data and isinstance(a2a_card_data["skills"], list):
                    for a2a_skill_dict in a2a_card_data["skills"]:
                        if not isinstance(a2a_skill_dict, dict) or \
                                not a2a_skill_dict.get("id") or \
                                not a2a_skill_dict.get("name"):
                            logger.warning(
                                f"Skipping malformed A2A skill from {agent_payload.name}'s card: {a2a_skill_dict}")
                            continue

                        # Create a unique and descriptive MCP tool name
                        # Example: "AgentName_A2A_SkillID"
                        mcp_tool_name = f"{agent_payload.name.replace(' ', '_')}_A2A_{a2a_skill_dict['id']}"

                        # The 'parameters' from A2ASkill should be a JSON schema object for inputSchema
                        input_schema_for_mcp = a2a_skill_dict.get("parameters", {"type": "object",
                                                                                 "properties": {}})  # Default if not present

                        # Create an MCPTool instance (using your Hub's MCPTool Pydantic model)
                        mcp_tool_def = MCPTool(
                            name=mcp_tool_name,
                            description=a2a_skill_dict.get("description", "No description provided."),
                            inputSchema=input_schema_for_mcp,
                            annotations={
                                "aira_bridge_type": "a2a",  # Critical for the Hub to know how to call it
                                "aira_a2a_target_skill_id": a2a_skill_dict['id'],
                                "aira_a2a_agent_url": agent_payload.url  # Base URL of the A2A agent
                            }
                        )
                        translated_mcp_tools.append(mcp_tool_def)

                    # Replace the agent_payload's mcp_tools with these translated ones
                    agent_payload.mcp_tools = translated_mcp_tools
                    logger.info(
                        f"Translated {len(agent_payload.mcp_tools)} A2A skills to MCP tools for {agent_payload.name}.")
                else:
                    logger.warning(
                        f"No 'skills' array found or it's not a list in A2A Agent Card for {agent_payload.name}. No MCP tools generated from A2A skills.")
                    agent_payload.mcp_tools = []  # Ensure it's empty if card was invalid

        except httpx.HTTPStatusError as e_http:
            logger.error(
                f"HTTP error fetching A2A Agent Card for {agent_payload.name} from {agent_card_url}: {e_http.response.status_code} - {e_http.response.text[:200]}",
                exc_info=True)
            # Decide: Do you fail registration or register without A2A-derived tools?
            # For now, let's register it but it won't have tools from A2A.
            agent_payload.mcp_tools = []  # Ensure no tools if card fetch failed
        except httpx.RequestError as e_req:
            logger.error(
                f"Request error fetching A2A Agent Card for {agent_payload.name} from {agent_card_url}: {e_req}",
                exc_info=True)
            agent_payload.mcp_tools = []
        except json.JSONDecodeError as e_json:
            logger.error(
                f"JSON decode error parsing A2A Agent Card for {agent_payload.name} from {agent_card_url}: {e_json}",
                exc_info=True)
            agent_payload.mcp_tools = []
        except Exception as e_card_proc:
            logger.error(f"Unexpected error processing A2A Agent Card for {agent_payload.name}: {e_card_proc}",
                         exc_info=True)
            agent_payload.mcp_tools = []
    # --- END A2A Skill Fetching and Translation ---

    # Check if agent already exists by URL (more reliable for updates)
    existing_agent = await storage_inst.get_agent_by_url(agent_payload.url)
    agent_to_save = agent_payload  # Start with the (potentially modified by A2A translation) payload
    status_message = "registered"

    if existing_agent:
        # Update existing agent's information but keep its ID and creation time
        agent_to_save.agent_id = existing_agent.agent_id
        agent_to_save.created_at = existing_agent.created_at

        # Preserve metrics if not being overwritten by the new payload
        if existing_agent.metrics and agent_to_save.metrics is None:
            agent_to_save.metrics = existing_agent.metrics

        status_message = "updated"
        logger.info(f"Agent {agent_to_save.name} (ID: {agent_to_save.agent_id}) found. Preparing update.")
    else:
        # It's a new agent, agent_id would have been defaulted by Pydantic or can be generated here
        # agent_to_save.agent_id = str(uuid.uuid4()) # Already handled by AgentRegistration model default_factory
        logger.info(
            f"New agent registration: {agent_to_save.name} at {agent_to_save.url} with new ID {agent_to_save.agent_id}")

    # Initialize metrics if not present from payload or existing
    if agent_to_save.metrics is None:
        agent_to_save.metrics = AgentMetrics()

    # Set status and timestamps for this registration/update event
    agent_to_save.status = AgentStatus.ONLINE
    agent_to_save.last_seen = time.time()

    # Save the agent (with potentially translated A2A skills as MCP tools)
    if not await storage_inst.save_agent(agent_to_save):
        logger.error(f"Failed to save agent {agent_to_save.name} (ID: {agent_to_save.agent_id}) to database.")
        raise HTTPException(status_code=500, detail="Failed to save agent to database")

    # Notify connected clients about the new/updated agent
    connection_manager = getattr(request_obj.app.state, 'connection_manager', None)
    if connection_manager:
        event_type = "agent_registered" if status_message == "registered" and not existing_agent else "agent_updated"
        background_tasks.add_task(
            connection_manager.broadcast_event,
            event_type,
            {
                "agent_id": agent_to_save.agent_id,
                "name": agent_to_save.name,
                "url": agent_to_save.url,
                "status": agent_to_save.status.value,
                "mcp_tools_count": len(agent_to_save.mcp_tools),  # Include tool count
                "a2a_skills_count": len(agent_to_save.a2a_skills)
            }
        )

    logger.info(f"Agent {status_message} process completed for: {agent_to_save.name} (ID: {agent_to_save.agent_id})")

    return {
        "status": status_message,
        "agent_id": agent_to_save.agent_id,
        "url": agent_to_save.url,
        "discovered_mcp_tools_from_a2a": len(agent_to_save.mcp_tools) if "a2a" in agent_to_save.aira_capabilities else 0
    }


@app.post("/heartbeat/{agent_id}", tags=["Agents"])
async def heartbeat_endpoint(
        request: Request,
        agent_id: str,
        storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    """
    Update agent heartbeat

    This endpoint allows agents to send heartbeats to indicate they are still active.
    """
    # Get existing agent
    agent = await storage_inst.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Update heartbeat timestamp
    now = time.time()
    updated = await storage_inst.update_agent_heartbeat(agent_id, now)

    if updated:
        # Update agent status to ONLINE if needed
        status_changed = False
        if agent.status != AgentStatus.ONLINE:
            agent.status = AgentStatus.ONLINE
            await storage_inst.save_agent(agent) # Save the updated agent status
            status_changed = True
            logger.info(f"Agent {agent_id} status changed to ONLINE via heartbeat")

        # Notify clients if status changed
        if status_changed and hasattr(request.app.state, 'connection_manager'):
            await request.app.state.connection_manager.broadcast_event(
                "agent_status_updated", # More specific event name
                {
                    "agent_id": agent_id,
                    "name": agent.name,
                    "url": agent.url,
                    "status": AgentStatus.ONLINE.value
                }
            )

    return {"status": "ok"}


# --- Agent Discovery ---

@app.get("/agents", tags=["Discovery"])
async def list_agents_endpoint(
        request: Request,
        status: Optional[AgentStatus] = Query(None, description="Filter by agent status"),
        category: Optional[str] = Query(None, description="Filter by agent category"),
        tag: Optional[str] = Query(None, description="Filter by a specific agent tag"),
        capability: Optional[str] = Query(None, description="Filter by a specific agent capability"),
        offset: int = Query(0, ge=0, description="Offset for pagination"), # <--- Corrected
        limit: int = Query(100, ge=1, le=1000, description="Limit for pagination (max 1000)"), # <--- Corrected
        storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    """
    List registered agents

    This endpoint returns a list of registered agents with optional filtering.
    """
    # Build query
    query = {}

    if status:
        query["status"] = status.value
    if category:
        query["category"] = category
    if tag:
        query["tags"] = tag
    if capability:
        query["aira_capabilities"] = capability

    # Get total count
    total = await storage_inst.count_agents(query)

    # Get agents with pagination
    agents = await storage_inst.search_agents(query, offset, limit)

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "agents": agents
    }


@app.get("/agents/{agent_id}", tags=["Discovery"])
async def get_agent_endpoint(
        request: Request,
        agent_id: str,
        storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    """
    Get details for a specific agent

    This endpoint returns detailed information about a specific agent.
    """
    agent = await storage_inst.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    return agent


@app.delete("/agents/{agent_id}", tags=["Agents"])
async def unregister_agent_endpoint(
        request: Request,
        agent_id: str,
        background_tasks: BackgroundTasks,
        storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    """
    Unregister an agent

    This endpoint allows agents to unregister themselves from the hub.
    """
    # Get the agent first to include its info in the notification
    agent = await storage_inst.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Delete the agent
    deleted = await storage_inst.delete_agent(agent_id)

    if deleted:
        # Notify connected clients
        if hasattr(request.app.state, 'connection_manager'):
            background_tasks.add_task(
                request.app.state.connection_manager.broadcast_event,
                "agent_unregistered",
                {
                    "agent_id": agent_id,
                    "name": agent.name,
                    "url": agent.url
                }
            )

        logger.info(f"Agent unregistered: {agent.name} ({agent.url}) with ID {agent_id}")
        return {"status": "unregistered", "agent_id": agent_id}
    else:
        # This might happen if agent was deleted between get_agent and delete_agent
        logger.warning(f"Attempted to unregister agent {agent_id} but deletion failed (possibly already deleted).")
        raise HTTPException(status_code=404, detail="Agent not found or failed to delete")


# --- Tools and Resources ---

@app.get("/tools", tags=["Tools"])
async def list_tools_endpoint(
        request: Request,
        agent_id: Optional[str] = None,
        storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    """
    List available tools

    This endpoint returns a list of all available tools from ONLINE agents,
    optionally filtered by agent.
    """
    if agent_id:
        # Get tools from a specific agent
        agent = await storage_inst.get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")

        tools = []
        if agent.status != AgentStatus.ONLINE:
            logger.info(f"Attempted to list tools for offline agent {agent_id}")
            return {"tools": []}

        for tool in agent.mcp_tools:
            tools.append({
                "name": tool.name,
                "description": tool.description,
                "agent": agent.name,
                "agent_id": agent.agent_id,
                "inputSchema": tool.inputSchema,
                "annotations": tool.annotations
            })
    else:
        # Get tools from all online agents
        tools = await storage_inst.list_tools()

    return {"tools": tools}


@app.get("/tags", tags=["Discovery"])
async def list_tags_endpoint(
        request: Request,
        type: str = "agent",  # "agent", "skill", or "tool" (tool tags are not currently modeled)
        storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    """
    List all tags

    This endpoint returns a list of all unique tags used by agents, skills, or tools.
    """
    # Get all agents
    agents = await storage_inst.list_agents()

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
        # MCPTool doesn't have tags in current model
        logger.warning("Tag type 'tool' requested, but MCPTool model does not currently support explicit tags. Returning empty list.")
    else:
        raise HTTPException(status_code=400, detail="Invalid tag type. Must be 'agent' or 'skill'.")

    return {"tags": sorted(list(tags))}


@app.get("/categories", tags=["Discovery"])
async def list_categories_endpoint(
        request: Request,
        storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    """
    List all categories

    This endpoint returns a list of all unique categories used by agents.
    """
    # Get all agents
    agents = await storage_inst.list_agents()

    # Collect categories
    categories = set()
    for agent in agents:
        if agent.category:
            categories.add(agent.category)

    return {"categories": sorted(list(categories))}


# --- Status and System ---

@app.get("/status", tags=["System"])
async def system_status_endpoint(
        request: Request
):
    """
    Get system status

    This endpoint returns the current status of the AIRA hub.
    """
    cm = getattr(request.app.state, 'connection_manager', None)
    msm = getattr(request.app.state, 'mcp_session_manager', None)

    connected_clients_count = len(cm.get_all_connections()) if cm else 0
    active_sessions_count = len(msm.active_sessions) if msm else 0


    if (getattr(request.app.state, 'storage_failed', False) or
            not hasattr(request.app.state, 'storage') or
            request.app.state.storage is None or
            request.app.state.storage.db is None):
        # Return a degraded status if DB is not available
        return {
            "status": "degraded - database unavailable",
            "uptime": time.time() - request.app.state.start_time if hasattr(request.app.state, 'start_time') else 0,
            "registered_agents": 0,
            "online_agents": 0,
            "available_tools": 0,
            "connected_clients": connected_clients_count,
            "active_sessions": active_sessions_count,
            "version": "1.0.0"
        }

    storage_inst: MongoDBStorage = request.app.state.storage

    # Get all agents
    agents = await storage_inst.list_agents() # This could be slow if many agents; consider direct count queries

    # Count online agents (more efficiently)
    now = time.time()
    # This online_agents count method is not efficient for large numbers.
    # A direct DB query would be better for status. For now, keeping as is from original.
    online_agents_list = [a for a in agents if (now - a.last_seen) <= DEFAULT_HEARTBEAT_TIMEOUT and a.status == AgentStatus.ONLINE]


    # Count tools (only from online agents)
    tools = await storage_inst.list_tools() # This lists tools, not just counts them

    return {
        "status": "healthy",
        "uptime": time.time() - request.app.state.start_time,
        "registered_agents": len(agents),
        "online_agents": len(online_agents_list),
        "available_tools": len(tools), # Number of unique tool definitions
        "connected_clients": connected_clients_count,
        "active_sessions": active_sessions_count,
        "version": "1.0.0"
    }


@app.get("/health", include_in_schema=False)
async def health_check_endpoint(request: Request):
    """
    Health check endpoint

    This endpoint returns a simple health check for load balancers and monitoring.
    """
    db_status = "ok"
    storage_inst = getattr(request.app.state, 'storage', None)

    if not storage_inst or not storage_inst.mongo_db_client:
        db_status = "error - not initialized or client unavailable"
    else:
        try:
            await storage_inst.mongo_db_client.admin.command('ping')
        except Exception as e:
            logger.warning(f"Health check: DB ping failed: {e}")
            db_status = "error - unreachable"

    overall_status = "ok" if db_status == "ok" else "degraded"

    return {"status": overall_status, "database": db_status}


# --- MCP Streamable HTTP Endpoint ---

@app.post("/mcp/stream", tags=["MCP (Streamable HTTP)"])
async def mcp_stream_endpoint(
        request: Request,
        storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    """
    MCP Streamable HTTP endpoint

    This endpoint implements the MCP Streamable HTTP protocol.
    It supports both request/response and streaming modes.
    """
    stream_id = str(uuid.uuid4())
    client_host = request.client.host if request.client else "Unknown"
    logger.info(f"MCP Stream {stream_id}: New connection from {client_host}")

    # --- Process Initial Request Body ---
    initial_messages = []
    try:
        initial_body_bytes = await request.body()

        if initial_body_bytes.strip():
            initial_body_str = initial_body_bytes.decode('utf-8')
            logger.debug(f"MCP Stream {stream_id}: Initial POST body: {initial_body_str}")

            parsed_initial_body = json.loads(initial_body_str)

            if isinstance(parsed_initial_body, list): # Batch request
                initial_messages.extend(parsed_initial_body)
            elif isinstance(parsed_initial_body, dict): # Single request or error
                initial_messages.append(parsed_initial_body)
            else:
                logger.warning(f"MCP Stream {stream_id}: Invalid initial POST body type: {type(parsed_initial_body)}")
                # This error should be sent as a direct JSONResponse, not through the stream handler
                # if the stream isn't even established yet.
                # However, to match existing logic, we'll pass it for the handler to send.
                initial_messages.append({
                    "jsonrpc": "2.0", # Make it look like an MCP error response
                    "id": None,
                    "error": {
                        "code": -32700,
                        "message": "Invalid initial request format (not object or array)"
                    }
                })
        else:
            logger.debug(f"MCP Stream {stream_id}: Initial POST body empty")

    except json.JSONDecodeError:
        logger.error(f"MCP Stream {stream_id}: Invalid JSON in initial POST body", exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error in initial request"}
            }
        )
    except Exception as e_init:
        logger.error(f"MCP Stream {stream_id}: Error processing initial body: {e_init}", exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32603, "message": "Internal error processing initial request"}
            }
        )

    # --- Start Background Handler and Response Stream ---
    send_queue = asyncio.Queue()

    # Pass initial messages to the handler
    handler_task = asyncio.create_task(
        mcp_stream_handler(
            stream_id, initial_messages, request.stream(), send_queue, storage_inst, request.app.state
        )
    )
    if hasattr(request.app.state, 'active_mcp_streams'):
        request.app.state.active_mcp_streams[stream_id] = handler_task

    # Generator for StreamingResponse
    async def response_generator():
        try:
            while True:
                item = await send_queue.get()

                if item is None:
                    logger.info(f"MCP Stream {stream_id}: Closing response generator (received None sentinel)")
                    break

                if isinstance(item, MCPResponseModel):
                    try:
                        json_resp_str = item.model_dump_json(exclude_none=True)
                        logger.debug(f"MCP Stream {stream_id}: SEND: {json_resp_str}")
                        yield json_resp_str.encode('utf-8') + STREAM_SEPARATOR
                    except Exception as e_ser:
                        logger.error(f"MCP Stream {stream_id}: Error serializing MCPResponseModel: {e_ser}", exc_info=True)
                        # Optionally send a generic error back if serialization fails critically
                else:
                    logger.warning(f"MCP Stream {stream_id}: Unknown item type in send_queue: {type(item)}")

                send_queue.task_done()

        except asyncio.CancelledError:
            logger.info(f"MCP Stream {stream_id}: Response generator cancelled")
        except Exception as e_gen_outer:
            logger.error(f"MCP Stream {stream_id}: Error in response generator: {e_gen_outer}", exc_info=True)
        finally:
            logger.info(f"MCP Stream {stream_id}: Response generator finished")
            # Ensure handler task is cleaned up if generator finishes/errors or client disconnects
            if handler_task and not handler_task.done():
                logger.info(f"MCP Stream {stream_id}: Cancelling handler task from response_generator finally block.")
                handler_task.cancel()
                try:
                    await handler_task # Wait for cancellation to complete
                except asyncio.CancelledError:
                    logger.info(f"MCP Stream {stream_id}: Handler task successfully cancelled by response_generator.")
                except Exception as e_task_cleanup:
                    logger.error(f"MCP Stream {stream_id}: Error during handler task cleanup in response_generator: {e_task_cleanup}", exc_info=True)


    return StreamingResponse(
        response_generator(),
        media_type="application/json-seq" # Correct MIME type for JSON Text Sequences
    )


# --- A2A Protocol Endpoints ---

@app.post("/a2a/discover", tags=["A2A"])
async def discover_a2a_agents_endpoint(
        request_obj: Request,
        query: DiscoverQuery,
        storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    """
    Discover agents based on criteria using A2A protocol

    This endpoint allows clients to discover agents based on various criteria.
    """
    # Build query
    query_dict = {}

    if query.status:
        query_dict["status"] = query.status.value
    if query.category:
        query_dict["category"] = query.category
    if query.agent_tags:
        query_dict["tags"] = {"$in": query.agent_tags} # Match any of the tags
    if query.capabilities:
        query_dict["aira_capabilities"] = {"$all": query.capabilities} # Match all capabilities

    # Handle skill criteria
    skill_query_parts = []
    if query.skill_id:
        skill_query_parts.append({"a2a_skills.id": query.skill_id})
    if query.skill_tags:
        skill_query_parts.append({"a2a_skills.tags": {"$in": query.skill_tags}}) # Match any skill tag

    if skill_query_parts:
        # Combine with other query parts
        # If $and already exists, append to it, otherwise create it
        if "$and" not in query_dict:
            query_dict["$and"] = []

        query_dict["$and"].append({"aira_capabilities": "a2a"})  # Ensure agent supports A2A
        query_dict["$and"].append({"$or": skill_query_parts})  # Match any of the skill criteria
    elif query.skill_id is not None or query.skill_tags is not None: # if skill criteria were provided but empty
        query_dict["aira_capabilities"] = "a2a" # Still ensure agent has a2a if skill search attempted


    # Get total count and agents
    total = await storage_inst.count_agents(query_dict)
    agents_list = await storage_inst.search_agents(query_dict, query.offset, query.limit)

    return {
        "total": total,
        "offset": query.offset,
        "limit": query.limit,
        "agents": agents_list
    }


@app.get("/a2a/skills", tags=["A2A"])
async def list_a2a_skills_endpoint(
        request_obj: Request,
        agent_id: Optional[str] = None,
        tag: Optional[str] = None,
        storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    """
    List available A2A skills

    This endpoint returns a list of all available A2A skills,
    optionally filtered by agent or tag.
    """
    skills_list = []

    # Build query
    query_dict = {"aira_capabilities": "a2a", "status": AgentStatus.ONLINE.value}
    if agent_id:
        query_dict["agent_id"] = agent_id

    # Get agents
    agents_found = await storage_inst.search_agents(query_dict)

    # Collect skills
    for agent_obj in agents_found:
        for skill in agent_obj.a2a_skills:
            if tag is None or (skill.tags and tag in skill.tags):
                skills_list.append({
                    "id": skill.id,
                    "name": skill.name,
                    "description": skill.description,
                    "version": skill.version,
                    "agent_name": agent_obj.name, # Added for clarity
                    "agent_id": agent_obj.agent_id,
                    "tags": skill.tags,
                    "parameters": skill.parameters,
                    "examples": skill.examples
                })

    return {"skills": skills_list}


@app.post("/a2a/tasks/send", tags=["A2A"], status_code=status.HTTP_202_ACCEPTED, response_model=A2ATask)
async def a2a_send_task_endpoint(
        request_obj: Request,
        # Using a Pydantic model for the request body is cleaner
        task_submission: Dict[str, Any], # Example: {"agent_id": "...", "skill_id": "...", "message": {...}, "session_id": "..."}
        storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    """
    Send a task to an agent using A2A protocol

    This endpoint allows clients to send tasks to agents using the A2A protocol.
    The hub records the task and is responsible for forwarding it (actual forwarding TBD).
    """
    # Parse request data from task_submission
    agent_id = task_submission.get("agent_id")
    skill_id = task_submission.get("skill_id")
    task_message = task_submission.get("message")
    session_id = task_submission.get("session_id") # Optional

    # Validate request
    if not all([agent_id, skill_id, task_message]) or not isinstance(task_message, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="agent_id, skill_id, and a valid 'message' (object) are required"
        )

    # Get agent
    agent = await storage_inst.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    # Check agent status and capabilities
    if agent.status != AgentStatus.ONLINE:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Agent {agent.name} is offline")
    if "a2a" not in agent.aira_capabilities:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Agent {agent.name} does not support A2A")

    # Verify skill exists on the agent
    found_skill = next((s for s in agent.a2a_skills if s.id == skill_id), None)
    if not found_skill:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Skill {skill_id} not found on agent {agent.name}")

    # Create task
    task_obj = A2ATask(
        agent_id=agent_id,
        skill_id=skill_id,
        session_id=session_id,
        original_message=task_message,
        current_status=A2ATaskStatusUpdate(state=A2ATaskState.SUBMITTED, message="Task submitted to hub."),
        history=[{"role": "user", "timestamp": datetime.now(timezone.utc).isoformat(), "content": task_message}]
    )

    # Save task
    if not await storage_inst.save_a2a_task(task_obj):
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to save task")

    logger.info(f"A2A Task {task_obj.id} submitted for agent {agent.name} ({agent_id}), skill {skill_id}")

    # TODO: Implement actual task forwarding to the target agent.
    # This would involve an HTTP call to an A2A endpoint on the agent.
    logger.warning(f"A2A Task {task_obj.id}: Forwarding to agent {agent_id} NOT IMPLEMENTED in this version.")

    return task_obj


@app.get("/a2a/tasks/{task_id}", tags=["A2A"], response_model=A2ATask)
async def a2a_get_task_endpoint(
        request_obj: Request,
        task_id: str,
        storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    """
    Get A2A task status

    This endpoint allows clients to check the status of an A2A task.
    """
    task_obj = await storage_inst.get_a2a_task(task_id)
    if not task_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="A2A Task not found")

    # Mock state transition (for demo purposes - REMOVE FOR PRODUCTION)
    # This logic should be driven by the agent updating the task status back to the hub.
    if (task_obj.current_status.state == A2ATaskState.SUBMITTED and
            (datetime.now(timezone.utc) - task_obj.created_at).total_seconds() > 5 and
            (datetime.now(timezone.utc) - task_obj.updated_at).total_seconds() > 5): # Avoid rapid updates
        logger.info(f"A2A Task {task_obj.id}: Mock transition to WORKING (DEMO ONLY)")
        task_obj.current_status = A2ATaskStatusUpdate(
            state=A2ATaskState.WORKING,
            message="Task processing initiated by agent (mock update)."
        )
        task_obj.history.append({
            "role": "system", # Or "agent"
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "status_update",
            "content": task_obj.current_status.message
        })
        task_obj.updated_at = datetime.now(timezone.utc)
        await storage_inst.save_a2a_task(task_obj) # Save the updated task

    return task_obj


# --- Analytics Endpoints ---

@app.get("/analytics/summary", tags=["Analytics"])
async def analytics_summary_endpoint(
        request_obj: Request,
        storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    """
    Get analytics summary

    This endpoint returns a summary of AIRA hub analytics.
    """
    # Get all agents
    agents_list = await storage_inst.list_agents()

    # Count agents by status
    status_counts = {s.value: 0 for s in AgentStatus}
    capability_counts = {}
    total_tools_count = 0
    total_skills_count = 0
    tag_counts_map = {}

    # Process agent data
    for agent_obj in agents_list:
        # Count by status
        status_counts[agent_obj.status.value] = status_counts.get(agent_obj.status.value, 0) + 1

        # Count capabilities
        for cap in agent_obj.aira_capabilities:
            capability_counts[cap] = capability_counts.get(cap, 0) + 1

        # Count tools and skills
        total_tools_count += len(agent_obj.mcp_tools)
        total_skills_count += len(agent_obj.a2a_skills)

        # Count agent tags
        for tag_item in agent_obj.tags:
            tag_counts_map[tag_item] = tag_counts_map.get(tag_item, 0) + 1

        # Count skill tags
        for skill in agent_obj.a2a_skills:
            for skill_tag in skill.tags:
                tag_counts_map[skill_tag] = tag_counts_map.get(skill_tag, 0) + 1

    # Get top tags
    top_tags_dict = dict(sorted(tag_counts_map.items(), key=lambda item: item[1], reverse=True)[:10])

    # Count recent agents
    now = time.time()
    agents_added_today_count = len([a for a in agents_list if (now - a.created_at) < 86400])  # 86400 seconds in a day

    # Get A2A task counts
    num_a2a_tasks = 0
    num_completed_a2a_tasks = 0
    if storage_inst.db: # Ensure DB is available
        num_a2a_tasks = await storage_inst.db.tasks.count_documents({})
        num_completed_a2a_tasks = await storage_inst.db.tasks.count_documents(
            {"current_status.state": A2ATaskState.COMPLETED.value}
        )

    return {
        "total_agents": len(agents_list),
        "status_counts": status_counts,
        "capability_counts": capability_counts,
        "total_tools": total_tools_count,
        "total_skills": total_skills_count,
        "top_tags": top_tags_dict,
        "agents_added_today": agents_added_today_count,
        "total_a2a_tasks_recorded": num_a2a_tasks,
        "completed_a2a_tasks_recorded": num_completed_a2a_tasks
    }


@app.get("/analytics/activity", tags=["Analytics"])
async def analytics_activity_endpoint(
        request_obj: Request,
        days: int = Query(7, ge=1, le=30), # Add validation for days
        storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    """
    Get activity analytics

    This endpoint returns activity data for the specified number of days.
    """
    activity_data_list = []
    now_ts = time.time()
    day_seconds = 86400  # seconds in a day

    # Get all agents (can be optimized for very large datasets)
    agents_all = await storage_inst.list_agents()

    # Generate activity data for each day
    for i in range(days):
        range_end_ts = now_ts - (i * day_seconds)
        range_start_ts = range_end_ts - day_seconds
        day_date_str = datetime.fromtimestamp(range_start_ts, timezone.utc).strftime("%Y-%m-%d")

        # Count registrations for this day
        new_registrations_count = len([a for a in agents_all
                                       if range_start_ts <= a.created_at < range_end_ts])

        # Approximate active agents for this day
        # An agent is active if:
        # 1. It was seen during this day OR
        # 2. It's currently ONLINE and its last_seen was before this day but within the heartbeat timeout from now
        active_agents_approx_count = 0
        for a in agents_all:
            if range_start_ts <= a.last_seen < range_end_ts: # Seen within the day
                 active_agents_approx_count +=1
            elif a.status == AgentStatus.ONLINE and a.last_seen < range_start_ts and \
                 (now_ts - a.last_seen) <= DEFAULT_HEARTBEAT_TIMEOUT : # Online and seen recently enough
                 active_agents_approx_count +=1


        # Count A2A tasks created for this day
        a2a_tasks_created_count = 0
        if storage_inst.db:
            a2a_tasks_created_count = await storage_inst.db.tasks.count_documents({
                "created_at": {
                    "$gte": datetime.fromtimestamp(range_start_ts, timezone.utc),
                    "$lt": datetime.fromtimestamp(range_end_ts, timezone.utc)
                }
            })

        # Add day data
        activity_data_list.append({
            "date": day_date_str,
            "new_agent_registrations": new_registrations_count,
            "approx_active_agents": active_agents_approx_count,
            "a2a_tasks_created": a2a_tasks_created_count
        })

    # Return data in chronological order (oldest to newest)
    return {"activity": list(reversed(activity_data_list))}


# --- Admin Endpoints ---

class AdminSyncPayload(BaseModel):
    hub_urls: List[str]

@app.post("/admin/sync_agents", tags=["Admin"])
async def admin_sync_agents_endpoint(
        payload: AdminSyncPayload,
        storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    """
    Sync agents with remote registries

    This endpoint allows administrators to sync agents with other AIRA hubs.
    It fetches agent lists from specified hub URLs and registers new agents locally.
    """
    hub_urls = payload.hub_urls
    results_map = {}

    # Use a shared HTTP client for efficiency
    async with httpx.AsyncClient(timeout=30.0) as http_client:
        for hub_url_str in hub_urls:
            if not hub_url_str.startswith(('http://', 'https://')):
                results_map[hub_url_str] = {"status": "error", "message": "Invalid hub URL format."}
                logger.warning(f"Invalid hub URL for sync: {hub_url_str}")
                continue
            try:
                # Construct URL for agent list (usually /agents)
                sync_url_str = urllib.parse.urljoin(hub_url_str.rstrip('/') + '/', "agents")
                logger.info(f"Syncing agents from {sync_url_str}")

                response = await http_client.get(sync_url_str)

                if response.status_code != 200:
                    logger.warning(f"Failed to get agents from {hub_url_str}: HTTP {response.status_code} - {response.text}")
                    results_map[hub_url_str] = {
                        "status": "error",
                        "message": f"Failed to get agents: HTTP {response.status_code}",
                        "details": response.text[:500] # Truncate long error messages
                    }
                    continue

                remote_data_dict = response.json()
                # Assuming the remote /agents endpoint returns a list under "agents" key
                remote_agents_list_data = remote_data_dict.get("agents", [])
                if not isinstance(remote_agents_list_data, list):
                    logger.warning(f"Invalid agent data format from {hub_url_str}. Expected list under 'agents'. Got: {type(remote_agents_list_data)}")
                    results_map[hub_url_str] = {"status": "error", "message": "Invalid agent data format from remote hub."}
                    continue


                registered_c = 0
                skipped_c = 0
                failed_c = 0

                for agent_data_dict in remote_agents_list_data:
                    if not isinstance(agent_data_dict, dict):
                        logger.warning(f"Skipping non-dict agent data from {hub_url_str}: {agent_data_dict}")
                        failed_c +=1
                        continue
                    try:
                        # Basic validation
                        if not agent_data_dict.get('url') or not agent_data_dict.get('name'):
                            logger.warning(f"Skipping remote agent from {hub_url_str}, missing URL/Name: {agent_data_dict.get('url')}")
                            failed_c += 1
                            continue

                        # Check if agent already exists by URL
                        if await storage_inst.get_agent_by_url(agent_data_dict['url']):
                            skipped_c += 1
                            logger.debug(f"Agent from {agent_data_dict['url']} already exists, skipping sync.")
                            continue

                        # Create agent model from received data (ensure all fields are optional or have defaults)
                        # This part might need more robust handling if remote schemas differ significantly
                        agent_to_create = AgentRegistration(
                            url=agent_data_dict['url'],
                            name=agent_data_dict['name'],
                            description=agent_data_dict.get('description'),
                            version=agent_data_dict.get('version', "1.0.0"),
                            mcp_tools=[MCPTool(**t) for t in agent_data_dict.get('mcp_tools', []) if isinstance(t, dict)],
                            a2a_skills=[A2ASkill(**s) for s in agent_data_dict.get('a2a_skills', []) if isinstance(s, dict)],
                            aira_capabilities=agent_data_dict.get('aira_capabilities', []),
                            tags=agent_data_dict.get('tags', []),
                            category=agent_data_dict.get('category'),
                            provider=agent_data_dict.get('provider'),
                            mcp_url=agent_data_dict.get('mcp_url'),
                            mcp_sse_url=agent_data_dict.get('mcp_sse_url'),
                            mcp_stream_url=agent_data_dict.get('mcp_stream_url'),
                            # Remote agent is assumed ONLINE upon sync, last_seen will be now
                            status=AgentStatus.ONLINE,
                            last_seen=time.time()
                        )

                        await storage_inst.save_agent(agent_to_create)
                        registered_c += 1
                        logger.info(f"Synced new agent: {agent_to_create.name} from {hub_url_str}")

                    except ValidationError as e_val:
                        agent_name_preview = agent_data_dict.get('name', agent_data_dict.get('url', 'Unknown'))
                        logger.error(f"Validation error processing remote agent '{agent_name_preview}' from {hub_url_str}: {e_val}", exc_info=False) # exc_info=False to avoid overly verbose logs for common validation issues
                        failed_c += 1
                    except Exception as e_agent_proc:
                        agent_name_preview = agent_data_dict.get('name', agent_data_dict.get('url', 'Unknown'))
                        logger.error(f"Error processing remote agent '{agent_name_preview}' from {hub_url_str}: {e_agent_proc}", exc_info=True)
                        failed_c += 1

                results_map[hub_url_str] = {
                    "status": "success",
                    "registered_new": registered_c,
                    "skipped_existing": skipped_c,
                    "failed_to_process": failed_c,
                    "total_remote_agents_processed": len(remote_agents_list_data)
                }

            except httpx.RequestError as e_req_outer:
                logger.warning(f"Network error during sync with {hub_url_str}: {e_req_outer}")
                results_map[hub_url_str] = {"status": "error", "message": f"Network error: {str(e_req_outer)}"}
            except json.JSONDecodeError as e_json_outer:
                logger.warning(f"JSON decode error from {hub_url_str}: {e_json_outer}")
                results_map[hub_url_str] = {"status": "error", "message": "Failed to parse JSON response from remote hub."}
            except Exception as e_hub_outer:
                logger.error(f"Unexpected error during sync with {hub_url_str}: {e_hub_outer}", exc_info=True)
                results_map[hub_url_str] = {"status": "error", "message": f"Internal processing error: {str(e_hub_outer)}"}
    return {"sync_results": results_map}


class AdminCleanupPayload(BaseModel):
    agent_threshold_seconds: int = Field(DEFAULT_HEARTBEAT_TIMEOUT * 3, ge=60) # Default 15 mins
    session_threshold_seconds: int = Field(3600, ge=60) # Default 1 hour

@app.post("/admin/cleanup", tags=["Admin"])
async def admin_cleanup_endpoint(
        payload: AdminCleanupPayload,
        request_obj: Request, # For app.state access
        storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    """
    Clean up stale data

    This endpoint allows administrators to clean up stale agents (offline for too long)
    and stale MCP sessions.
    """
    agent_threshold_sec = payload.agent_threshold_seconds
    session_threshold_sec = payload.session_threshold_seconds
    now = time.time()

    # Find stale agents (agents whose last_seen is older than threshold)
    # This could also include agents marked OFFLINE explicitly if desired.
    # For now, only time-based.
    agents_all = await storage_inst.list_agents()
    agent_ids_to_remove_list = [
        a.agent_id for a in agents_all
        if (now - a.last_seen) > agent_threshold_sec
    ]

    agent_removed_count = 0
    delete_errors = []

    if agent_ids_to_remove_list:
        logger.info(f"Admin Cleanup: Found {len(agent_ids_to_remove_list)} stale agents to remove.")
        delete_results = await asyncio.gather(
            *[storage_inst.delete_agent(aid) for aid in agent_ids_to_remove_list],
            return_exceptions=True
        )

        for i, res in enumerate(delete_results):
            if res is True:
                agent_removed_count += 1
            elif isinstance(res, Exception):
                err_msg = f"Error deleting agent {agent_ids_to_remove_list[i]}: {str(res)}"
                logger.error(err_msg, exc_info=True)
                delete_errors.append(err_msg)
            # If res is False, delete_agent handled it (e.g., agent already gone)
    else:
        logger.info("Admin Cleanup: No stale agents found based on threshold.")


    # Clean up stale MCP sessions
    session_removed_count = 0
    mcp_session_manager = getattr(request_obj.app.state, 'mcp_session_manager', None)
    if mcp_session_manager:
        session_removed_count = mcp_session_manager.cleanup_stale_sessions(session_threshold_sec)
    else:
        logger.warning("Admin Cleanup: MCP Session Manager not found on app state.")

    return {
        "status": "success" if not delete_errors else "partial_success",
        "agents_removed": agent_removed_count,
        "agent_removal_errors": len(delete_errors),
        "error_details": delete_errors,
        "sessions_removed": session_removed_count
    }

class AdminBroadcastPayload(BaseModel):
    message: str
    event_type: str = "admin_message"

@app.post("/admin/broadcast", tags=["Admin"])
async def admin_broadcast_endpoint(
        payload: AdminBroadcastPayload,
        request_obj: Request, # For app.state access
        background_tasks: BackgroundTasks
):
    """
    Broadcast a message to all connected UI clients

    This endpoint allows administrators to broadcast a message (event) to all
    clients connected to the hub's event stream (e.g., for UI updates).
    """
    connection_mgr = getattr(request_obj.app.state, 'connection_manager', None)
    if not connection_mgr:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Connection manager not available")

    broadcast_data_dict = {
        "message": payload.message,
        "timestamp": time.time()
    }

    # Queue broadcast as background task
    background_tasks.add_task(
        connection_mgr.broadcast_event,
        payload.event_type,
        broadcast_data_dict
    )

    recipients_queued_count = len(connection_mgr.get_all_connections())
    logger.info(f"Admin broadcast '{payload.event_type}' queued for {recipients_queued_count} clients with message: {payload.message[:100]}...")

    return {"status": "broadcast_queued", "recipients_estimated": recipients_queued_count}


# --- UI Endpoint ---

@app.get("/ui", tags=["UI"], include_in_schema=False) # Typically UIs are served statically or redirect
async def ui_dashboard_endpoint(request: Request):
    """
    AIRA Hub Dashboard UI (Placeholder)

    This endpoint is a placeholder for serving the main dashboard UI.
    In a production setup, this would likely serve an HTML file or redirect.
    """
    # For now, just return a message.
    # A real implementation might use:
    # from fastapi.responses import HTMLResponse
    # return HTMLResponse(content="<html><body><h1>AIRA Hub UI (Coming Soon)</h1></body></html>", status_code=200)
    return JSONResponse(content={
        "message": "AIRA Hub UI not implemented yet. Please refer to the API documentation at /docs or /redoc."
    })


# --- Custom Agent Connect Endpoints ---
# These seem to be for specific agent integration patterns, not standard MCP/A2A.
# They are kept for compatibility if they were used by some agents.

class CustomConnectStreamParams(BaseModel):
    agent_url: str = Field(..., description="URL of the agent's SSE stream.")
    name: str = Field(..., description="Name to register the agent under if new.")
    aira_capabilities: Optional[str] = Field(None, description="Comma-separated list of capabilities (e.g., 'mcp,a2a').")


@app.post("/connect/stream", tags=["Custom Agent Connect (Legacy)"])
async def connect_stream_custom_endpoint(
        params: CustomConnectStreamParams = Depends(), # Using Depends for query/form params
        request_obj: Request = Request, # Allow FastAPI to inject request if needed by generator
        storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    """
    Connect to an agent's SSE stream and potentially proxy it. (Legacy)

    NOTE: This is a custom endpoint, likely for older agent integrations.
    Prefer standard /register and MCP/A2A protocols.
    This endpoint tries to register/update an agent based on its SSE stream.
    """
    logger.warning("/connect/stream is a custom, non-standard endpoint. Prefer standard registration.")

    agent_url = params.agent_url
    name = params.name
    capabilities_list = params.aira_capabilities.split(",") if params.aira_capabilities else []

    async def event_generator_custom():
        async with httpx.AsyncClient(timeout=None) as http_client_custom: # timeout=None for long-lived streams
            try:
                logger.info(f"Custom Connect: Attempting to proxy SSE from agent: {agent_url}")

                async with http_client_custom.stream("GET", agent_url, headers={"Accept": "text/event-stream"}) as response_stream:
                    if response_stream.status_code != 200:
                        err_msg = f"Custom Connect: Agent stream at {agent_url} failed with HTTP {response_stream.status_code}"
                        logger.error(err_msg)
                        yield f"event: error\ndata: {json.dumps({'error': err_msg})}\n\n"
                        return

                    # Register/update agent based on this stream connection
                    existing_agent = await storage_inst.get_agent_by_url(agent_url)
                    agent_id_for_stream: str

                    if not existing_agent:
                        agent_obj_new = AgentRegistration(
                            url=agent_url, name=name,
                            description=f"Registered via custom SSE endpoint: {agent_url}",
                            aira_capabilities=capabilities_list,
                            mcp_sse_url=agent_url, # Mark that it uses this SSE URL
                            status=AgentStatus.ONLINE, last_seen=time.time()
                        )
                        await storage_inst.save_agent(agent_obj_new)
                        agent_id_for_stream = agent_obj_new.agent_id
                        logger.info(f"Custom Connect: Registered new agent via SSE: {agent_obj_new.name} ({agent_id_for_stream}) from {agent_url}")
                    else:
                        agent_id_for_stream = existing_agent.agent_id
                        existing_agent.status = AgentStatus.ONLINE
                        existing_agent.last_seen = time.time()
                        if not existing_agent.mcp_sse_url: # If not already set
                            existing_agent.mcp_sse_url = agent_url
                        await storage_inst.save_agent(existing_agent)
                        logger.info(f"Custom Connect: Updated agent status via SSE: {existing_agent.name} ({agent_id_for_stream}) from {agent_url}")

                    # Forward SSE events from agent
                    async for line_text in response_stream.aiter_lines():
                        yield line_text + "\n" # Send the line as is
                        if not line_text.strip(): # SSE events end with an extra newline
                             yield "\n"

                        # Special handling for "event: endpoint" from original code
                        if line_text.startswith("event: endpoint"):
                            try:
                                # The next line should be data: "mcp_url_here"
                                data_line_text = await response_stream.aiter_lines().__anext__()
                                yield data_line_text + "\n\n" # Send it and the final newline

                                if data_line_text.startswith("data:"):
                                    endpoint_data_str = data_line_text[len("data:"):].strip()
                                    try:
                                        # Expect data to be a JSON string containing the URL
                                        mcp_endpoint_url = json.loads(endpoint_data_str)
                                        if isinstance(mcp_endpoint_url, str):
                                            agent_to_update_mcp = await storage_inst.get_agent(agent_id_for_stream)
                                            if agent_to_update_mcp:
                                                resolved_mcp_url = urllib.parse.urljoin(agent_url, mcp_endpoint_url)
                                                agent_to_update_mcp.mcp_url = resolved_mcp_url
                                                await storage_inst.save_agent(agent_to_update_mcp)
                                                logger.info(f"Custom Connect: Updated agent {agent_to_update_mcp.name} MCP URL via SSE event: {resolved_mcp_url}")
                                        else:
                                            logger.warning(f"Custom Connect: 'endpoint' data not a string: {endpoint_data_str} from {agent_url}")
                                    except json.JSONDecodeError:
                                        logger.warning(f"Custom Connect: 'endpoint' data not valid JSON: {endpoint_data_str} from {agent_url}")
                                    except Exception as e_update:
                                        logger.error(f"Custom Connect: Error updating agent MCP URL for {agent_id_for_stream} from {agent_url}: {e_update}", exc_info=True)
                            except StopAsyncIteration:
                                logger.warning(f"Custom Connect: Stream from {agent_url} ended prematurely after 'event: endpoint'.")
                                break # Stop processing this stream

                    logger.info(f"Custom Connect: Agent stream ended for {agent_url}")

            except httpx.RequestError as e_req_custom:
                logger.error(f"Custom Connect: Network error proxying SSE from {agent_url}: {e_req_custom}", exc_info=True)
                yield f"event: error\ndata: {json.dumps({'error': f'Network error connecting to agent: {str(e_req_custom)}'})}\n\n"
            except Exception as e_custom_outer:
                logger.error(f"Custom Connect: Unexpected error proxying SSE from {agent_url}: {e_custom_outer}", exc_info=True)
                yield f"event: error\ndata: {json.dumps({'error': f'Unexpected error proxying agent stream: {str(e_custom_outer)}'})}\n\n"
            finally:
                logger.info(f"Custom Connect: Event generator for {agent_url} finished.")


    sse_resp_headers = {
        "Content-Type": "text/event-stream", "Cache-Control": "no-cache",
        "Connection": "keep-alive", "X-Accel-Buffering": "no" # For Nginx
    }
    return StreamingResponse(event_generator_custom(), headers=sse_resp_headers)


@app.post("/connect/stream/init", tags=["Custom Agent Connect (Legacy)"])
async def connect_stream_init_custom_endpoint(
        agent_payload: AgentRegistration, # Expects full AgentRegistration model in body
        request_obj: Request,
        background_tasks: BackgroundTasks,
        storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    """
    Initialize agent stream connection by full registration. (Legacy)

    NOTE: This is a custom endpoint, similar to /register but perhaps with
    a different historical context. Prefer standard /register.
    """
    logger.warning("/connect/stream/init is a custom, non-standard endpoint. Prefer /register.")

    # Use the logic similar to /register
    existing_agent = await storage_inst.get_agent_by_url(agent_payload.url)
    agent_to_save = agent_payload
    status_msg_key = "initialized_new"

    if existing_agent:
        agent_to_save.agent_id = existing_agent.agent_id
        agent_to_save.created_at = existing_agent.created_at
        if existing_agent.metrics and agent_to_save.metrics is None: # Preserve metrics
            agent_to_save.metrics = existing_agent.metrics
        status_msg_key = "initialized_updated"
        logger.info(f"Custom Init: Updating agent {agent_to_save.agent_id} for {agent_to_save.url}")
    else:
        logger.info(f"Custom Init: Creating new agent for {agent_to_save.url}")

    if agent_to_save.metrics is None: # Initialize metrics if not present
        agent_to_save.metrics = AgentMetrics()

    agent_to_save.status = AgentStatus.ONLINE
    agent_to_save.last_seen = time.time()

    if not await storage_inst.save_agent(agent_to_save):
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to save agent via custom init")

    cm = getattr(request_obj.app.state, 'connection_manager', None)
    if cm:
        event_name = "agent_registered" if status_msg_key == "initialized_new" else "agent_updated"
        background_tasks.add_task(
            cm.broadcast_event, event_name,
            {
                "agent_id": agent_to_save.agent_id, "name": agent_to_save.name,
                "url": agent_to_save.url, "status": agent_to_save.status.value
            }
        )

    logger.info(f"Custom Init: Agent {status_msg_key} for: {agent_to_save.name} ({agent_to_save.agent_id})")
    return JSONResponse(content={"status": status_msg_key, "agent_id": agent_to_save.agent_id})


# --- Debug Endpoint ---

@app.post("/debug/register-test-agent", tags=["Debug"], include_in_schema=os.environ.get("DEBUG", "false").lower() == "true")
async def debug_register_test_agent(
        request: Request, # For app.state access
        background_tasks: BackgroundTasks,
        storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    """
    DEBUGGING ONLY: Manually registers/updates a predefined test weather agent.
    This endpoint is only available if DEBUG environment variable is true.
    """
    if os.environ.get("DEBUG", "false").lower() != "true":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Debug endpoint not available.")

    agent_id_fixed = "test-weather-agent-fixed-001" # Fixed ID for idempotency
    # Ensure URL is unique if multiple hubs run against same DB, or make it configurable
    agent_url = f"http://fake-weather-service.example.com/{agent_id_fixed}"
    mcp_endpoint = f"{agent_url}/mcp/stream"

    logger.info(f"DEBUG: Attempting to register/update test agent: {agent_id_fixed}")

    test_agent_payload = AgentRegistration(
        agent_id=agent_id_fixed,
        url=agent_url,
        name="Fixed Test Weather Service",
        description="Provides weather forecasts (Simulated for Debug).",
        version="1.2.0",
        mcp_tools=[
            MCPTool(
                name="get_forecast", description="Get weather forecast for a location.",
                inputSchema={
                    "type": "object", "properties": {
                        "latitude": {"type": "number", "description": "Latitude"},
                        "longitude": {"type": "number", "description": "Longitude"}
                    }, "required": ["latitude", "longitude"]
                }
            ),
            MCPTool(
                name="get_alerts", description="Get weather alerts for a US state.",
                inputSchema={
                    "type": "object", "properties": {
                        "state": {"type": "string", "description": "Two-letter US state code (e.g., CA)"}
                    }, "required": ["state"]
                }
            )
        ],
        aira_capabilities=["mcp"], # Only MCP for this test agent
        status=AgentStatus.ONLINE, # Will be set to online
        tags=["weather", "forecast", "debug_test"],
        category="Utilities-Debug",
        provider={"name": "AIRA Hub Debug Suite"},
        mcp_stream_url=mcp_endpoint, # Assumes it can handle POSTs or hub proxies appropriately
        mcp_url=mcp_endpoint
    )

    try:
        existing = await storage_inst.get_agent(agent_id_fixed) # Check by fixed ID
        if not existing: # If not by ID, check by URL (in case ID changed but URL is reused)
             existing = await storage_inst.get_agent_by_url(test_agent_payload.url)


        agent_to_save = test_agent_payload # Start with the new payload

        if existing:
            logger.info(f"DEBUG: Test agent {agent_id_fixed} (URL: {test_agent_payload.url}) already exists, updating.")
            agent_to_save.agent_id = existing.agent_id # Ensure consistent ID
            agent_to_save.created_at = existing.created_at # Preserve original creation time
            agent_to_save.metrics = existing.metrics # Preserve existing metrics
        else:
            logger.info(f"DEBUG: Test agent {agent_id_fixed} (URL: {test_agent_payload.url}) does not exist, creating.")

        agent_to_save.status = AgentStatus.ONLINE
        agent_to_save.last_seen = time.time()

        saved_ok = await storage_inst.save_agent(agent_to_save)
        if not saved_ok:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to save test agent to DB.")

        cm = getattr(request.app.state, 'connection_manager', None)
        if cm:
            event_name = "agent_updated" if existing else "agent_registered"
            background_tasks.add_task(
                cm.broadcast_event, event_name,
                {
                    "agent_id": agent_to_save.agent_id, "name": agent_to_save.name,
                    "url": agent_to_save.url, "status": agent_to_save.status.value
                }
            )
        logger.info(f"DEBUG: Test agent '{agent_to_save.name}' (ID: {agent_to_save.agent_id}) registration/update successful.")
        return {
            "status": "ok",
            "operation": "updated" if existing else "created",
            "agent_id": agent_to_save.agent_id,
            "message": f"Test agent {agent_to_save.name} processed."
        }

    except HTTPException: # Re-raise HTTPExceptions
        raise
    except Exception as e:
        logger.error(f"DEBUG: Error registering/updating test agent {agent_id_fixed}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to process test agent: {str(e)}")


# ----- Main Entry Point -----

if __name__ == "__main__":
    import uvicorn
    from fastapi.routing import APIRoute

    # Get port from environment or use default
    port = int(os.environ.get("PORT", 8017))
    host = os.environ.get("HOST", "0.0.0.0")  # Default to all interfaces
    debug_mode = os.environ.get("DEBUG", "false").lower() == "true"

    # Simplify operation IDs for OpenAPI clients
    for route in app.routes:
        if isinstance(route, APIRoute):
            route.operation_id = route.name

    # Run the application
    logger.info(f"Starting AIRA Hub on {host}:{port} (Debug Mode: {debug_mode})")
    uvicorn.run(
        "__main__:app", # Use __main__:app when running script directly
        host=host,
        port=port,
        reload=debug_mode, # Enable reload only in debug mode
        log_level="debug" if debug_mode else "info"
    )