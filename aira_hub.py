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
from datetime import datetime, timezone, timedelta
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
MCP_CLIENT_TIMEOUT = 300.0 # Default timeout for the HTTP client used in MCPStreamSession
AGENT_MCP_REQUEST_TIMEOUT = 1800.0 # Specific timeout for long-polling agent calls
A2A_FORWARD_TIMEOUT = 30.0 # Timeout for forwarding A2A tasks to agents

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
        if not v.startswith(('http://', 'https://', 'local://')):
            raise ValueError('URL must start with http://, https://, or local://')
        try:
            parsed = urllib.parse.urlparse(v)
            if not parsed.scheme:
                raise ValueError("URL must have a scheme")
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

            index_futures = [
                self.db.agents.create_index("url", unique=True),
                self.db.agents.create_index("agent_id", unique=True),
                self.db.agents.create_index("last_seen"),
                self.db.agents.create_index("status"),
                self.db.agents.create_index("tags"),
                self.db.agents.create_index("aira_capabilities"),
                self.db.agents.create_index("category"),
                self.db.agents.create_index("mcp_tools.name"),
                self.db.tasks.create_index("agent_id"),
                self.db.tasks.create_index("skill_id"),
                self.db.tasks.create_index("current_status.state"),
                self.db.tasks.create_index("created_at"),
            ]
            await asyncio.gather(*index_futures)
            logger.info("MongoDB indexes created successfully")
            await self.refresh_tool_cache()
            logger.info(f"Connected to MongoDB database: aira_hub")
        except Exception as e:
            logger.error(f"Error connecting/initializing MongoDB: {str(e)}", exc_info=True)
            self.db = None
            self.mongo_db_client = None
            raise e

    async def close(self) -> None:
        if self.mongo_db_client:
            self.mongo_db_client.close()
            logger.info("MongoDB connection closed")
            self.mongo_db_client = None
            self.db = None

    async def refresh_tool_cache(self) -> None:
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
                if not agent_id_str: continue
                for tool in agent_doc.get("mcp_tools", []):
                    tool_name = tool.get("name")
                    if tool_name: self.tool_cache[tool_name] = agent_id_str
            logger.info(f"Tool cache refreshed: {len(self.tool_cache)} tools")
        except Exception as e:
            logger.error(f"Error refreshing tool cache: {e}", exc_info=True)

    async def save_agent(self, agent: AgentRegistration) -> bool:
        if self.db is None:
            logger.error("Database not initialized, cannot save agent.")
            return False
        agent_dict = agent.model_dump(exclude_unset=True)
        agent_id_str = str(agent.agent_id)

        tools_to_remove = [k for k, v in self.tool_cache.items() if v == agent_id_str]
        for tool_name in tools_to_remove:
            if tool_name in self.tool_cache: del self.tool_cache[tool_name]
        if agent.status == AgentStatus.ONLINE:
            for tool in agent.mcp_tools:
                if hasattr(tool, 'name') and tool.name: self.tool_cache[tool.name] = agent_id_str
        try:
            result = await self.db.agents.update_one(
                {"agent_id": agent_id_str}, {"$set": agent_dict}, upsert=True
            )
            if result.upserted_id: logger.info(f"New agent created: {agent.name} ({agent_id_str})")
            elif result.modified_count > 0: logger.info(f"Agent updated: {agent.name} ({agent_id_str})")
            return True
        except Exception as e:
            logger.error(f"Error saving agent {agent.name}: {e}", exc_info=True)
            return False

    async def get_agent(self, agent_id: str) -> Optional[AgentRegistration]:
        if self.db is None: return None
        try:
            agent_dict = await self.db.agents.find_one({"agent_id": agent_id})
            if agent_dict:
                agent_dict.pop("_id", None)
                return AgentRegistration(**agent_dict)
            return None
        except Exception as e:
            logger.error(f"Error getting agent {agent_id}: {e}", exc_info=True)
            return None

    async def get_agent_by_url(self, url: str) -> Optional[AgentRegistration]:
        if self.db is None: return None
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
        if self.db is None: return None
        try:
            agent_id_from_cache = self.tool_cache.get(tool_name)
            if agent_id_from_cache:
                agent = await self.get_agent(agent_id_from_cache)
                if agent and agent.status == AgentStatus.ONLINE: return agent
                else:
                    if tool_name in self.tool_cache: del self.tool_cache[tool_name]
                    logger.debug(f"Removed stale cache entry for tool: {tool_name}")
            agent_dict = await self.db.agents.find_one(
                {"mcp_tools.name": tool_name, "status": AgentStatus.ONLINE.value}
            )
            if agent_dict:
                agent_dict.pop("_id", None)
                agent_id_str = str(agent_dict["agent_id"])
                self.tool_cache[tool_name] = agent_id_str
                return AgentRegistration(**agent_dict)
            return None
        except Exception as e:
            logger.error(f"Error getting agent by tool {tool_name}: {e}", exc_info=True)
            return None

    async def list_agents(self) -> List[AgentRegistration]:
        agents = []
        if self.db is None: return []
        try:
            cursor = self.db.agents.find()
            async for agent_dict in cursor:
                agent_dict.pop("_id", None)
                try: agents.append(AgentRegistration(**agent_dict))
                except Exception as p_err: logger.warning(f"Skipping agent due to validation error: {agent_dict.get('agent_id')}. Error: {p_err}")
            return agents
        except Exception as e:
            logger.error(f"Error listing agents: {e}", exc_info=True)
            return []

    async def list_tools(self) -> List[Dict[str, Any]]:
        tools = []
        if self.db is None: return []
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
                        "name": tool_data.get("name"), "description": tool_data.get("description"),
                        "agent": agent_name, "agent_id": agent_id_str,
                        "inputSchema": tool_data.get("inputSchema", {}),
                        "annotations": tool_data.get("annotations")
                    }
                    if tool_dict["name"] and tool_dict["inputSchema"] is not None: tools.append(tool_dict)
                    else: logger.warning(f"Skipping malformed tool from agent {agent_id_str}: {tool_data}")
            return tools
        except Exception as e:
            logger.error(f"Error listing tools: {e}", exc_info=True)
            return []

    async def delete_agent(self, agent_id: str) -> bool:
        if self.db is None: return False
        try:
            agent = await self.get_agent(agent_id)
            if agent:
                agent_id_str = str(agent.agent_id)
                tools_to_remove = [k for k, v in self.tool_cache.items() if v == agent_id_str]
                for tool_name in tools_to_remove:
                    if tool_name in self.tool_cache: del self.tool_cache[tool_name]
                result = await self.db.agents.delete_one({"agent_id": agent_id})
                if result.deleted_count > 0:
                    logger.info(f"Agent deleted: {agent.name} ({agent_id})")
                    return True
            return False
        except Exception as e:
            logger.error(f"Error deleting agent {agent_id}: {e}", exc_info=True)
            return False

    async def update_agent_heartbeat(self, agent_id: str, timestamp: float) -> bool:
        if self.db is None: return False
        try:
            result = await self.db.agents.update_one(
                {"agent_id": agent_id}, {"$set": {"last_seen": timestamp}}
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Error updating heartbeat for agent {agent_id}: {e}", exc_info=True)
            return False

    async def count_agents(self, query: Dict[str, Any] = None) -> int:
        if self.db is None: return 0
        try: return await self.db.agents.count_documents(query or {})
        except Exception as e:
            logger.error(f"Error counting agents: {e}", exc_info=True)
            return 0

    async def search_agents(self, query: Dict[str, Any] = None, skip: int = 0, limit: int = 100) -> List[AgentRegistration]:
        agents = []
        if self.db is None: return []
        try:
            cursor = self.db.agents.find(query or {}).skip(skip).limit(limit)
            async for agent_dict in cursor:
                agent_dict.pop("_id", None)
                try: agents.append(AgentRegistration(**agent_dict))
                except Exception as p_err: logger.warning(f"Skipping agent in search due to validation error: {agent_dict.get('agent_id')}. Error: {p_err}")
            return agents
        except Exception as e:
            logger.error(f"Error searching agents: {e}", exc_info=True)
            return []

    async def save_a2a_task(self, task: A2ATask) -> bool:
        if self.db is None:
            logger.error("Database not initialized, cannot save A2A task.")
            return False
        try:
            task_dict = task.model_dump(exclude_unset=True)
            task_dict["updated_at"] = datetime.now(timezone.utc)
            await self.db.tasks.update_one(
                {"id": task.id}, {"$set": task_dict}, upsert=True
            )
            logger.info(f"A2A Task {task.id} saved/updated.")
            return True
        except Exception as e:
            logger.error(f"Error saving A2A task {task.id}: {e}", exc_info=True)
            return False

    async def get_a2a_task(self, task_id: str) -> Optional[A2ATask]:
        if self.db is None: return None
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
    def __init__(self, storage_param: Optional[MongoDBStorage]):
        self.active_sessions: Dict[str, Dict[str, Any]] = {}
        self.client_connections: Dict[str, List[str]] = {}
        self.storage = storage_param # Not actively used by MCPSession itself, but kept if needed later

    def create_session(self, client_id: str) -> str:
        session_id = str(uuid.uuid4())
        self.active_sessions[session_id] = {
            "client_id": client_id, "created_at": time.time(),
            "last_activity": time.time(), "state": {}
        }
        self.client_connections.setdefault(client_id, []).append(session_id)
        logger.debug(f"Created MCP session {session_id} for client {client_id}")
        return session_id

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        session = self.active_sessions.get(session_id)
        if session: session["last_activity"] = time.time()
        return session

    def update_session_activity(self, session_id: str) -> None:
        if session_id in self.active_sessions:
            self.active_sessions[session_id]["last_activity"] = time.time()
            logger.debug(f"Updated session {session_id} activity")

    def close_session(self, session_id: str) -> None:
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
        return self.client_connections.get(client_id, [])

    def cleanup_stale_sessions(self, max_age_seconds: int = 3600) -> int:
        now = time.time()
        to_remove = [sid for sid, s in self.active_sessions.items()
                     if now - s.get("last_activity", 0) > max_age_seconds]
        count = 0
        for session_id in to_remove:
            self.close_session(session_id)
            count += 1
        if count > 0: logger.info(f"Cleaned up {count} stale MCP sessions (max_age={max_age_seconds}s).")
        return count


class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, Dict[str, Any]] = {}

    def register_connection(self, client_id: str, send_func: Callable[[str, Any], Awaitable[None]]) -> None:
        self.active_connections[client_id] = {
            "connected_at": time.time(), "last_activity": time.time(), "send_func": send_func
        }
        logger.info(f"Registered client UI connection: {client_id}")

    def unregister_connection(self, client_id: str) -> None:
        if client_id in self.active_connections:
            del self.active_connections[client_id]
            logger.info(f"Unregistered client UI connection: {client_id}")

    async def send_event(self, client_id: str, event: str, data: Any) -> bool:
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
        await asyncio.gather(*[
            self.send_event(cid, event, data) for cid in list(self.active_connections.keys())
        ])

    def get_all_connections(self) -> List[str]:
        return list(self.active_connections.keys())

# ----- PERIODIC CLEANUP TASK -----
async def periodic_cleanup_runner(app_state: Any, interval_seconds: int):
    storage_inst: Optional[MongoDBStorage] = getattr(app_state, 'storage', None)
    mcp_session_manager: Optional[MCPSession] = getattr(app_state, 'mcp_session_manager', None)

    while True:
        try:
            await asyncio.sleep(interval_seconds)
            logger.info("Periodic cleanup task running...")
            now = time.time()

            # Mark stale agents offline
            if storage_inst and storage_inst.db:
                query_stale = {
                    "status": AgentStatus.ONLINE.value,
                    "last_seen": {"$lt": now - DEFAULT_HEARTBEAT_TIMEOUT}
                }
                update_result = await storage_inst.db.agents.update_many(
                    query_stale,
                    {"$set": {"status": AgentStatus.OFFLINE.value}}
                )
                updated_count = update_result.modified_count
                if updated_count > 0:
                    logger.info(f"Periodic Cleanup: Marked {updated_count} agents as OFFLINE due to inactivity.")
                    await storage_inst.refresh_tool_cache() # Refresh cache if any agents changed state
            else:
                logger.warning("Periodic Cleanup: Storage not available for agent cleanup.")

            # Cleanup MCP sessions
            if mcp_session_manager:
                mcp_session_manager.cleanup_stale_sessions() # Uses its own default max_age
            else:
                logger.warning("Periodic Cleanup: MCP Session Manager not available.")

        except asyncio.CancelledError:
            logger.info("Periodic cleanup task cancelled.")
            break
        except Exception as e:
            logger.error(f"Error in periodic cleanup: {e}", exc_info=True)


# ----- APPLICATION LIFECYCLE -----

storage: Optional[MongoDBStorage] = None
mongo_client: Optional[AsyncIOMotorClient] = None
db: Optional[AsyncIOMotorDatabase] = None


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    global storage, mongo_client, db
    logger.info("AIRA Hub lifespan startup...")
    temp_storage = MongoDBStorage(MONGODB_URL)
    app_instance.state.storage_failed = False
    try:
        await temp_storage.init()
        storage = temp_storage
        mongo_client = temp_storage.mongo_db_client
        db = temp_storage.db
        app_instance.state.storage = storage
        app_instance.state.mongo_client = mongo_client
        app_instance.state.db = db
        app_instance.state.connection_manager = ConnectionManager()
        app_instance.state.mcp_session_manager = MCPSession(storage)
        app_instance.state.start_time = time.time()
        app_instance.state.active_mcp_streams = {}

        # Start periodic cleanup task
        app_instance.state.cleanup_task = asyncio.create_task(
            periodic_cleanup_runner(app_instance.state, DEFAULT_CLEANUP_INTERVAL),
            name="PeriodicCleanupRunner"
        )
        logger.info("AIRA Hub services initialized successfully, periodic cleanup started.")
        yield
        logger.info("AIRA Hub lifespan shutdown...")
        if hasattr(app_instance.state, 'cleanup_task') and app_instance.state.cleanup_task:
            app_instance.state.cleanup_task.cancel()
            try:
                await app_instance.state.cleanup_task
            except asyncio.CancelledError:
                logger.info("Periodic cleanup task successfully cancelled during shutdown.")
            except Exception as e_cleanup_shutdown:
                logger.error(f"Error awaiting cleanup task during shutdown: {e_cleanup_shutdown}", exc_info=True)

        if storage: await storage.close()
        logger.info("AIRA Hub shutdown complete.")
    except Exception as e:
        logger.critical(f"AIRA Hub critical startup error: {e}", exc_info=True)
        app_instance.state.storage_failed = True
        yield # Allow app to run even if startup fails, to report error via health check perhaps


# ----- MCP STREAM HANDLER (NOW A CLASS) -----
class MCPStreamSession:
    def __init__(self, stream_id: str, initial_messages: List[Dict[str, Any]],
                 request_stream: AsyncGenerator[bytes, None], send_queue: asyncio.Queue,
                 storage_inst: MongoDBStorage, app_state: Any):
        self.stream_id = stream_id
        self.initial_messages = initial_messages
        self.request_stream = request_stream
        self.send_queue = send_queue
        self.storage_inst = storage_inst
        self.app_state = app_state

        self.pending_tasks: Dict[str, asyncio.Task] = {}
        self.background_notifications: List[asyncio.Task] = []
        self.expected_response_ids: Set[Union[str, int]] = set()

        self.reader_task_finished_event = asyncio.Event()
        self.outstanding_responses_event = asyncio.Event()
        self.outstanding_responses_event.set()  # Start as set

        self.reader_main_task: Optional[asyncio.Task] = None
        self.current_event_wait_tasks: List[asyncio.Task] = [] # Tasks for event.wait() in the main loop

        self.agent_http_client: Optional[httpx.AsyncClient] = None # Will be initialized in handle_stream

    async def _check_and_manage_outstanding_event(self):
        all_done = self.reader_task_finished_event.is_set() and \
                   not self.pending_tasks and \
                   not self.expected_response_ids

        logger.debug(
            f"MCP Stream {self.stream_id}: CheckOutstanding: ReaderDone={self.reader_task_finished_event.is_set()}, "
            f"PendingTasks={len(self.pending_tasks)}, ExpectedIDs={len(self.expected_response_ids)}, AllDoneCondition={all_done}"
        )

        if all_done:
            if not self.outstanding_responses_event.is_set():
                logger.info(f"MCP Stream {self.stream_id}: All conditions met. Setting outstanding_responses_event.")
                self.outstanding_responses_event.set()
        else:
            if self.outstanding_responses_event.is_set():
                logger.info(f"MCP Stream {self.stream_id}: Conditions NOT fully met. Clearing outstanding_responses_event.")
                self.outstanding_responses_event.clear()

    async def _forward_and_process_agent_response(
            self, client_mcp_req_id: Union[str, int], original_client_method: str,
            agent_target_url: str, payload_to_agent: Dict[str, Any],
            agent_name_for_log: str, is_a2a_bridged: bool
    ):
        logger.info(
            f"MCP Stream {self.stream_id}: ENTERING _fwd_process for MCP ID {client_mcp_req_id} to '{agent_name_for_log}' at {agent_target_url}"
        )
        response_text_debug = ""
        response_queued_successfully = False
        final_mcp_object_to_queue: Optional[MCPResponseModel] = None

        custom_headers = {
            "User-Agent": "AIRA-Hub-MCP-Proxy/1.0", "Accept": "application/json, application/json-seq, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9", "Connection": "keep-alive"
        }

        try:
            if not self.agent_http_client:
                # This should not happen if handle_stream initializes it.
                logger.error(f"MCP Stream {self.stream_id}: agent_http_client not initialized in _forward_and_process_agent_response.")
                raise MCPError(-32000, "Hub internal error: HTTP client not available")

            logger.info(
                f"MCP Stream {self.stream_id}: ATTEMPTING POST to agent '{agent_name_for_log}' at {agent_target_url} for MCP ID {client_mcp_req_id}."
            )
            logger.debug(f"MCP Stream {self.stream_id}: Payload to agent: {json.dumps(payload_to_agent)}")

            response_from_agent_http = await self.agent_http_client.post(
                agent_target_url, json=payload_to_agent, headers=custom_headers,
                timeout=AGENT_MCP_REQUEST_TIMEOUT
            )
            response_text_debug = response_from_agent_http.text
            logger.info(
                f"MCP Stream {self.stream_id}: Agent '{agent_name_for_log}' HTTP status {response_from_agent_http.status_code} for MCP ID {client_mcp_req_id}."
            )
            logger.debug(
                f"MCP Stream {self.stream_id}: Agent '{agent_name_for_log}' raw response for MCP ID {client_mcp_req_id}: {response_text_debug[:500]}..."
            )
            response_from_agent_http.raise_for_status()

            agent_response_data_dict = response_from_agent_http.json()

            if is_a2a_bridged:
                # ... (A2A to MCP translation logic - kept same as original)
                logger.debug(
                    f"MCP Stream {self.stream_id}: Translating A2A response from '{agent_name_for_log}' for MCP ID {client_mcp_req_id}. A2A: {json.dumps(agent_response_data_dict)}")
                mcp_result_payload_for_client: Optional[List[Dict[str, Any]]] = None
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
                    else:
                        status_state = a2a_task_result.get("status", {}).get("state", "unknown_a2a_state")
                        status_message_obj = a2a_task_result.get("status", {}).get("message", {})
                        status_message_parts = status_message_obj.get("parts", []) if isinstance(status_message_obj, dict) else []
                        a2a_status_text = None
                        if status_message_parts and status_message_parts[0].get("type") == "text": a2a_status_text = status_message_parts[0].get("text")
                        fallback_msg = f"A2A Task Status: {status_state}." + (f" Message: {a2a_status_text}" if a2a_status_text else " No primary text artifact.")
                        logger.warning(f"MCP Stream {self.stream_id}: No primary text artifact in A2A for MCP ID {client_mcp_req_id}. Fallback: {fallback_msg}")
                        mcp_result_payload_for_client = [{"type": "text", "text": fallback_msg}]
                    final_mcp_object_to_queue = MCPResponseModel(id=client_mcp_req_id, result=mcp_result_payload_for_client)
                elif "error" in agent_response_data_dict:
                    a2a_error = agent_response_data_dict['error']
                    logger.error(f"MCP Stream {self.stream_id}: A2A Agent '{agent_name_for_log}' (MCP ID {client_mcp_req_id}) returned A2A error: {a2a_error}")
                    final_mcp_object_to_queue = MCPResponseModel(id=client_mcp_req_id, error={"code": a2a_error.get("code", -32006), "message": f"A2A Agent Error: {a2a_error.get('message', 'Unknown A2A error')}", "data": a2a_error.get("data")})
                else:
                    logger.error(f"MCP Stream {self.stream_id}: Unexpected A2A response structure from '{agent_name_for_log}' for MCP ID {client_mcp_req_id}: {agent_response_data_dict}")
                    final_mcp_object_to_queue = MCPResponseModel(id=client_mcp_req_id, error={"code": -32005, "message": "Hub error: Could not parse A2A agent's response"})
            else: # Direct MCP
                # ... (Direct MCP response processing - kept same as original)
                logger.debug(f"MCP Stream {self.stream_id}: Processing direct MCP response from '{agent_name_for_log}' for MCP ID {client_mcp_req_id}.")
                if isinstance(agent_response_data_dict, dict) and agent_response_data_dict.get("jsonrpc") == "2.0":
                    try:
                        mcp_resp_from_agent = MCPResponseModel(**agent_response_data_dict)
                        final_mcp_object_to_queue = MCPResponseModel(id=client_mcp_req_id, result=mcp_resp_from_agent.result, error=mcp_resp_from_agent.error)
                        if mcp_resp_from_agent.error: logger.warning(f"MCP Stream {self.stream_id}: Direct MCP agent '{agent_name_for_log}' returned error for its ID {mcp_resp_from_agent.id} (Client MCP ID {client_mcp_req_id}): {mcp_resp_from_agent.error}")
                    except ValidationError as e_val_agent_resp:
                        logger.error(f"MCP Stream {self.stream_id}: Invalid MCPResponseModel from direct MCP agent '{agent_name_for_log}' for MCP ID {client_mcp_req_id}: {e_val_agent_resp}")
                        final_mcp_object_to_queue = MCPResponseModel(id=client_mcp_req_id, error={"code": -32005, "message": "Invalid response from target MCP agent", "data": str(e_val_agent_resp)})
                else:
                    logger.warning(f"MCP Stream {self.stream_id}: Direct MCP agent '{agent_name_for_log}' returned non-JSONRPC or unexpected format for MCP ID {client_mcp_req_id}. Result: {agent_response_data_dict}")
                    wrapped_result = [{"type": "text", "text": json.dumps(agent_response_data_dict) if agent_response_data_dict is not None else "Agent returned non-standard data."}]
                    final_mcp_object_to_queue = MCPResponseModel(id=client_mcp_req_id, result=wrapped_result)

            if final_mcp_object_to_queue:
                if original_client_method == "tools/call" and \
                   final_mcp_object_to_queue.result and \
                   isinstance(final_mcp_object_to_queue.result, list) and \
                   not final_mcp_object_to_queue.error:
                    logger.debug(f"MCP Stream {self.stream_id}: Wrapping 'tools/call' result for MCP ID {client_mcp_req_id}.")
                    actual_mcp_result_object = {"content": final_mcp_object_to_queue.result}
                    final_mcp_object_to_queue.result = actual_mcp_result_object

                await self.send_queue.put(final_mcp_object_to_queue)
                response_queued_successfully = True
                logger.info(
                    f"MCP Stream {self.stream_id}: Successfully queued MCPResponse for MCP ID {client_mcp_req_id} to client. Final structure: {final_mcp_object_to_queue.model_dump_json(exclude_none=True, indent=2)}"
                )

        except httpx.HTTPStatusError as e_http:
            logger.error(f"MCP Stream {self.stream_id}: Agent HTTPStatusError for MCP ID {client_mcp_req_id} from '{agent_name_for_log}': {e_http.response.status_code} - Resp: {response_text_debug[:500]}", exc_info=True)
            await self.send_queue.put(MCPResponseModel(id=client_mcp_req_id, error={"code": -32003, "message": f"Agent error: {e_http.response.status_code}", "data": response_text_debug[:500]}))
            response_queued_successfully = True
        except httpx.ConnectTimeout as e_connect_timeout:
            logger.error(f"MCP Stream {self.stream_id}: ConnectTimeout calling agent '{agent_name_for_log}' for MCP ID {client_mcp_req_id}: {e_connect_timeout}", exc_info=True)
            await self.send_queue.put(MCPResponseModel(id=client_mcp_req_id, error={"code": -32004, "message": f"Agent connection timed out: {type(e_connect_timeout).__name__}"}))
            response_queued_successfully = True
        except httpx.ReadTimeout as e_read_timeout:
            logger.error(f"MCP Stream {self.stream_id}: ReadTimeout from agent '{agent_name_for_log}' for MCP ID {client_mcp_req_id}: {e_read_timeout}", exc_info=True)
            await self.send_queue.put(MCPResponseModel(id=client_mcp_req_id, error={"code": -32004, "message": f"Agent read timed out: {type(e_read_timeout).__name__}"}))
            response_queued_successfully = True
        except httpx.Timeout as e_general_timeout:
            logger.error(f"MCP Stream {self.stream_id}: General Timeout with agent '{agent_name_for_log}' for MCP ID {client_mcp_req_id}: {e_general_timeout}", exc_info=True)
            await self.send_queue.put(MCPResponseModel(id=client_mcp_req_id, error={"code": -32004, "message": f"Agent operation timed out: {type(e_general_timeout).__name__}"}))
            response_queued_successfully = True
        except httpx.RequestError as e_req:
            logger.error(f"MCP Stream {self.stream_id}: Agent httpx.RequestError for MCP ID {client_mcp_req_id} to '{agent_name_for_log}': {type(e_req).__name__} - {e_req}", exc_info=True)
            await self.send_queue.put(MCPResponseModel(id=client_mcp_req_id, error={"code": -32004, "message": f"Agent communication error: {type(e_req).__name__}"}))
            response_queued_successfully = True
        except json.JSONDecodeError as e_json_dec:
            logger.error(f"MCP Stream {self.stream_id}: Agent JSONDecodeError for MCP ID {client_mcp_req_id} from '{agent_name_for_log}'. Resp: {response_text_debug[:500]}", exc_info=True)
            await self.send_queue.put(MCPResponseModel(id=client_mcp_req_id, error={"code": -32005, "message": "Agent response parse error", "data": str(e_json_dec)}))
            response_queued_successfully = True
        except MCPError as e_mcp_internal: # Catch internal MCPError explicitly
            logger.error(f"MCP Stream {self.stream_id}: Internal MCPError in _fwd_process for MCP ID {client_mcp_req_id}: {e_mcp_internal}", exc_info=True)
            await self.send_queue.put(MCPResponseModel(id=client_mcp_req_id, error={"code": e_mcp_internal.code, "message": e_mcp_internal.message, "data": e_mcp_internal.data}))
            response_queued_successfully = True
        except Exception as e_fwd_generic:
            logger.error(f"MCP Stream {self.stream_id}: GENERIC EXCEPTION in _fwd_process for MCP ID {client_mcp_req_id} to '{agent_name_for_log}': {type(e_fwd_generic).__name__} - {e_fwd_generic}", exc_info=True)
            try:
                await self.send_queue.put(MCPResponseModel(id=client_mcp_req_id, error={"code": -32000, "message": "Hub internal error during agent communication."}))
                response_queued_successfully = True
            except Exception as e_send_q_err:
                logger.error(f"MCP Stream {self.stream_id}: FAILED to put generic error on send_queue for MCP ID {client_mcp_req_id}: {e_send_q_err}", exc_info=True)
        finally:
            if response_queued_successfully and client_mcp_req_id is not None:
                self.expected_response_ids.discard(client_mcp_req_id)

            task_id_str = str(client_mcp_req_id)
            self.pending_tasks.pop(task_id_str, None) # Use pop for idempotency

            await self._check_and_manage_outstanding_event()
            logger.info(f"MCP Stream {self.stream_id}: EXITING _fwd_process for MCP ID {client_mcp_req_id}")

    async def _process_mcp_request(self, req_data: Dict[str, Any]):
        try:
            mcp_req = MCPRequestModel(**req_data)
        except ValidationError as e_val:
            logger.warning(f"MCP Stream {self.stream_id}: Invalid MCP request: {req_data}, Error: {e_val}")
            error_id_val = req_data.get("id")
            await self.send_queue.put(MCPResponseModel(id=error_id_val, error={"code": -32600, "message": "Invalid Request", "data": e_val.errors()}))
            if error_id_val is not None:
                self.expected_response_ids.discard(error_id_val)
                await self._check_and_manage_outstanding_event()
            return

        logger.info(f"MCP Stream {self.stream_id}: RECV: {mcp_req.method} (ID: {mcp_req.id}) Params: {json.dumps(mcp_req.params)[:200]}...")

        if mcp_req.id is not None:
            self.expected_response_ids.add(mcp_req.id)
            # If we add an expected ID, the event *must* be cleared until that ID is resolved or other conditions change.
            if self.outstanding_responses_event.is_set():
                logger.debug(f"MCP Stream {self.stream_id}: Added req ID {mcp_req.id}, clearing outstanding_responses_event.")
                self.outstanding_responses_event.clear()
            # await self._check_and_manage_outstanding_event() # Not strictly needed here as clearing is definite if set.

        response_sent_directly = False
        if mcp_req.method == "initialize":
            # ... (initialize logic - same as original) ...
            client_info = mcp_req.params.get("clientInfo", {}) if isinstance(mcp_req.params, dict) else {}
            logger.info(f"MCP Stream {self.stream_id}: Initializing for {client_info.get('name', 'Unknown')} v{client_info.get('version', 'N/A')}")
            resp_params = {
                "protocolVersion": mcp_req.params.get("protocolVersion", "2024-11-05") if isinstance(mcp_req.params, dict) else "2024-11-05",
                "serverInfo": {"name": "AIRA Hub", "version": "1.0.0"},
                "capabilities": {"toolProxy": True, "toolDiscovery": True, "resourceDiscovery": True}}
            await self.send_queue.put(MCPResponseModel(id=mcp_req.id, result=resp_params))
            response_sent_directly = True
        elif mcp_req.method == "notifications/initialized":
            logger.info(f"MCP Stream {self.stream_id}: Client acknowledged initialization.")
            # No response needed, no ID tracking.
        elif mcp_req.method == "tools/list" or mcp_req.method == "mcp.discoverTools":
            # ... (tools/list logic - same as original) ...
            hub_tools = await self.storage_inst.list_tools()
            mcp_tools_resp = []
            for ht in hub_tools:
                td = {"name": ht.get("name"), "inputSchema": ht.get("inputSchema")}
                if ht.get("description"): td["description"] = ht.get("description")
                if ht.get("annotations"): td["annotations"] = ht.get("annotations")
                mcp_tools_resp.append(td)
            await self.send_queue.put(MCPResponseModel(id=mcp_req.id, result={"tools": mcp_tools_resp}))
            response_sent_directly = True
        elif mcp_req.method == "resources/list":
            # ... (resources/list logic - same as original) ...
            agents = await self.storage_inst.list_agents()
            mcp_res = []
            for ar in agents:
                uri = f"aira-hub://agent/{ar.agent_id}"
                desc = f"AIRA Agent: {ar.name} ({ar.status.value}) - {ar.description or ''}"
                meta = {"agent_id": ar.agent_id, "name": ar.name, "url": ar.url, "status": ar.status.value,
                        "capabilities": ar.aira_capabilities, "tags": ar.tags, "category": ar.category, "version": ar.version,
                        "registered_at": datetime.fromtimestamp(ar.created_at, timezone.utc).isoformat() if ar.created_at else None,
                        "last_seen": datetime.fromtimestamp(ar.last_seen, timezone.utc).isoformat() if ar.last_seen else None}
                mcp_res.append({"uri": uri, "description": desc, "metadata": meta})
            await self.send_queue.put(MCPResponseModel(id=mcp_req.id, result={"resources": mcp_res}))
            response_sent_directly = True
        elif mcp_req.method == "prompts/list":
            await self.send_queue.put(MCPResponseModel(id=mcp_req.id, result={"prompts": []}))
            response_sent_directly = True
        elif mcp_req.method == "tools/call":
            # ... (tools/call logic, now creating named tasks - mostly same as original) ...
            if mcp_req.id is None:
                logger.warning(f"MCP Stream {self.stream_id}: Received tools/call notification (no ID). Ignoring.")
                return
            if not isinstance(mcp_req.params, dict):
                await self.send_queue.put(MCPResponseModel(id=mcp_req.id, error={"code": -32602, "message": "Invalid params for tools/call"}))
                response_sent_directly = True
            else:
                tool_name = mcp_req.params.get("name")
                tool_args = mcp_req.params.get("arguments", {})
                if not tool_name or not isinstance(tool_name, str):
                    await self.send_queue.put(MCPResponseModel(id=mcp_req.id, error={"code": -32602, "message": "Invalid params: 'name' string required"}))
                    response_sent_directly = True
                else:
                    agent = await self.storage_inst.get_agent_by_tool(tool_name)
                    tool_def_from_db = None
                    if agent: tool_def_from_db = next((t for t in agent.mcp_tools if t.name == tool_name), None)

                    if not agent or not tool_def_from_db:
                        logger.warning(f"MCP Stream {self.stream_id}: Tool '{tool_name}' or provider not found/offline.")
                        await self.send_queue.put(MCPResponseModel(id=mcp_req.id, error={"code": -32601, "message": f"Tool not found or agent unavailable: {tool_name}"}))
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
                            target_url = annotations.get("aira_a2a_agent_url") # This is A2A agent's base URL for A2A /task/send
                            if not skill_id_a2a or not target_url:
                                logger.error(f"MCP Stream {self.stream_id}: Misconfigured A2A bridge for tool '{tool_name}'. Missing skill_id or agent_url.")
                                await self.send_queue.put(MCPResponseModel(id=mcp_req.id, error={"code": -32002, "message": "Hub A2A bridge misconfiguration"}))
                                response_sent_directly = True
                            else: # Construct A2A tasks/send payload
                                a2a_data = {"skill_id": skill_id_a2a}
                                if "user_input" in tool_args: a2a_data["user_input"] = tool_args["user_input"]
                                # A2A /tasks/send expects params like {"id": "task_id", "message": ...}
                                # The tool_args from MCP client become the content of A2A message's "data" part
                                downstream_payload = {
                                    "jsonrpc": "2.0", "id": str(uuid.uuid4()), # JSON-RPC ID for this A2A call
                                    "method": "tasks/send",
                                    "params": {
                                        "id": str(mcp_req.id), # Use MCP req ID as A2A task ID for tracing
                                        "message": {"role": "user", "parts": [{"type": "data", "data": tool_args}]} # Use full tool_args here
                                    }
                                }
                                # The actual A2A target URL is agent.url (A2A agent's root)
                                target_url = agent.url.rstrip('/') # Ensure no double slash
                        elif agent.stdio_command:
                            logger.warning(f"MCP Stream {self.stream_id}: Tool '{tool_name}' on stdio agent '{agent.name}'. Hub cannot proxy.")
                            err_data = {"message": f"Tool '{tool_name}' is on local stdio agent.", "agent_name": agent.name, "command": agent.stdio_command}
                            await self.send_queue.put(MCPResponseModel(id=mcp_req.id, error={"code": -32010, "message": "Local stdio execution required", "data": err_data}))
                            response_sent_directly = True
                        else: # Direct MCP proxy
                            is_a2a = False
                            target_url = agent.mcp_url or agent.mcp_stream_url
                            if not target_url:
                                logger.error(f"MCP Stream {self.stream_id}: Agent {agent.name} for tool '{tool_name}' has no mcp_url/mcp_stream_url.")
                                await self.send_queue.put(MCPResponseModel(id=mcp_req.id, error={"code": -32001, "message": "Agent endpoint misconfiguration"}))
                                response_sent_directly = True
                            else:
                                downstream_payload = MCPRequestModel(method="tools/call", params={"name": tool_name, "arguments": tool_args}, id=str(uuid.uuid4())).model_dump(exclude_none=True)

                        if not response_sent_directly:
                            if not target_url:
                                logger.error(f"MCP Stream {self.stream_id}: Target URL for '{tool_name}' is None after logic. Bug.")
                                await self.send_queue.put(MCPResponseModel(id=mcp_req.id, error={"code": -32000, "message": "Internal Hub Error: Target URL undefined"}))
                                response_sent_directly = True
                            else:
                                fwd_task = asyncio.create_task(
                                    self._forward_and_process_agent_response(
                                        client_mcp_req_id=mcp_req.id, agent_target_url=target_url,
                                        original_client_method=mcp_req.method,
                                        payload_to_agent=downstream_payload, agent_name_for_log=agent.name,
                                        is_a2a_bridged=is_a2a
                                    ),
                                    name=f"AgentForwarder-{self.stream_id}-{mcp_req.id}"
                                )
                                self.pending_tasks[str(mcp_req.id)] = fwd_task
        else: # Non-standard method
            # ... (Non-standard method logic, creating named tasks - same as original) ...
            logger.warning(f"MCP Stream {self.stream_id}: Non-standard method '{mcp_req.method}'. Attempting direct proxy if agent found for this as a tool name.")
            agent_direct = await self.storage_inst.get_agent_by_tool(mcp_req.method)
            if not agent_direct:
                logger.warning(f"MCP Stream {self.stream_id}: Agent for direct method/tool '{mcp_req.method}' not found.")
                if mcp_req.id is not None:
                    await self.send_queue.put(MCPResponseModel(id=mcp_req.id, error={"code": -32601, "message": f"Method not found: {mcp_req.method}"}))
                    response_sent_directly = True
            else:
                target_url_direct = agent_direct.mcp_url or agent_direct.mcp_stream_url
                if not target_url_direct:
                    logger.error(f"MCP Stream {self.stream_id}: Agent {agent_direct.name} for direct method '{mcp_req.method}' has no MCP endpoint.")
                    if mcp_req.id is not None:
                        await self.send_queue.put(MCPResponseModel(id=mcp_req.id, error={"code": -32001, "message": "Agent endpoint misconfiguration for direct call"}))
                        response_sent_directly = True
                else:
                    payload_direct = mcp_req.model_dump(exclude_none=True)
                    if mcp_req.id is not None:
                        fwd_direct_task = asyncio.create_task(
                            self._forward_and_process_agent_response(
                                client_mcp_req_id=mcp_req.id, agent_target_url=target_url_direct,
                                original_client_method=mcp_req.method, # Pass original method
                                payload_to_agent=payload_direct, agent_name_for_log=agent_direct.name,
                                is_a2a_bridged=False
                            ),
                            name=f"AgentDirectForwarder-{self.stream_id}-{mcp_req.id}"
                        )
                        self.pending_tasks[str(mcp_req.id)] = fwd_direct_task
                    else: # Notification
                        logger.info(f"MCP Stream {self.stream_id}: Forwarding direct notification '{mcp_req.method}' to agent {agent_direct.name}")
                        if not self.agent_http_client: # Should be initialized
                             logger.error(f"MCP Stream {self.stream_id}: agent_http_client not available for background notification.")
                             return # or raise an error
                        bg_notify_task = asyncio.create_task(
                            self.agent_http_client.post(target_url_direct, json=payload_direct),
                            name=f"AgentBgNotification-{self.stream_id}-{mcp_req.method}"
                        )
                        self.background_notifications.append(bg_notify_task)

        if response_sent_directly and mcp_req.id is not None:
            self.expected_response_ids.discard(mcp_req.id)
            await self._check_and_manage_outstanding_event()

    async def _read_from_client(self):
        buffer = b""
        try:
            async for chunk in self.request_stream:
                if not chunk: continue
                logger.debug(f"MCP Stream {self.stream_id}: Raw chunk from client: {chunk!r}")
                buffer += chunk
                while STREAM_SEPARATOR in buffer:
                    message_bytes, buffer = buffer.split(STREAM_SEPARATOR, 1)
                    if message_bytes.strip():
                        try:
                            req_data_outer = json.loads(message_bytes.decode('utf-8'))
                            req_list_to_process = []
                            if isinstance(req_data_outer, list):
                                req_list_to_process.extend(d for d in req_data_outer if isinstance(d, dict))
                            elif isinstance(req_data_outer, dict):
                                req_list_to_process.append(req_data_outer)
                            else:
                                logger.warning(f"MCP Stream {self.stream_id}: Invalid JSON type from client: {type(req_data_outer)}")
                                await self.send_queue.put(MCPResponseModel(id=None, error={"code": -32700, "message": "Invalid request format (not object or array)"}))
                                continue
                            for req_data_item in req_list_to_process:
                                await self._process_mcp_request(req_data_item)
                        except json.JSONDecodeError:
                            logger.error(f"MCP Stream {self.stream_id}: JSON decode error from client: {message_bytes!r}", exc_info=True)
                            await self.send_queue.put(MCPResponseModel(id=None, error={"code": -32700, "message": "Parse error"}))
                        except Exception as e_p:
                            logger.error(f"MCP Stream {self.stream_id}: Error processing client message: {message_bytes!r}, Error: {e_p}", exc_info=True)
                            error_id_p = None
                            try: error_id_p = json.loads(message_bytes.decode('utf-8')).get("id")
                            except: pass
                            await self.send_queue.put(MCPResponseModel(id=error_id_p, error={"code": -32603, "message": "Internal error processing message"}))
            logger.info(f"MCP Stream {self.stream_id}: Client request stream finished.")
        except asyncio.CancelledError:
            logger.info(f"MCP Stream {self.stream_id}: Client reader task _read_from_client cancelled.")
            raise
        except Exception as e_read_outer:
            logger.error(f"MCP Stream {self.stream_id}: Error reading from client stream: {e_read_outer}", exc_info=True)
            try: await self.send_queue.put(MCPResponseModel(id=None, error={"code": -32000, "message": "Hub stream read error"}))
            except: pass
        finally:
            logger.info(f"MCP Stream {self.stream_id}: Client reader task _read_from_client finally block.")


    async def _read_from_client_wrapper(self):
        try:
            await self._read_from_client()
        except asyncio.CancelledError:
            logger.info(f"MCP Stream {self.stream_id}: _read_from_client_wrapper: Reader task cancelled.")
            raise
        except Exception as e:
            logger.error(f"MCP Stream {self.stream_id}: _read_from_client_wrapper: UNEXPECTED Error in reader: {e}", exc_info=True)
        finally:
            logger.info(f"MCP Stream {self.stream_id}: _read_from_client_wrapper: Reader finished. Signalling.")
            self.reader_task_finished_event.set()
            await self._check_and_manage_outstanding_event()

    async def _cleanup(self):
        logger.info(
            f"MCP Stream {self.stream_id}: _cleanup: Cancelling tasks. ReaderDone={self.reader_main_task.done() if self.reader_main_task else 'N/A'}, "
            f"Pending={len(self.pending_tasks)}, BgNotifs={len(self.background_notifications)}, EventWaits={len(self.current_event_wait_tasks)}"
        )

        all_tasks_to_cancel_at_end: List[asyncio.Task] = []

        if self.reader_main_task and not self.reader_main_task.done():
            self.reader_main_task.cancel()
            all_tasks_to_cancel_at_end.append(self.reader_main_task)

        task_collections_to_cancel = [
            list(self.pending_tasks.values()),
            list(self.background_notifications),
            list(self.current_event_wait_tasks) # Event waits created in the loop
        ]

        for task_collection in task_collections_to_cancel:
            for task_obj in task_collection:
                if task_obj and not task_obj.done():
                    task_obj.cancel()
                    all_tasks_to_cancel_at_end.append(task_obj)

        if all_tasks_to_cancel_at_end:
            logger.debug(f"MCP Stream {self.stream_id}: _cleanup: Gathering {len(all_tasks_to_cancel_at_end)} tasks for cancellation.")
            await asyncio.gather(*all_tasks_to_cancel_at_end, return_exceptions=True)
            logger.debug(f"MCP Stream {self.stream_id}: _cleanup: Gathered cancelled tasks.")
        else:
            logger.debug(f"MCP Stream {self.stream_id}: _cleanup: No tasks needed explicit cancellation.")

        try:
            await self.send_queue.put(None)
            logger.debug(f"MCP Stream {self.stream_id}: _cleanup: Put None sentinel on send_queue.")
        except Exception as e_final_q:
            logger.error(f"MCP Stream {self.stream_id}: Error putting None sentinel on send_queue in _cleanup: {e_final_q}")

        if self.app_state and hasattr(self.app_state, 'active_mcp_streams') and self.stream_id in self.app_state.active_mcp_streams:
            del self.app_state.active_mcp_streams[self.stream_id]
        logger.info(f"MCP Stream {self.stream_id}: Handler fully cleaned up and removed from active streams.")


    async def handle_stream(self):
        logger.info(f"MCP Stream {self.stream_id}: Handler initiated.")

        async with httpx.AsyncClient(timeout=MCP_CLIENT_TIMEOUT, verify=True) as client:
            self.agent_http_client = client # Make client available to methods

            self.reader_main_task = asyncio.create_task(
                self._read_from_client_wrapper(),
                name=f"MCPStreamReader-{self.stream_id}"
            )
            try:
                if self.initial_messages:
                    logger.debug(f"MCP Stream {self.stream_id}: Processing {len(self.initial_messages)} initial POSTed messages.")
                    for msg_data in self.initial_messages:
                        if isinstance(msg_data, dict) and msg_data.get("jsonrpc") == "2.0":
                            await self._process_mcp_request(msg_data)
                        elif isinstance(msg_data, dict) and msg_data.get("error"): # Hub generated error pre-stream
                            await self.send_queue.put(MCPResponseModel(**msg_data))
                            if msg_data.get("id") is not None:
                                self.expected_response_ids.discard(msg_data.get("id"))
                        else:
                            logger.warning(f"MCP Stream {self.stream_id}: Invalid initial non-JSONRPC message in POST: {msg_data}")
                            error_id_initial = msg_data.get("id") if isinstance(msg_data, dict) else None
                            await self.send_queue.put(MCPResponseModel(id=error_id_initial, error={"code": -32600, "message": "Invalid initial request structure"}))
                            if error_id_initial is not None:
                                self.expected_response_ids.discard(error_id_initial)
                    await self._check_and_manage_outstanding_event() # Check after processing initial messages

                while not (self.reader_task_finished_event.is_set() and self.outstanding_responses_event.is_set()):
                    active_tasks_for_wait = []
                    if self.reader_main_task and not self.reader_main_task.done():
                        active_tasks_for_wait.append(self.reader_main_task)

                    active_tasks_for_wait.extend(t for t in self.pending_tasks.values() if t and not t.done())
                    active_tasks_for_wait.extend(t for t in self.background_notifications if t and not t.done())

                    self.current_event_wait_tasks = [
                        asyncio.create_task(self.reader_task_finished_event.wait(), name=f"EventWait-ReaderDone-{self.stream_id}"),
                        asyncio.create_task(self.outstanding_responses_event.wait(), name=f"EventWait-OutstandingDone-{self.stream_id}")
                    ]
                    tasks_to_wait_on_loop = active_tasks_for_wait + self.current_event_wait_tasks

                    if not tasks_to_wait_on_loop:
                        logger.debug(f"MCP Stream {self.stream_id}: Main loop: No active tasks or event waits. Breaking.")
                        break

                    logger.debug(
                        f"MCP Stream {self.stream_id}: Main loop waiting. ReaderDone={self.reader_task_finished_event.is_set()}, "
                        f"OutstandingDone={self.outstanding_responses_event.is_set()}, PendingTasks={len(self.pending_tasks)}, "
                        f"ExpectedIDs={len(self.expected_response_ids)}, WaitTasks={len(tasks_to_wait_on_loop)}"
                    )

                    done_in_loop, _ = await asyncio.wait(
                        tasks_to_wait_on_loop, timeout=1.0, return_when=asyncio.FIRST_COMPLETED
                    )

                    for task_done in done_in_loop:
                        if task_done in self.background_notifications and task_done.done():
                            if task_done.exception():
                                logger.error(f"MCP Stream {self.stream_id}: Background notification task failed: {task_done.exception()}", exc_info=task_done.exception())
                            try: self.background_notifications.remove(task_done)
                            except ValueError: pass

                    # Explicitly re-check after wait if an event might have changed state
                    # or a task finished that affects conditions.
                    # _check_and_manage_outstanding_event is called internally by task completions,
                    # but an explicit call here ensures loop conditions are re-evaluated promptly.
                    await self._check_and_manage_outstanding_event()


                logger.info(f"MCP Stream {self.stream_id}: Loop exited. ReaderDone={self.reader_task_finished_event.is_set()}, OutstandingDone={self.outstanding_responses_event.is_set()}.")

            except asyncio.CancelledError:
                logger.info(f"MCP Stream {self.stream_id}: Main handler_stream task was cancelled.")
                # Cancellation will propagate to self.reader_main_task if it's still running.
            except Exception as e_handler_outer:
                logger.error(f"MCP Stream {self.stream_id}: Unhandled error in MCP stream main handler: {e_handler_outer}", exc_info=True)
                try: await self.send_queue.put(MCPResponseModel(id=None, error={"code": -32000, "message": "Hub internal stream error"}))
                except Exception: pass
            finally:
                logger.info(f"MCP Stream {self.stream_id}: Main handler_stream finally block. Performing cleanup.")
                await self._cleanup()


# ----- FASTAPI APP -----

app = FastAPI(
    title="AIRA Hub",
    description="A decentralized registry for MCP and A2A tools",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS", "DELETE"], allow_headers=["*"],
)

@app.middleware("http")
async def log_requests_middleware(request: Request, call_next):
    start_time = time.time()
    path, method, client_host = request.url.path, request.method, request.client.host if request.client else "Unknown"
    logger.info(f"Request START: {method} {path} from {client_host}")
    try:
        response = await call_next(request)
        duration, status_code = time.time() - start_time, response.status_code
        log_level = logging.INFO if status_code < 400 else logging.WARNING if status_code < 500 else logging.ERROR
        logger.log(log_level, f"Request END: {method} {path} - Status {status_code} ({duration:.4f}s)")
        return response
    except Exception as e:
        duration = time.time() - start_time
        logger.error(f"Request FAILED: {method} {path} after {duration:.4f}s. Error: {e}", exc_info=True)
        raise

# ----- DEPENDENCIES -----

def get_storage_dependency(request: Request) -> MongoDBStorage:
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
        request_obj: Request, agent_payload: AgentRegistration,
        background_tasks: BackgroundTasks, storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    logger.info(f"Received registration request for agent: {agent_payload.name} at URL: {agent_payload.url}")

    if "a2a" in agent_payload.aira_capabilities and agent_payload.url:
        logger.info(f"Agent {agent_payload.name} has 'a2a' capability. Fetching A2A Agent Card.")
        base_url = agent_payload.url.rstrip('/')
        agent_card_url = f"{base_url}/.well-known/agent.json" if not base_url.endswith("/.well-known/agent.json") and not base_url.endswith("/.well-known") else \
                         f"{base_url}/agent.json" if base_url.endswith("/.well-known") else \
                         f"{base_url}/.well-known/agent.json" # Default case
        try:
            async with httpx.AsyncClient(timeout=HUB_TIMEOUT) as client:
                logger.debug(f"Fetching A2A Agent Card from: {agent_card_url}")
                response = await client.get(agent_card_url)
                response.raise_for_status()
                a2a_card_data = response.json()
                logger.info(f"Successfully fetched A2A Agent Card for {agent_payload.name} from {agent_card_url}")

                translated_mcp_tools = []
                if "skills" in a2a_card_data and isinstance(a2a_card_data["skills"], list):
                    for a2a_skill_dict in a2a_card_data["skills"]:
                        if not isinstance(a2a_skill_dict, dict) or not a2a_skill_dict.get("id") or not a2a_skill_dict.get("name"):
                            logger.warning(f"Skipping malformed A2A skill from {agent_payload.name}'s card: {a2a_skill_dict}")
                            continue
                        mcp_tool_name = f"{agent_payload.name.replace(' ', '_')}_A2A_{a2a_skill_dict['id']}"
                        input_schema_for_mcp = a2a_skill_dict.get("parameters", {"type": "object", "properties": {}})
                        mcp_tool_def = MCPTool(
                            name=mcp_tool_name, description=a2a_skill_dict.get("description", "No description provided."),
                            inputSchema=input_schema_for_mcp,
                            annotations={ # Critical for Hub to know how to call it via MCP stream
                                "aira_bridge_type": "a2a",
                                "aira_a2a_target_skill_id": a2a_skill_dict['id'],
                                "aira_a2a_agent_url": agent_payload.url # A2A agent's base URL
                            }
                        )
                        translated_mcp_tools.append(mcp_tool_def)
                    agent_payload.mcp_tools = translated_mcp_tools
                    logger.info(f"Translated {len(agent_payload.mcp_tools)} A2A skills to MCP tools for {agent_payload.name}.")
                else:
                    logger.warning(f"No 'skills' array in A2A Agent Card for {agent_payload.name}. No MCP tools from A2A.")
                    agent_payload.mcp_tools = []
        except (httpx.HTTPStatusError, httpx.RequestError, json.JSONDecodeError, Exception) as e_card_proc:
            logger.error(f"Error processing A2A Agent Card for {agent_payload.name} from {agent_card_url}: {e_card_proc}", exc_info=True)
            agent_payload.mcp_tools = [] # Register without A2A-derived tools on error

    existing_agent = await storage_inst.get_agent_by_url(agent_payload.url)
    agent_to_save = agent_payload
    status_message = "registered"
    if existing_agent:
        agent_to_save.agent_id = existing_agent.agent_id
        agent_to_save.created_at = existing_agent.created_at
        if existing_agent.metrics and agent_to_save.metrics is None: agent_to_save.metrics = existing_agent.metrics
        status_message = "updated"
        logger.info(f"Agent {agent_to_save.name} (ID: {agent_to_save.agent_id}) found. Preparing update.")
    else:
        logger.info(f"New agent registration: {agent_to_save.name} at {agent_to_save.url} with new ID {agent_to_save.agent_id}")

    if agent_to_save.metrics is None: agent_to_save.metrics = AgentMetrics()
    agent_to_save.status = AgentStatus.ONLINE
    agent_to_save.last_seen = time.time()

    if not await storage_inst.save_agent(agent_to_save):
        logger.error(f"Failed to save agent {agent_to_save.name} (ID: {agent_to_save.agent_id}) to database.")
        raise HTTPException(status_code=500, detail="Failed to save agent to database")

    connection_manager = getattr(request_obj.app.state, 'connection_manager', None)
    if connection_manager:
        event_type = "agent_registered" if status_message == "registered" and not existing_agent else "agent_updated"
        background_tasks.add_task(
            connection_manager.broadcast_event, event_type,
            {"agent_id": agent_to_save.agent_id, "name": agent_to_save.name, "url": agent_to_save.url,
             "status": agent_to_save.status.value, "mcp_tools_count": len(agent_to_save.mcp_tools),
             "a2a_skills_count": len(agent_to_save.a2a_skills)}
        )
    logger.info(f"Agent {status_message} process completed for: {agent_to_save.name} (ID: {agent_to_save.agent_id})")
    return {"status": status_message, "agent_id": agent_to_save.agent_id, "url": agent_to_save.url,
            "discovered_mcp_tools_from_a2a": len(agent_to_save.mcp_tools) if "a2a" in agent_to_save.aira_capabilities else 0}

@app.post("/heartbeat/{agent_id}", tags=["Agents"])
async def heartbeat_endpoint(
        request: Request, agent_id: str, storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    agent = await storage_inst.get_agent(agent_id)
    if not agent: raise HTTPException(status_code=404, detail="Agent not found")
    now = time.time()
    updated = await storage_inst.update_agent_heartbeat(agent_id, now)
    if updated:
        status_changed = False
        if agent.status != AgentStatus.ONLINE:
            agent.status = AgentStatus.ONLINE
            # agent.last_seen = now # Ensure last_seen is also updated in the object if saving full
            await storage_inst.save_agent(agent) # Save full agent to update status and trigger cache logic
            status_changed = True
            logger.info(f"Agent {agent_id} status changed to ONLINE via heartbeat")
        if status_changed and hasattr(request.app.state, 'connection_manager'):
            await request.app.state.connection_manager.broadcast_event(
                "agent_status_updated",
                {"agent_id": agent_id, "name": agent.name, "url": agent.url, "status": AgentStatus.ONLINE.value}
            )
    return {"status": "ok"}

# --- Agent Discovery ---
@app.get("/agents", tags=["Discovery"])
async def list_agents_endpoint(
        request: Request, status: Optional[AgentStatus] = Query(None), category: Optional[str] = Query(None),
        tag: Optional[str] = Query(None), capability: Optional[str] = Query(None),
        offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000),
        storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    query = {}
    if status: query["status"] = status.value
    if category: query["category"] = category
    if tag: query["tags"] = tag
    if capability: query["aira_capabilities"] = capability
    total = await storage_inst.count_agents(query)
    agents = await storage_inst.search_agents(query, offset, limit)
    return {"total": total, "offset": offset, "limit": limit, "agents": agents}

@app.get("/agents/{agent_id}", tags=["Discovery"])
async def get_agent_endpoint(
        request: Request, agent_id: str, storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    agent = await storage_inst.get_agent(agent_id)
    if not agent: raise HTTPException(status_code=404, detail="Agent not found")
    return agent

@app.delete("/agents/{agent_id}", tags=["Agents"])
async def unregister_agent_endpoint(
        request: Request, agent_id: str, background_tasks: BackgroundTasks,
        storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    agent = await storage_inst.get_agent(agent_id)
    if not agent: raise HTTPException(status_code=404, detail="Agent not found")
    deleted = await storage_inst.delete_agent(agent_id)
    if deleted:
        if hasattr(request.app.state, 'connection_manager'):
            background_tasks.add_task(
                request.app.state.connection_manager.broadcast_event, "agent_unregistered",
                {"agent_id": agent_id, "name": agent.name, "url": agent.url}
            )
        logger.info(f"Agent unregistered: {agent.name} ({agent.url}) with ID {agent_id}")
        return {"status": "unregistered", "agent_id": agent_id}
    else:
        logger.warning(f"Attempted to unregister agent {agent_id} but deletion failed.")
        raise HTTPException(status_code=404, detail="Agent not found or failed to delete")

# --- Tools and Resources ---
@app.get("/tools", tags=["Tools"])
async def list_tools_endpoint(
        request: Request, agent_id: Optional[str] = None,
        storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    tools = []
    if agent_id:
        agent = await storage_inst.get_agent(agent_id)
        if not agent: raise HTTPException(status_code=404, detail="Agent not found")
        if agent.status != AgentStatus.ONLINE:
            logger.info(f"Attempted to list tools for offline agent {agent_id}")
            return {"tools": []}
        for tool in agent.mcp_tools:
            tools.append({
                "name": tool.name, "description": tool.description, "agent": agent.name,
                "agent_id": agent.agent_id, "inputSchema": tool.inputSchema, "annotations": tool.annotations
            })
    else:
        tools = await storage_inst.list_tools()
    return {"tools": tools}

@app.get("/tags", tags=["Discovery"])
async def list_tags_endpoint(
    request: Request, type: str = Query("agent", enum=["agent", "skill", "tool"]),
    storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    tags = set()
    if type == "agent":
        pipeline = [{"$unwind": "$tags"}, {"$group": {"_id": "$tags"}}]
        results = await storage_inst.db.agents.aggregate(pipeline).to_list(None)
        tags.update(item["_id"] for item in results if item["_id"])
    elif type == "skill":
        pipeline = [{"$unwind": "$a2a_skills"}, {"$unwind": "$a2a_skills.tags"}, {"$group": {"_id": "$a2a_skills.tags"}}]
        results = await storage_inst.db.agents.aggregate(pipeline).to_list(None)
        tags.update(item["_id"] for item in results if item["_id"])
    elif type == "tool":
        logger.warning("Tag type 'tool' requested, MCPTool model does not currently support tags. Returning empty.")
    return {"tags": sorted(list(tags))}

@app.get("/categories", tags=["Discovery"])
async def list_categories_endpoint(
        request: Request, storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    pipeline = [{"$match": {"category": {"$ne": None}}}, {"$group": {"_id": "$category"}}]
    results = await storage_inst.db.agents.aggregate(pipeline).to_list(None)
    categories = sorted([item["_id"] for item in results if item["_id"]])
    return {"categories": categories}

# --- Status and System ---
@app.get("/status", tags=["System"])
async def system_status_endpoint(request: Request):
    cm = getattr(request.app.state, 'connection_manager', None)
    msm = getattr(request.app.state, 'mcp_session_manager', None)
    connected_clients_count = len(cm.get_all_connections()) if cm else 0
    active_sessions_count = len(msm.active_sessions) if msm else 0
    start_time_attr = getattr(request.app.state, 'start_time', time.time()) # Default to now if not set

    if (getattr(request.app.state, 'storage_failed', False) or
        not hasattr(request.app.state, 'storage') or
        request.app.state.storage is None or
        request.app.state.storage.db is None):
        return {"status": "degraded - database unavailable", "uptime": time.time() - start_time_attr,
                "registered_agents": 0, "online_agents": 0, "available_tools": 0,
                "connected_clients": connected_clients_count, "active_sessions": active_sessions_count, "version": "1.0.0"}

    storage_inst: MongoDBStorage = request.app.state.storage
    registered_agents_count = await storage_inst.count_agents({})
    online_agents_count = await storage_inst.count_agents({
        "status": AgentStatus.ONLINE.value,
        # "last_seen": {"$gte": time.time() - DEFAULT_HEARTBEAT_TIMEOUT} # More accurate for "truly" online
    })

    # Efficient tool count using aggregation
    pipeline_tools = [
        {"$match": {"status": AgentStatus.ONLINE.value, "mcp_tools": {"$exists": True, "$ne": []}}},
        {"$project": {"num_tools": {"$size": "$mcp_tools"}}},
        {"$group": {"_id": None, "total_online_tools": {"$sum": "$num_tools"}}}
    ]
    tool_count_result = await storage_inst.db.agents.aggregate(pipeline_tools).to_list(None)
    available_tools_count = tool_count_result[0]["total_online_tools"] if tool_count_result else 0

    return {"status": "healthy", "uptime": time.time() - start_time_attr,
            "registered_agents": registered_agents_count, "online_agents": online_agents_count,
            "available_tools": available_tools_count, "connected_clients": connected_clients_count,
            "active_sessions": active_sessions_count, "version": "1.0.0"}

@app.get("/health", include_in_schema=False)
async def health_check_endpoint(request: Request):
    db_status = "ok"
    storage_inst = getattr(request.app.state, 'storage', None)
    if not storage_inst or not storage_inst.mongo_db_client: db_status = "error - not initialized"
    else:
        try: await storage_inst.mongo_db_client.admin.command('ping')
        except Exception as e:
            logger.warning(f"Health check: DB ping failed: {e}")
            db_status = "error - unreachable"
    overall_status = "ok" if db_status == "ok" else "degraded"
    return {"status": overall_status, "database": db_status}

# --- MCP Streamable HTTP Endpoint ---
@app.post("/mcp/stream", tags=["MCP (Streamable HTTP)"])
async def mcp_stream_endpoint(
        request: Request, storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    stream_id = str(uuid.uuid4())
    client_host = request.client.host if request.client else "Unknown"
    logger.info(f"MCP Stream {stream_id}: New connection from {client_host}")

    initial_messages = []
    try:
        initial_body_bytes = await request.body()
        if initial_body_bytes.strip():
            initial_body_str = initial_body_bytes.decode('utf-8')
            logger.debug(f"MCP Stream {stream_id}: Initial POST body: {initial_body_str}")
            parsed_initial_body = json.loads(initial_body_str)
            if isinstance(parsed_initial_body, list): initial_messages.extend(parsed_initial_body)
            elif isinstance(parsed_initial_body, dict): initial_messages.append(parsed_initial_body)
            else:
                logger.warning(f"MCP Stream {stream_id}: Invalid initial POST body type: {type(parsed_initial_body)}")
                initial_messages.append({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid initial request format"}})
        else: logger.debug(f"MCP Stream {stream_id}: Initial POST body empty")
    except json.JSONDecodeError:
        logger.error(f"MCP Stream {stream_id}: Invalid JSON in initial POST body", exc_info=True)
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error in initial request"}})
    except Exception as e_init:
        logger.error(f"MCP Stream {stream_id}: Error processing initial body: {e_init}", exc_info=True)
        return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": "Internal error processing initial request"}})

    send_queue = asyncio.Queue()
    mcp_session = MCPStreamSession(
        stream_id, initial_messages, request.stream(), send_queue, storage_inst, request.app.state
    )
    handler_task = asyncio.create_task(
        mcp_session.handle_stream(),
        name=f"MCPStreamSession-{stream_id}"
    )
    if hasattr(request.app.state, 'active_mcp_streams'):
        request.app.state.active_mcp_streams[stream_id] = handler_task

    async def response_generator():
        try:
            while True:
                item = await send_queue.get()
                if item is None:
                    logger.info(f"MCP Stream {stream_id}: Closing response generator (None sentinel)")
                    break
                if isinstance(item, MCPResponseModel):
                    try:
                        json_resp_str = item.model_dump_json(exclude_none=True)
                        logger.debug(f"MCP Stream {stream_id}: SEND: {json_resp_str}")
                        yield json_resp_str.encode('utf-8') + STREAM_SEPARATOR
                    except Exception as e_ser: logger.error(f"MCP Stream {stream_id}: Error serializing MCPResponseModel: {e_ser}", exc_info=True)
                else: logger.warning(f"MCP Stream {stream_id}: Unknown item type in send_queue: {type(item)}")
                send_queue.task_done()
        except asyncio.CancelledError: logger.info(f"MCP Stream {stream_id}: Response generator cancelled")
        except Exception as e_gen_outer: logger.error(f"MCP Stream {stream_id}: Error in response generator: {e_gen_outer}", exc_info=True)
        finally:
            logger.info(f"MCP Stream {stream_id}: Response generator finished.")
            if handler_task and not handler_task.done():
                logger.info(f"MCP Stream {stream_id}: Cancelling handler task from response_generator finally.")
                handler_task.cancel()
                try: await handler_task
                except asyncio.CancelledError: logger.info(f"MCP Stream {stream_id}: Handler task successfully cancelled by response_generator.")
                except Exception as e_task_cleanup: logger.error(f"MCP Stream {stream_id}: Error during handler task cleanup: {e_task_cleanup}", exc_info=True)
    return StreamingResponse(response_generator(), media_type="application/json-seq")

# --- A2A Protocol Endpoints ---
@app.post("/a2a/discover", tags=["A2A"])
async def discover_a2a_agents_endpoint(
        request_obj: Request, query: DiscoverQuery, storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    query_dict = {}
    if query.status: query_dict["status"] = query.status.value
    if query.category: query_dict["category"] = query.category
    if query.agent_tags: query_dict["tags"] = {"$in": query.agent_tags}
    if query.capabilities: query_dict["aira_capabilities"] = {"$all": query.capabilities}

    skill_query_parts = []
    if query.skill_id: skill_query_parts.append({"a2a_skills.id": query.skill_id})
    if query.skill_tags: skill_query_parts.append({"a2a_skills.tags": {"$in": query.skill_tags}})

    if skill_query_parts:
        if "$and" not in query_dict: query_dict["$and"] = []
        query_dict["$and"].append({"aira_capabilities": "a2a"})
        query_dict["$and"].append({"$or": skill_query_parts})
    elif query.skill_id is not None or query.skill_tags is not None:
        query_dict["aira_capabilities"] = "a2a"

    total = await storage_inst.count_agents(query_dict)
    agents_list = await storage_inst.search_agents(query_dict, query.offset, query.limit)
    return {"total": total, "offset": query.offset, "limit": query.limit, "agents": agents_list}

@app.get("/a2a/skills", tags=["A2A"])
async def list_a2a_skills_endpoint(
        request_obj: Request, agent_id: Optional[str] = None, tag: Optional[str] = None,
        storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    skills_list = []
    query_dict = {"aira_capabilities": "a2a", "status": AgentStatus.ONLINE.value}
    if agent_id: query_dict["agent_id"] = agent_id
    agents_found = await storage_inst.search_agents(query_dict)
    for agent_obj in agents_found:
        for skill in agent_obj.a2a_skills:
            if tag is None or (skill.tags and tag in skill.tags):
                skills_list.append({
                    "id": skill.id, "name": skill.name, "description": skill.description, "version": skill.version,
                    "agent_name": agent_obj.name, "agent_id": agent_obj.agent_id, "tags": skill.tags,
                    "parameters": skill.parameters, "examples": skill.examples
                })
    return {"skills": skills_list}

async def forward_a2a_task_to_agent(agent_url: str, task_payload: Dict[str, Any], hub_task_id: str):
    """Background task to forward A2A task to the agent."""
    try:
        # A2A agents expect tasks at their root URL typically for tasks/send
        # Ensure agent_url is the base URL (e.g. http://agent.com)
        # The method is tasks/send which is part of the JSON-RPC payload.
        target_url = agent_url.rstrip('/') # Agent's root A2A endpoint

        logger.info(f"A2A Forwarding (Task {hub_task_id}): Sending to agent at {target_url}. Payload: {json.dumps(task_payload)}")
        async with httpx.AsyncClient(timeout=A2A_FORWARD_TIMEOUT) as client:
            response = await client.post(target_url, json=task_payload)
            response.raise_for_status()
            agent_response_data = response.json()
            logger.info(f"A2A Forwarding (Task {hub_task_id}): Agent at {target_url} responded: {agent_response_data}")
            # TODO: Optionally, update hub_task_id status based on agent_response_data
            # e.g. if agent returns its own task ID or an immediate error.
    except httpx.HTTPStatusError as e_http:
        logger.error(f"A2A Forwarding (Task {hub_task_id}): HTTP error sending to agent {agent_url}: {e_http.response.status_code} - {e_http.response.text[:200]}", exc_info=True)
    except httpx.RequestError as e_req:
        logger.error(f"A2A Forwarding (Task {hub_task_id}): Request error sending to agent {agent_url}: {e_req}", exc_info=True)
    except Exception as e:
        logger.error(f"A2A Forwarding (Task {hub_task_id}): Unexpected error sending to agent {agent_url}: {e}", exc_info=True)

@app.post("/a2a/tasks/send", tags=["A2A"], status_code=status.HTTP_202_ACCEPTED, response_model=A2ATask)
async def a2a_send_task_endpoint(
        request_obj: Request, task_submission: Dict[str, Any], background_tasks: BackgroundTasks,
        storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    agent_id = task_submission.get("agent_id")
    skill_id = task_submission.get("skill_id")
    task_message = task_submission.get("message") # This is the A2A message (e.g. {"role": "user", "parts": [...]})
    session_id = task_submission.get("session_id")

    if not all([agent_id, skill_id, task_message]) or not isinstance(task_message, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="agent_id, skill_id, and 'message' (object) required")

    agent = await storage_inst.get_agent(agent_id)
    if not agent: raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if agent.status != AgentStatus.ONLINE: raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Agent {agent.name} is offline")
    if "a2a" not in agent.aira_capabilities: raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Agent {agent.name} does not support A2A")

    found_skill = next((s for s in agent.a2a_skills if s.id == skill_id), None)
    if not found_skill: raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Skill {skill_id} not found on agent {agent.name}")

    task_obj = A2ATask(
        agent_id=agent_id, skill_id=skill_id, session_id=session_id, original_message=task_message,
        current_status=A2ATaskStatusUpdate(state=A2ATaskState.SUBMITTED, message="Task submitted to hub."),
        history=[{"role": "user", "timestamp": datetime.now(timezone.utc).isoformat(), "content": task_message}]
    )
    if not await storage_inst.save_a2a_task(task_obj):
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to save task")

    logger.info(f"A2A Task {task_obj.id} submitted for agent {agent.name} ({agent_id}), skill {skill_id}")

    # Prepare payload for forwarding to the A2A agent
    # The agent's A2A endpoint will expect a JSON-RPC call for "tasks/send"
    a2a_forward_payload = {
        "jsonrpc": "2.0",
        "method": "tasks/send",
        "params": {
            "id": task_obj.id,  # Hub's task ID, useful for agent context
            "message": task_obj.original_message # The message received by the hub
            # skill_id is often part of the message.parts[data] or agent handles routing based on its config
        },
        "id": str(uuid.uuid4()) # JSON-RPC ID for this specific HTTP call to agent
    }

    background_tasks.add_task(
        forward_a2a_task_to_agent, agent.url, a2a_forward_payload, task_obj.id
    )
    logger.info(f"A2A Task {task_obj.id}: Queued for forwarding to agent {agent.name} at {agent.url}")
    return task_obj

@app.get("/a2a/tasks/{task_id}", tags=["A2A"], response_model=A2ATask)
async def a2a_get_task_endpoint(
        request_obj: Request, task_id: str, storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    task_obj = await storage_inst.get_a2a_task(task_id)
    if not task_obj: raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="A2A Task not found")
    # Mock state transition removed - should be driven by agent updates
    return task_obj

# --- Analytics Endpoints (Optimized) ---
@app.get("/analytics/summary", tags=["Analytics"])
async def analytics_summary_endpoint(
    request_obj: Request, storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    if not storage_inst.db: raise HTTPException(status_code=503, detail="Database unavailable")

    total_agents = await storage_inst.count_agents({})

    # Status counts
    pipeline_status = [{"$group": {"_id": "$status", "count": {"$sum": 1}}}]
    status_results = await storage_inst.db.agents.aggregate(pipeline_status).to_list(None)
    status_counts = {s.value: 0 for s in AgentStatus}
    for item in status_results: status_counts[item["_id"]] = item["count"]

    # Capability counts
    pipeline_caps = [{"$unwind": "$aira_capabilities"}, {"$group": {"_id": "$aira_capabilities", "count": {"$sum": 1}}}]
    cap_results = await storage_inst.db.agents.aggregate(pipeline_caps).to_list(None)
    capability_counts = {item["_id"]: item["count"] for item in cap_results}

    # Total tools & skills
    pipeline_tools = [{"$match": {"mcp_tools": {"$exists": True, "$ne": []}}}, {"$project": {"num_tools": {"$size": "$mcp_tools"}}}, {"$group": {"_id": None, "total": {"$sum": "$num_tools"}}}]
    tools_res = await storage_inst.db.agents.aggregate(pipeline_tools).to_list(None)
    total_tools_count = tools_res[0]["total"] if tools_res else 0

    pipeline_skills = [{"$match": {"a2a_skills": {"$exists": True, "$ne": []}}}, {"$project": {"num_skills": {"$size": "$a2a_skills"}}}, {"$group": {"_id": None, "total": {"$sum": "$num_skills"}}}]
    skills_res = await storage_inst.db.agents.aggregate(pipeline_skills).to_list(None)
    total_skills_count = skills_res[0]["total"] if skills_res else 0

    # Top tags (agent + skill tags combined)
    pipeline_tags = [
        {"$project": {"all_tags": {"$concatArrays": [
            {"$ifNull": ["$tags", []]},
            {"$reduce": {"input": {"$ifNull": ["$a2a_skills", []]}, "initialValue": [], "in": {"$concatArrays": ["$$value", {"$ifNull": ["$$this.tags", []]}]}}}
        ]}}},
        {"$unwind": "$all_tags"}, {"$group": {"_id": "$all_tags", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}}, {"$limit": 10}
    ]
    top_tags_results = await storage_inst.db.agents.aggregate(pipeline_tags).to_list(None)
    top_tags_dict = {item["_id"]: item["count"] for item in top_tags_results}

    agents_added_today_count = await storage_inst.count_agents({"created_at": {"$gte": time.time() - 86400}})

    num_a2a_tasks = await storage_inst.db.tasks.count_documents({})
    num_completed_a2a_tasks = await storage_inst.db.tasks.count_documents({"current_status.state": A2ATaskState.COMPLETED.value})

    return {"total_agents": total_agents, "status_counts": status_counts, "capability_counts": capability_counts,
            "total_tools": total_tools_count, "total_skills": total_skills_count, "top_tags": top_tags_dict,
            "agents_added_today": agents_added_today_count, "total_a2a_tasks_recorded": num_a2a_tasks,
            "completed_a2a_tasks_recorded": num_completed_a2a_tasks}

@app.get("/analytics/activity", tags=["Analytics"])
async def analytics_activity_endpoint(
    request_obj: Request, days: int = Query(7, ge=1, le=30),
    storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    if not storage_inst.db: raise HTTPException(status_code=503, detail="Database unavailable")

    activity_data_list = []
    now_ts = time.time()
    now_dt = datetime.now(timezone.utc)
    day_seconds = 86400

    for i in range(days):
        range_end_dt = now_dt - timedelta(days=i)
        range_start_dt = now_dt - timedelta(days=i + 1)
        day_date_str = range_start_dt.strftime("%Y-%m-%d")

        new_registrations_count = await storage_inst.db.agents.count_documents({
            "created_at": {"$gte": range_start_dt.timestamp(), "$lt": range_end_dt.timestamp()}
        })

        # Approx active agents: seen in day OR (online AND last_seen < start_of_day AND last_seen within heartbeat from now_ts)
        query_seen_in_day = {"last_seen": {"$gte": range_start_dt.timestamp(), "$lt": range_end_dt.timestamp()}}
        query_online_and_recent_overall = {
            "status": AgentStatus.ONLINE.value,
            "last_seen": {"$lt": range_start_dt.timestamp(), "$gte": now_ts - DEFAULT_HEARTBEAT_TIMEOUT}
        }
        active_agents_query = {"$or": [query_seen_in_day, query_online_and_recent_overall]}
        approx_active_agents_count = await storage_inst.db.agents.count_documents(active_agents_query)

        a2a_tasks_created_count = await storage_inst.db.tasks.count_documents({
            "created_at": {"$gte": range_start_dt, "$lt": range_end_dt}
        })
        activity_data_list.append({
            "date": day_date_str, "new_agent_registrations": new_registrations_count,
            "approx_active_agents": approx_active_agents_count, "a2a_tasks_created": a2a_tasks_created_count
        })
    return {"activity": list(reversed(activity_data_list))}


# --- Admin Endpoints --- (Largely unchanged, kept for brevity, assuming they are okay)
class AdminSyncPayload(BaseModel): hub_urls: List[str]
@app.post("/admin/sync_agents", tags=["Admin"])
async def admin_sync_agents_endpoint(payload: AdminSyncPayload, storage_inst: MongoDBStorage = Depends(get_storage_dependency)):
    # ... (Sync logic as in original, can be further optimized if needed)
    hub_urls = payload.hub_urls; results_map = {}
    async with httpx.AsyncClient(timeout=30.0) as http_client:
        for hub_url_str in hub_urls:
            if not hub_url_str.startswith(('http://', 'https://')):
                results_map[hub_url_str] = {"status": "error", "message": "Invalid hub URL format."}; logger.warning(f"Invalid hub URL for sync: {hub_url_str}"); continue
            try:
                sync_url_str = urllib.parse.urljoin(hub_url_str.rstrip('/') + '/', "agents")
                logger.info(f"Syncing agents from {sync_url_str}")
                response = await http_client.get(sync_url_str)
                if response.status_code != 200:
                    logger.warning(f"Failed to get agents from {hub_url_str}: HTTP {response.status_code} - {response.text}")
                    results_map[hub_url_str] = {"status": "error", "message": f"Failed to get agents: HTTP {response.status_code}", "details": response.text[:500]}; continue
                remote_data_dict = response.json()
                remote_agents_list_data = remote_data_dict.get("agents", [])
                if not isinstance(remote_agents_list_data, list):
                    logger.warning(f"Invalid agent data format from {hub_url_str}. Expected list under 'agents'."); results_map[hub_url_str] = {"status": "error", "message": "Invalid agent data format."}; continue
                registered_c, skipped_c, failed_c = 0,0,0
                for agent_data_dict in remote_agents_list_data:
                    if not isinstance(agent_data_dict, dict): failed_c +=1; continue
                    try:
                        if not agent_data_dict.get('url') or not agent_data_dict.get('name'): failed_c += 1; continue
                        if await storage_inst.get_agent_by_url(agent_data_dict['url']): skipped_c += 1; continue
                        agent_to_create = AgentRegistration(
                            url=agent_data_dict['url'], name=agent_data_dict['name'], description=agent_data_dict.get('description'), version=agent_data_dict.get('version', "1.0.0"),
                            mcp_tools=[MCPTool(**t) for t in agent_data_dict.get('mcp_tools', []) if isinstance(t, dict)],
                            a2a_skills=[A2ASkill(**s) for s in agent_data_dict.get('a2a_skills', []) if isinstance(s, dict)],
                            aira_capabilities=agent_data_dict.get('aira_capabilities', []), tags=agent_data_dict.get('tags', []), category=agent_data_dict.get('category'),
                            provider=agent_data_dict.get('provider'), mcp_url=agent_data_dict.get('mcp_url'), mcp_sse_url=agent_data_dict.get('mcp_sse_url'),
                            mcp_stream_url=agent_data_dict.get('mcp_stream_url'), status=AgentStatus.ONLINE, last_seen=time.time()
                        )
                        await storage_inst.save_agent(agent_to_create); registered_c += 1; logger.info(f"Synced new agent: {agent_to_create.name} from {hub_url_str}")
                    except ValidationError as e_val: logger.error(f"Validation error processing remote agent from {hub_url_str}: {e_val}", exc_info=False); failed_c += 1
                    except Exception as e_agent_proc: logger.error(f"Error processing remote agent from {hub_url_str}: {e_agent_proc}", exc_info=True); failed_c += 1
                results_map[hub_url_str] = {"status": "success", "registered_new": registered_c, "skipped_existing": skipped_c, "failed_to_process": failed_c, "total_remote_agents_processed": len(remote_agents_list_data)}
            except httpx.RequestError as e_req_outer: results_map[hub_url_str] = {"status": "error", "message": f"Network error: {str(e_req_outer)}"}
            except json.JSONDecodeError as e_json_outer: results_map[hub_url_str] = {"status": "error", "message": "Failed to parse JSON response."}
            except Exception as e_hub_outer: results_map[hub_url_str] = {"status": "error", "message": f"Internal processing error: {str(e_hub_outer)}"}
    return {"sync_results": results_map}

class AdminCleanupPayload(BaseModel):
    agent_threshold_seconds: int = Field(DEFAULT_HEARTBEAT_TIMEOUT * 3, ge=60)
    session_threshold_seconds: int = Field(3600, ge=60)
@app.post("/admin/cleanup", tags=["Admin"])
async def admin_cleanup_endpoint(payload: AdminCleanupPayload, request_obj: Request, storage_inst: MongoDBStorage = Depends(get_storage_dependency)):
    # ... (Cleanup logic as in original, leverages periodic cleanup's DB efficiency implicitly if run after)
    # This manual cleanup could also use the more efficient DB queries for agent removal + refresh_tool_cache
    agent_threshold_sec, session_threshold_sec = payload.agent_threshold_seconds, payload.session_threshold_seconds
    now, agent_removed_count, delete_errors, session_removed_count = time.time(), 0, [], 0

    if storage_inst and storage_inst.db:
        query_stale_agents = {"last_seen": {"$lt": now - agent_threshold_sec}} # Also consider status != OFFLINE
        # To correctly use storage.delete_agent and thus tool_cache, we'd iterate.
        # For efficiency, similar to periodic_cleanup:
        agents_to_remove_docs = await storage_inst.db.agents.find(query_stale_agents, {"agent_id": 1}).to_list(None)
        agent_ids_to_remove = [doc["agent_id"] for doc in agents_to_remove_docs]

        if agent_ids_to_remove:
            logger.info(f"Admin Cleanup: Found {len(agent_ids_to_remove)} stale agents to remove.")
            # To ensure tool_cache is updated correctly, it's better to call delete_agent for each
            # Or, do a bulk update to OFFLINE, then delete those marked OFFLINE if policy is full removal.
            # For now, let's stick to individual deletion for cache safety or assume periodic will handle offline marking.
            # Simplified: If we just want to *remove* very old ones:
            result = await storage_inst.db.agents.delete_many(query_stale_agents)
            agent_removed_count = result.deleted_count
            if agent_removed_count > 0:
                logger.info(f"Admin Cleanup: Deleted {agent_removed_count} very stale agents.")
                await storage_inst.refresh_tool_cache() # Crucial after bulk delete
        else:
            logger.info("Admin Cleanup: No very stale agents found for deletion based on threshold.")

    mcp_session_manager = getattr(request_obj.app.state, 'mcp_session_manager', None)
    if mcp_session_manager: session_removed_count = mcp_session_manager.cleanup_stale_sessions(session_threshold_sec)
    return {"status": "success", "agents_removed": agent_removed_count, "agent_removal_errors": len(delete_errors),
            "error_details": delete_errors, "sessions_removed": session_removed_count}

class AdminBroadcastPayload(BaseModel): message: str; event_type: str = "admin_message"
@app.post("/admin/broadcast", tags=["Admin"])
async def admin_broadcast_endpoint(payload: AdminBroadcastPayload, request_obj: Request, background_tasks: BackgroundTasks):
    # ... (Broadcast logic as in original)
    connection_mgr = getattr(request_obj.app.state, 'connection_manager', None)
    if not connection_mgr: raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Connection manager not available")
    broadcast_data_dict = {"message": payload.message, "timestamp": time.time()}
    background_tasks.add_task(connection_mgr.broadcast_event, payload.event_type, broadcast_data_dict)
    recipients_queued_count = len(connection_mgr.get_all_connections())
    logger.info(f"Admin broadcast '{payload.event_type}' queued for {recipients_queued_count} clients.")
    return {"status": "broadcast_queued", "recipients_estimated": recipients_queued_count}

# --- UI Endpoint ---
@app.get("/ui", tags=["UI"], include_in_schema=False)
async def ui_dashboard_endpoint(request: Request):
    return JSONResponse(content={"message": "AIRA Hub UI not implemented. Refer to /docs or /redoc."})

# --- Custom Agent Connect Endpoints (Legacy - unchanged) ---
class CustomConnectStreamParams(BaseModel): agent_url: str; name: str; aira_capabilities: Optional[str] = None
@app.post("/connect/stream", tags=["Custom Agent Connect (Legacy)"])
async def connect_stream_custom_endpoint(params: CustomConnectStreamParams = Depends(), request_obj: Request = Request, storage_inst: MongoDBStorage = Depends(get_storage_dependency)):
    # ... (Original /connect/stream logic - kept for compatibility, but marked as legacy)
    logger.warning("/connect/stream is a custom, non-standard endpoint. Prefer standard registration.")
    agent_url, name, capabilities_list = params.agent_url, params.name, params.aira_capabilities.split(",") if params.aira_capabilities else []
    async def event_generator_custom():
        async with httpx.AsyncClient(timeout=None) as http_client_custom:
            try:
                logger.info(f"Custom Connect: Proxying SSE from agent: {agent_url}")
                async with http_client_custom.stream("GET", agent_url, headers={"Accept": "text/event-stream"}) as response_stream:
                    if response_stream.status_code != 200:
                        err_msg = f"Custom Connect: Agent stream {agent_url} failed HTTP {response_stream.status_code}"; logger.error(err_msg); yield f"event: error\ndata: {json.dumps({'error': err_msg})}\n\n"; return
                    existing_agent = await storage_inst.get_agent_by_url(agent_url); agent_id_for_stream: str
                    if not existing_agent:
                        agent_obj_new = AgentRegistration(url=agent_url, name=name, description=f"Registered via custom SSE: {agent_url}", aira_capabilities=capabilities_list, mcp_sse_url=agent_url, status=AgentStatus.ONLINE, last_seen=time.time())
                        await storage_inst.save_agent(agent_obj_new); agent_id_for_stream = agent_obj_new.agent_id; logger.info(f"Custom Connect: Registered new agent via SSE: {agent_obj_new.name} ({agent_id_for_stream})")
                    else:
                        agent_id_for_stream = existing_agent.agent_id; existing_agent.status = AgentStatus.ONLINE; existing_agent.last_seen = time.time()
                        if not existing_agent.mcp_sse_url: existing_agent.mcp_sse_url = agent_url
                        await storage_inst.save_agent(existing_agent); logger.info(f"Custom Connect: Updated agent status via SSE: {existing_agent.name} ({agent_id_for_stream})")
                    async for line_text in response_stream.aiter_lines():
                        yield line_text + "\n";
                        if not line_text.strip(): yield "\n" # SSE event end
                        if line_text.startswith("event: endpoint"):
                            try:
                                data_line_text = await response_stream.aiter_lines().__anext__(); yield data_line_text + "\n\n"
                                if data_line_text.startswith("data:"):
                                    endpoint_data_str = data_line_text[len("data:"):].strip()
                                    mcp_endpoint_url = json.loads(endpoint_data_str)
                                    if isinstance(mcp_endpoint_url, str):
                                        agent_to_update_mcp = await storage_inst.get_agent(agent_id_for_stream)
                                        if agent_to_update_mcp: agent_to_update_mcp.mcp_url = urllib.parse.urljoin(agent_url, mcp_endpoint_url); await storage_inst.save_agent(agent_to_update_mcp); logger.info(f"Custom Connect: Updated agent {agent_to_update_mcp.name} MCP URL via SSE: {agent_to_update_mcp.mcp_url}")
                            except StopAsyncIteration: logger.warning(f"Custom Connect: Stream {agent_url} ended after 'event: endpoint'."); break
                            except Exception as e_update: logger.error(f"Custom Connect: Error updating agent MCP URL for {agent_id_for_stream}: {e_update}", exc_info=True)
                    logger.info(f"Custom Connect: Agent stream ended for {agent_url}")
            except httpx.RequestError as e_req_custom: yield f"event: error\ndata: {json.dumps({'error': f'Network error: {str(e_req_custom)}'})}\n\n"
            except Exception as e_custom_outer: yield f"event: error\ndata: {json.dumps({'error': f'Proxying error: {str(e_custom_outer)}'})}\n\n"
            finally: logger.info(f"Custom Connect: Event generator for {agent_url} finished.")
    sse_resp_headers = {"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}
    return StreamingResponse(event_generator_custom(), headers=sse_resp_headers)

@app.post("/connect/stream/init", tags=["Custom Agent Connect (Legacy)"])
async def connect_stream_init_custom_endpoint(agent_payload: AgentRegistration, request_obj: Request, background_tasks: BackgroundTasks, storage_inst: MongoDBStorage = Depends(get_storage_dependency)):
    # ... (Original /connect/stream/init logic - kept for compatibility)
    logger.warning("/connect/stream/init is custom. Prefer /register.")
    existing_agent = await storage_inst.get_agent_by_url(agent_payload.url); agent_to_save = agent_payload; status_msg_key = "initialized_new"
    if existing_agent: agent_to_save.agent_id = existing_agent.agent_id; agent_to_save.created_at = existing_agent.created_at; status_msg_key = "initialized_updated"
    if agent_to_save.metrics is None: agent_to_save.metrics = AgentMetrics()
    agent_to_save.status = AgentStatus.ONLINE; agent_to_save.last_seen = time.time()
    if not await storage_inst.save_agent(agent_to_save): raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to save agent via custom init")
    cm = getattr(request_obj.app.state, 'connection_manager', None)
    if cm: background_tasks.add_task(cm.broadcast_event, "agent_registered" if status_msg_key == "initialized_new" else "agent_updated", {"agent_id": agent_to_save.agent_id, "name": agent_to_save.name, "url": agent_to_save.url, "status": agent_to_save.status.value})
    logger.info(f"Custom Init: Agent {status_msg_key} for: {agent_to_save.name} ({agent_to_save.agent_id})")
    return JSONResponse(content={"status": status_msg_key, "agent_id": agent_to_save.agent_id})

# --- Debug Endpoint (unchanged) ---
@app.post("/debug/register-test-agent", tags=["Debug"], include_in_schema=os.environ.get("DEBUG", "false").lower() == "true")
async def debug_register_test_agent(request: Request, background_tasks: BackgroundTasks, storage_inst: MongoDBStorage = Depends(get_storage_dependency)):
    # ... (Original debug agent registration logic)
    if os.environ.get("DEBUG", "false").lower() != "true": raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Debug endpoint not available.")
    agent_id_fixed, agent_url_base = "test-weather-agent-fixed-001", f"http://fake-weather-service.example.com/fixed-test"
    agent_url, mcp_endpoint = f"{agent_url_base}/{agent_id_fixed}", f"{agent_url}/mcp/stream"
    logger.info(f"DEBUG: Register/update test agent: {agent_id_fixed}")
    test_agent_payload = AgentRegistration(
        agent_id=agent_id_fixed, url=agent_url, name="Fixed Test Weather Service", description="Provides weather forecasts (Simulated for Debug).", version="1.2.1", # Incremented version
        mcp_tools=[MCPTool(name="get_forecast", description="Get weather forecast.", inputSchema={"type": "object", "properties": {"latitude": {"type": "number"}, "longitude": {"type": "number"}}, "required": ["latitude", "longitude"]}),
                   MCPTool(name="get_alerts", description="Get weather alerts.", inputSchema={"type": "object", "properties": {"state": {"type": "string"}}, "required": ["state"]})],
        aira_capabilities=["mcp"], status=AgentStatus.ONLINE, tags=["weather", "forecast", "debug_test"], category="Utilities-Debug",
        provider={"name": "AIRA Hub Debug Suite"}, mcp_stream_url=mcp_endpoint, mcp_url=mcp_endpoint
    )
    try:
        existing = await storage_inst.get_agent(agent_id_fixed) or await storage_inst.get_agent_by_url(test_agent_payload.url)
        agent_to_save = test_agent_payload
        if existing: agent_to_save.agent_id = existing.agent_id; agent_to_save.created_at = existing.created_at; agent_to_save.metrics = existing.metrics
        agent_to_save.status = AgentStatus.ONLINE; agent_to_save.last_seen = time.time()
        if not await storage_inst.save_agent(agent_to_save): raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to save test agent.")
        cm = getattr(request.app.state, 'connection_manager', None)
        if cm: background_tasks.add_task(cm.broadcast_event, "agent_updated" if existing else "agent_registered", {"agent_id": agent_to_save.agent_id, "name": agent_to_save.name, "url": agent_to_save.url, "status": agent_to_save.status.value})
        logger.info(f"DEBUG: Test agent '{agent_to_save.name}' (ID: {agent_to_save.agent_id}) processed.")
        return {"status": "ok", "operation": "updated" if existing else "created", "agent_id": agent_to_save.agent_id}
    except Exception as e: logger.error(f"DEBUG: Error with test agent {agent_id_fixed}: {e}", exc_info=True); raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

# ----- Main Entry Point -----
if __name__ == "__main__":
    import uvicorn
    from fastapi.routing import APIRoute
    port = int(os.environ.get("PORT", 8017))
    host = os.environ.get("HOST", "0.0.0.0")
    debug_mode = os.environ.get("DEBUG", "false").lower() == "true"
    for route in app.routes:
        if isinstance(route, APIRoute): route.operation_id = route.name
    logger.info(f"Starting AIRA Hub on {host}:{port} (Debug Mode: {debug_mode})")
    uvicorn.run("__main__:app", host=host, port=port, reload=debug_mode, log_level="debug" if debug_mode else "info")