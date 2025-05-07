# --- START OF FILE aira_hub.py ---

"""
AIRA Hub: A Decentralized Registry for MCP and A2A Tools

This implementation provides:
- Peer-to-peer agent discovery and registration
- MCP tools proxy and aggregation (via Streamable HTTP) # Updated
- MongoDB integration for persistent storage
- Support for both MCP and A2A protocols
"""

import asyncio
import json
import logging
import os
import time
import urllib
import urllib.parse  # For urljoin
import uuid
from datetime import datetime, timezone  # For timezone-aware datetimes
from typing import Dict, List, Optional, Any, Union, Callable, Awaitable, AsyncGenerator, Literal
from enum import Enum
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException, Depends
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator, validator, ConfigDict, ValidationError

# Removed EventSourceResponse as we are moving away from simple SSE
# from sse_starlette.sse import EventSourceResponse
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
# (Keep existing logging config)
logging.basicConfig(
    level=logging.DEBUG, # Set to DEBUG for more detailed logs during troubleshooting
    format='%(asctime)s - %(name)s - %(levelname)s - [%(funcName)s] %(message)s', # Added funcName
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("aira_hub.log")
    ]
)
logger = logging.getLogger("aira_hub")


# Constants
DEFAULT_HEARTBEAT_TIMEOUT = 300
DEFAULT_HEARTBEAT_INTERVAL = 60
DEFAULT_CLEANUP_INTERVAL = 120
MAX_TOOLS_PER_AGENT = 100

# MongoDB configuration
MONGODB_URL = os.getenv("MONGODB_URL")
if not MONGODB_URL:
    logger.critical("MONGODB_URL environment variable is not set. AIRA Hub cannot start.")
    exit(1)

# Globals - Initialized in lifespan
mongo_client: Optional[AsyncIOMotorClient] = None
db = None # Will be AsyncIOMotorDatabase


# --- Enums and Pydantic Models ---
# (Keep AgentStatus, ResourceType, MCPTool, A2ASkill, AgentMetrics,
#  AgentRegistration, DiscoverQuery, MCPRequestModel, MCPResponseModel,
#  A2ATaskState, A2ATaskStatusUpdate, A2ATask - no changes needed here)
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
    name: str
    description: Optional[str] = None
    inputSchema: Dict[str, Any]
    annotations: Optional[Dict[str, Any]] = None

class A2ASkill(BaseModel):
    id: str
    name: str
    description: str
    version: str = "1.0.0"
    tags: List[str] = []
    parameters: Dict[str, Any] = {}
    examples: List[str] = []

class AgentMetrics(BaseModel):
    request_count: int = 0
    success_count: int = 0
    error_count: int = 0
    avg_response_time: float = 0.0
    uptime: float = 0.0
    last_updated: float = Field(default_factory=time.time)

class AgentRegistration(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": { # Example updated to include mcp_stream_url
                "url": "https://weather-agent.example.com",
                "name": "Weather Agent",
                "description": "Provides weather forecasts",
                "mcp_tools": [{"name": "get_weather", "description": "Get current weather", "inputSchema": {"type": "object", "properties": {"location": {"type": "string"}}, "required": ["location"]}}],
                "aira_capabilities": ["mcp", "a2a"],
                "tags": ["weather"], "category": "utilities",
                "mcp_stream_url": "https://weather-agent.example.com/mcp" # Agent's streamable endpoint
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
    mcp_url: Optional[str] = None # Kept for potential non-streaming agents? Or remove? Let's keep for now.
    mcp_sse_url: Optional[str] = None # Kept for potential non-streaming agents? Or remove? Let's keep for now.
    mcp_stream_url: Optional[str] = None # Preferred URL for agents supporting Streamable HTTP
    agent_id: str = Field(default_factory=lambda: str(uuid.uuid4()))

    @field_validator('url')
    @classmethod
    def url_must_be_valid(cls, v: str) -> str:  # Added type hints
        if not v.startswith(('http://', 'https://')):
            raise ValueError('URL must start with http:// or https://')
        parsed = urllib.parse.urlparse(v)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError('URL must be a valid absolute URL')
        return v

    # Use field_validator for Pydantic v2
    @field_validator('mcp_tools')
    @classmethod
    def max_tools_check(cls, v: List[MCPTool]) -> List[MCPTool]:  # Added type hints
        if len(v) > MAX_TOOLS_PER_AGENT:
            raise ValueError(f'Maximum of {MAX_TOOLS_PER_AGENT} tools allowed')
        return v


    # For simple URL format checks, apply individually.
    @field_validator('mcp_url', 'mcp_sse_url', 'mcp_stream_url',
                     mode='before')  # mode='before' allows checking raw input
    @classmethod
    def validate_mcp_url_format(cls, v: Optional[str]) -> Optional[str]:  # Added type hints
        if v:  # Only validate if a value is provided
            if not v.startswith(('http://', 'https://')):
                # Allow relative paths starting with '/' ? Let's be stricter for now.
                # if not v.startswith('/'):
                raise ValueError('MCP URL must start with http:// or https://')
            # Simple check, resolution against base 'url' would need model_validator
        return v

class DiscoverQuery(BaseModel):
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
    jsonrpc: Literal["2.0"] = "2.0"
    id: Optional[Union[str, int]] = None
    method: str
    params: Optional[Union[Dict[str, Any], List[Any]]] = None

class MCPResponseModel(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: Optional[Union[str, int]] = None
    result: Optional[Any] = None
    error: Optional[Dict[str, Any]] = None

class A2ATaskState(str, Enum):
    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

class A2ATaskStatusUpdate(BaseModel):
    state: A2ATaskState
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    message: Optional[str] = None

class A2ATask(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent_id: str; skill_id: str; session_id: Optional[str] = None
    original_message: Dict[str, Any]
    current_status: A2ATaskStatusUpdate
    history: List[Dict[str, Any]] = []; artifacts: List[Dict[str, Any]] = []
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    model_config = ConfigDict(use_enum_values=True)


# --- MongoDBStorage ---
# (Keep existing MongoDBStorage class - no changes needed for transport)
class MongoDBStorage:
    def __init__(self, connection_string: str):
        self.connection_string = connection_string
        self.client: Optional[AsyncIOMotorClient] = None
        self.db: Optional[AsyncIOMotorClient] = None
        self.tool_cache: Dict[str, str] = {}

    async def init(self):
        if not self.connection_string:
            logger.error("MONGODB_URL is not set. Persistence is disabled.")
            raise ValueError("MONGODB_URL environment variable not set")
        try:
            logger.info(f"Attempting to connect to MongoDB...")
            self.mongo_db_client = AsyncIOMotorClient(self.connection_string, serverSelectionTimeoutMS=5000)
            await self.mongo_db_client.admin.command('ping')
            logger.info("MongoDB ping successful.")
            self.db = self.mongo_db_client.aira_hub # type: ignore

            if self.db is None: raise ConnectionError("Failed to get database object 'aira_hub'")

            index_futures = [ # ... (keep index creation as before) ...
                 self.db.agents.create_index("url", unique=True),
                self.db.agents.create_index("agent_id", unique=True),
                self.db.agents.create_index("last_seen"),
                self.db.agents.create_index("status"),
                self.db.agents.create_index("tags"),
                self.db.agents.create_index("aira_capabilities"),
                self.db.agents.create_index("category"),
                self.db.agents.create_index("mcp_tools.name"),
                # Indexes for A2A tasks
                self.db.tasks.create_index("agent_id"),
                self.db.tasks.create_index("skill_id"),
                self.db.tasks.create_index("current_status.state"),
                self.db.tasks.create_index("created_at"),
            ]
            await asyncio.gather(*index_futures)
            logger.info(f"Indexes ensured for MongoDB database: aira_hub")
            await self.refresh_tool_cache()
            logger.info(f"Connected to MongoDB database: aira_hub")
        except Exception as e:
            logger.error(f"Error connecting/initializing MongoDB: {str(e)}", exc_info=True)
            self.db = None; self.mongo_db_client = None; raise e

    async def close(self):
        if self.mongo_db_client:
            self.mongo_db_client.close()
            logger.info("MongoDB connection closed")
            self.mongo_db_client = None; self.db = None

    async def refresh_tool_cache(self): # (Keep implementation)
        self.tool_cache = {}
        if self.db is None: logger.warning("DB not init, cannot refresh tool cache."); return
        try:
            cursor = self.db.agents.find({"status": AgentStatus.ONLINE.value, "mcp_tools": {"$exists": True, "$ne": []}}, {"agent_id": 1, "mcp_tools.name": 1})
            async for agent_doc in cursor:
                agent_id_str = str(agent_doc.get("agent_id"));
                if not agent_id_str: continue
                for tool in agent_doc.get("mcp_tools", []):
                    tool_name = tool.get("name");
                    if tool_name: self.tool_cache[tool_name] = agent_id_str
            logger.info(f"Tool cache refreshed: {len(self.tool_cache)} tools")
        except Exception as e: logger.error(f"Error refreshing tool cache: {e}", exc_info=True)

    async def save_agent(self, agent: AgentRegistration) -> bool: # (Keep implementation)
        if self.db is None: logger.error("DB not init, cannot save agent."); return False
        agent_dict = agent.model_dump(exclude_unset=True); agent_id_str = str(agent.agent_id)
        tools_to_remove = [k for k, v in self.tool_cache.items() if v == agent_id_str];
        for tool_name in tools_to_remove:
             if tool_name in self.tool_cache: del self.tool_cache[tool_name]
        if agent.status == AgentStatus.ONLINE:
            for tool in agent.mcp_tools:
                if hasattr(tool, 'name') and tool.name: self.tool_cache[tool.name] = agent_id_str
        try:
            result = await self.db.agents.update_one({"agent_id": agent_id_str}, {"$set": agent_dict}, upsert=True)
            if result.upserted_id: logger.info(f"New agent created: {agent.name} ({agent_id_str})")
            elif result.modified_count > 0: logger.info(f"Agent updated: {agent.name} ({agent_id_str})")
            # else: logger.debug(f"Agent save no changes: {agent.name} ({agent_id_str})")
            return True
        except Exception as e: logger.error(f"Error saving agent {agent.name}: {e}", exc_info=True); return False

    async def get_agent(self, agent_id: str) -> Optional[AgentRegistration]: # (Keep implementation)
        if self.db is None: return None
        try:
            agent_dict = await self.db.agents.find_one({"agent_id": agent_id})
            if agent_dict: agent_dict.pop("_id", None); return AgentRegistration(**agent_dict)
            return None
        except Exception as e: logger.error(f"Error getting agent {agent_id}: {e}", exc_info=True); return None

    async def get_agent_by_url(self, url: str) -> Optional[AgentRegistration]: # (Keep implementation)
        if self.db is None: return None
        try:
            agent_dict = await self.db.agents.find_one({"url": url})
            if agent_dict: agent_dict.pop("_id", None); return AgentRegistration(**agent_dict)
            return None
        except Exception as e: logger.error(f"Error getting agent by URL {url}: {e}", exc_info=True); return None

    async def get_agent_by_tool(self, tool_name: str) -> Optional[AgentRegistration]: # (Keep implementation)
        if self.db is None: return None
        try:
            agent_id_from_cache = self.tool_cache.get(tool_name)
            if agent_id_from_cache:
                agent = await self.get_agent(agent_id_from_cache)
                if agent and agent.status == AgentStatus.ONLINE: return agent
                else:
                    if tool_name in self.tool_cache: del self.tool_cache[tool_name]; logger.debug(f"Removed stale cache: {tool_name}")
            agent_dict = await self.db.agents.find_one({"mcp_tools.name": tool_name, "status": AgentStatus.ONLINE.value})
            if agent_dict:
                agent_dict.pop("_id", None); agent_id_str = str(agent_dict["agent_id"])
                self.tool_cache[tool_name] = agent_id_str; return AgentRegistration(**agent_dict)
            return None
        except Exception as e: logger.error(f"Error getting agent by tool {tool_name}: {e}", exc_info=True); return None

    async def list_agents(self) -> List[AgentRegistration]: # (Keep implementation)
        agents = [];
        if self.db is None: return []
        try:
            cursor = self.db.agents.find()
            async for agent_dict in cursor:
                agent_dict.pop("_id", None)
                try: agents.append(AgentRegistration(**agent_dict))
                except Exception as p_err: logger.warning(f"Skip agent validation err: {agent_dict.get('agent_id')}. Err: {p_err}")
            return agents
        except Exception as e: logger.error(f"Error listing agents: {e}", exc_info=True); return []

    async def list_tools(self) -> List[Dict[str, Any]]: # (Keep implementation)
        tools = [];
        if self.db is None: return []
        try:
            cursor = self.db.agents.find({"status": AgentStatus.ONLINE.value, "mcp_tools": {"$exists": True, "$ne": []}}, {"mcp_tools": 1, "name": 1, "agent_id": 1})
            async for agent_doc in cursor:
                agent_name = agent_doc.get("name", "Unknown"); agent_id_str = str(agent_doc.get("agent_id", ""))
                for tool_data in agent_doc.get("mcp_tools", []):
                    tool_dict = {"name": tool_data.get("name"), "description": tool_data.get("description"), "agent": agent_name, "agent_id": agent_id_str, "inputSchema": tool_data.get("inputSchema", {}), "annotations": tool_data.get("annotations")}
                    if tool_dict["name"] and tool_dict["inputSchema"] is not None: tools.append(tool_dict)
                    else: logger.warning(f"Skip malformed tool from agent {agent_id_str}: {tool_data}")
            return tools
        except Exception as e: logger.error(f"Error listing tools: {e}", exc_info=True); return []

    async def delete_agent(self, agent_id: str) -> bool: # (Keep implementation)
        if self.db is None: return False
        try:
            agent = await self.get_agent(agent_id)
            if agent:
                agent_id_str = str(agent.agent_id)
                tools_to_remove = [k for k, v in self.tool_cache.items() if v == agent_id_str]
                for tool_name in tools_to_remove:
                     if tool_name in self.tool_cache: del self.tool_cache[tool_name]
                result = await self.db.agents.delete_one({"agent_id": agent_id})
                if result.deleted_count > 0: logger.info(f"Agent deleted: {agent.name} ({agent_id})"); return True
            return False
        except Exception as e: logger.error(f"Error deleting agent {agent_id}: {e}", exc_info=True); return False

    async def update_agent_heartbeat(self, agent_id: str, timestamp: float) -> bool: # (Keep implementation)
        if self.db is None: return False
        try:
            result = await self.db.agents.update_one({"agent_id": agent_id}, {"$set": {"last_seen": timestamp}})
            return result.modified_count > 0
        except Exception as e: logger.error(f"Error updating heartbeat {agent_id}: {e}", exc_info=True); return False

    async def count_agents(self, query: Dict[str, Any] = None) -> int: # (Keep implementation)
        if self.db is None: return 0
        try: return await self.db.agents.count_documents(query or {})
        except Exception as e: logger.error(f"Error counting agents: {e}", exc_info=True); return 0

    async def search_agents(self, query: Dict[str, Any] = None, skip: int = 0, limit: int = 100) -> List[AgentRegistration]: # (Keep implementation)
        agents = [];
        if self.db is None: return []
        try:
            cursor = self.db.agents.find(query or {}).skip(skip).limit(limit)
            async for agent_dict in cursor:
                agent_dict.pop("_id", None)
                try: agents.append(AgentRegistration(**agent_dict))
                except Exception as p_err: logger.warning(f"Skip agent search validation err: {agent_dict.get('agent_id')}. Err: {p_err}")
            return agents
        except Exception as e: logger.error(f"Error searching agents: {e}", exc_info=True); return []

    async def save_a2a_task(self, task: A2ATask) -> bool: # (Keep implementation)
        if self.db is None: logger.error("DB not init, cannot save A2A task."); return False
        try:
            task_dict = task.model_dump(exclude_unset=True); task_dict["updated_at"] = datetime.now(timezone.utc)
            await self.db.tasks.update_one({"id": task.id}, {"$set": task_dict}, upsert=True)
            logger.info(f"A2A Task {task.id} saved/updated.")
            return True
        except Exception as e: logger.error(f"Error saving A2A task {task.id}: {e}", exc_info=True); return False

    async def get_a2a_task(self, task_id: str) -> Optional[A2ATask]: # (Keep implementation)
        if self.db is None: return None
        try:
            task_dict = await self.db.tasks.find_one({"id": task_id})
            if task_dict: task_dict.pop("_id", None); return A2ATask(**task_dict)
            return None
        except Exception as e: logger.error(f"Error retrieving A2A task {task_id}: {e}", exc_info=True); return None


# --- MCP Session Manager ---
# (Keep existing MCPSession class - it's largely unused for Streamable HTTP anyway)
class MCPSession:
    def __init__(self, storage_param: Optional[MongoDBStorage]): # Accepts Optional storage
        self.active_sessions: Dict[str, Dict[str, Any]] = {}
        self.client_connections: Dict[str, List[str]] = {}
        self.storage = storage_param

    # ... (rest of MCPSession methods remain the same) ...
    def create_session(self, client_id: str) -> str: # (Keep implementation)
        session_id = str(uuid.uuid4()); self.active_sessions[session_id] = {"client_id": client_id, "created_at": time.time(), "last_activity": time.time(), "state": {}}; self.client_connections.setdefault(client_id, []).append(session_id); logger.debug(f"Created MCP session {session_id} for client {client_id}"); return session_id
    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]: # (Keep implementation)
        session = self.active_sessions.get(session_id);
        if session: session["last_activity"] = time.time();
        return session
    def update_session_activity(self, session_id: str): # (Keep implementation)
        if session_id in self.active_sessions: self.active_sessions[session_id]["last_activity"] = time.time(); logger.debug(f"Updated session {session_id} activity")
    def close_session(self, session_id: str): # (Keep implementation)
        if session_id in self.active_sessions:
            session = self.active_sessions.pop(session_id); client_id = session.get("client_id");
            if client_id in self.client_connections:
                if session_id in self.client_connections[client_id]: self.client_connections[client_id].remove(session_id);
                if not self.client_connections[client_id]: del self.client_connections[client_id];
            logger.info(f"Closed MCP session {session_id}")
    def get_client_sessions(self, client_id: str) -> List[str]: # (Keep implementation)
        return self.client_connections.get(client_id, [])
    def cleanup_stale_sessions(self, max_age: int = 3600) -> int: # (Keep implementation)
        now = time.time(); to_remove = [sid for sid, s in self.active_sessions.items() if now - s.get("last_activity", 0) > max_age]; count = 0;
        for session_id in to_remove: self.close_session(session_id); count += 1;
        if count > 0: logger.info(f"Cleaned up {count} stale MCP sessions.");
        return count


# --- ConnectionManager (for UI) ---
# (Keep existing ConnectionManager class - no changes needed for transport)
class ConnectionManager:
    def __init__(self): self.active_connections: Dict[str, Dict[str, Any]] = {}
    def register_connection(self, client_id: str, send_func: Callable[[str, Any], Awaitable[None]]): self.active_connections[client_id] = {"connected_at": time.time(), "last_activity": time.time(), "send_func": send_func}; logger.info(f"Registered client UI connection: {client_id}")
    def unregister_connection(self, client_id: str):
        if client_id in self.active_connections: del self.active_connections[client_id]; logger.info(f"Unregistered client UI connection: {client_id}")
    async def send_event(self, client_id: str, event: str, data: Any):
        conn_details = self.active_connections.get(client_id);
        if conn_details:
            conn_details["last_activity"] = time.time();
            try: await conn_details["send_func"](event, json.dumps(data)); return True
            except Exception as e: logger.error(f"Error sending UI event '{event}' to {client_id}: {e}", exc_info=True); self.unregister_connection(client_id)
        return False
    async def broadcast_event(self, event: str, data: Any): await asyncio.gather(*[self.send_event(cid, event, data) for cid in list(self.active_connections.keys())])
    def get_all_connections(self) -> List[str]: return list(self.active_connections.keys())


# --- Application Setup ---
storage: Optional[MongoDBStorage] = None

@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    global storage, mongo_client, db
    logger.info("AIRA Hub lifespan startup...")
    temp_storage = MongoDBStorage(MONGODB_URL)
    app_instance.state.storage_failed = False

    try:
        await temp_storage.init()
        storage = temp_storage
        mongo_client = storage.mongo_db_client # type: ignore
        db = storage.db # type: ignore

        app_instance.state.storage = storage
        app_instance.state.mcp_session_manager = MCPSession(storage)
        app_instance.state.connection_manager = ConnectionManager()
        app_instance.state.start_time = time.time()
        app_instance.state.active_mcp_streams = {} # Track active Streamable HTTP handlers

        app_instance.state.cleanup_task = asyncio.create_task(cleanup_inactive_agents_task(app_instance))
        app_instance.state.heartbeat_task = asyncio.create_task(send_ui_heartbeats_task(app_instance))
        logger.info("AIRA Hub background tasks started.")
        logger.info("AIRA Hub started successfully.")
        yield # App runs
    except Exception as e:
        logger.error(f"AIRA Hub startup failed: {e}", exc_info=True)
        app_instance.state.storage = None; app_instance.state.storage_failed = True
        storage = mongo_client = db = None
        logger.warning("AIRA Hub starting in degraded mode (DB unavailable).")
        yield # Allow FastAPI to manage state even if degraded
        if temp_storage and temp_storage.mongo_db_client: await temp_storage.close()
        logger.info("AIRA Hub stopped after startup failure.")
        return

    # --- Shutdown ---
    logger.info("Shutting down AIRA Hub...")
    # Cancel background tasks (keep existing logic)
    tasks_to_cancel = [t for t in [getattr(app_instance.state, n, None) for n in ['cleanup_task', 'heartbeat_task']] if t and not t.done()]
    for task in tasks_to_cancel: task.cancel()
    if tasks_to_cancel:
        results = await asyncio.gather(*tasks_to_cancel, return_exceptions=True);
        # Log errors during cancellation (keep existing logic)
        logger.info("Background tasks cancelled.")

    # Close active MCP streams (keep existing logic)
    if hasattr(app_instance.state, 'active_mcp_streams'):
        active_streams = app_instance.state.active_mcp_streams; logger.info(f"Closing {len(active_streams)} active MCP streams...")
        for task in list(active_streams.values()):
             if task and not task.done(): task.cancel()
        await asyncio.gather(*[t for t in active_streams.values() if t], return_exceptions=True);
        active_streams.clear(); logger.info("Active MCP streams closed.")

    # Close DB connection (keep existing logic)
    if hasattr(app_instance.state, 'storage') and app_instance.state.storage:
        logger.info("Closing database connection..."); await app_instance.state.storage.close();
        storage = mongo_client = db = None
    elif getattr(app_instance.state, 'storage_failed', False): logger.info("DB connection failed at startup, skipping close.")
    else: logger.warning("Storage state unclear during shutdown.")
    logger.info("AIRA Hub stopped successfully.")


# --- Background Tasks ---
# (Keep cleanup_inactive_agents_task and send_ui_heartbeats_task implementations)
async def cleanup_inactive_agents_task(app_instance: FastAPI): # (Keep implementation)
    await asyncio.sleep(10);
    while True:
        try:
            await asyncio.sleep(DEFAULT_CLEANUP_INTERVAL); now = time.time()
            if getattr(app_instance.state, 'storage_failed', False) or not hasattr(app_instance.state, 'storage') or app_instance.state.storage is None or app_instance.state.storage.db is None: logger.warning("Cleanup: Storage unavailable."); continue
            storage_inst = app_instance.state.storage; agents = await storage_inst.list_agents(); inactive_count = 0; offline_agents_ids = []
            for agent_obj in agents:
                if agent_obj.status != AgentStatus.OFFLINE and (now - agent_obj.last_seen) > DEFAULT_HEARTBEAT_TIMEOUT:
                    agent_obj.status = AgentStatus.OFFLINE; logger.info(f"Marking agent {agent_obj.agent_id} offline.");
                    try:
                        if await storage_inst.save_agent(agent_obj): inactive_count += 1; offline_agents_ids.append(agent_obj.agent_id)
                        else: logger.error(f"Failed save offline status agent {agent_obj.agent_id}")
                    except Exception as save_e: logger.error(f"Error save offline status agent {agent_obj.agent_id}: {save_e}", exc_info=True)
            session_count = 0;
            if hasattr(app_instance.state, 'mcp_session_manager'): session_count = app_instance.state.mcp_session_manager.cleanup_stale_sessions()
            if inactive_count > 0 or session_count > 0: logger.info(f"Cleanup: Marked {inactive_count} agents offline, cleaned {session_count} sessions.")
            if offline_agents_ids and hasattr(app_instance.state, 'connection_manager'): await asyncio.gather(*[app_instance.state.connection_manager.broadcast_event("agent_status", {"agent_id": aid, "status": AgentStatus.OFFLINE.value}) for aid in offline_agents_ids], return_exceptions=True)
        except asyncio.CancelledError: logger.info("Cleanup task cancelled."); break
        except Exception as e: logger.error(f"Error in cleanup task: {e}", exc_info=True); await asyncio.sleep(DEFAULT_CLEANUP_INTERVAL)

async def send_ui_heartbeats_task(app_instance: FastAPI): # (Keep implementation)
    while True:
        try:
            await asyncio.sleep(DEFAULT_HEARTBEAT_INTERVAL);
            if hasattr(app_instance.state, 'connection_manager'):
                cm = app_instance.state.connection_manager;
                if cm.get_all_connections(): await cm.broadcast_event("heartbeat", {"timestamp": time.time()})
        except asyncio.CancelledError: logger.info("UI Heartbeat task cancelled."); break
        except Exception as e: logger.error(f"Error in UI heartbeat task: {e}", exc_info=True); await asyncio.sleep(DEFAULT_HEARTBEAT_INTERVAL)


# --- FastAPI App and Middleware ---
app = FastAPI(
    title="AIRA Hub", description="Decentralized Registry & MCP/A2A Hub",
    version="1.0.0", lifespan=lifespan
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS", "DELETE"], allow_headers=["*"],
)

@app.middleware("http")
async def log_requests_middleware(request: Request, call_next): # Renamed from log_requests_middleware_simplified
    start_time = time.time(); path = request.url.path; method = request.method
    client_host = request.client.host if request.client else "Unknown"
    logger.info(f"Request START: {method} {path} from {client_host}")
    try:
        response = await call_next(request)
        duration = time.time() - start_time; status_code = response.status_code
        log_level = logging.INFO if status_code < 400 else logging.WARNING if status_code < 500 else logging.ERROR
        logger.log(log_level, f"Request END: {method} {path} - Status {status_code} ({duration:.4f}s)")
        return response
    except Exception as e:
        duration = time.time() - start_time
        logger.error(f"Request FAILED during processing: {method} {path} after {duration:.4f}s. Error: {e}", exc_info=True)
        raise # Re-raise to let FastAPI handle it

# --- Storage Dependency ---
def get_storage_dependency(request: Request) -> MongoDBStorage: # (Keep implementation)
    logger.debug(f"Executing get_storage_dependency for {request.method} {request.url.path}")
    if getattr(request.app.state, 'storage_failed', False):
        logger.error("Storage dependency: Storage initialization failed."); raise HTTPException(status_code=503, detail="DB unavailable (init failed)")
    storage_inst = getattr(request.app.state, 'storage', None)
    if not storage_inst or storage_inst.db is None:
        logger.error("Storage dependency: Storage not found or DB object is None."); raise HTTPException(status_code=503, detail="DB unavailable")
    logger.debug("Storage dependency check successful.")
    return storage_inst


# --- API Endpoints ---
# (Keep /register, /heartbeat, /agents, /agents/{id}, DELETE /agents/{id},
#  /tools, /tags, /categories, /status, /health, A2A, Admin, UI, Custom Connect endpoints
#  Ensure they use Depends(get_storage_dependency) if they need storage)
# Example:
@app.post("/register", status_code=201, tags=["Agents"])
async def register_agent_endpoint(request_obj: Request, agent_payload: AgentRegistration, background_tasks: BackgroundTasks, storage_inst: MongoDBStorage = Depends(get_storage_dependency)):
    # (Keep implementation)
    existing_agent = await storage_inst.get_agent_by_url(agent_payload.url); agent_to_save = agent_payload; status_message = "registered"
    if existing_agent:
        agent_to_save.agent_id = existing_agent.agent_id; agent_to_save.created_at = existing_agent.created_at;
        if existing_agent.metrics and agent_to_save.metrics is None: agent_to_save.metrics = existing_agent.metrics;
        status_message = "updated"; logger.info(f"Agent {agent_to_save.name} update: {agent_to_save.agent_id}")
    else: logger.info(f"New agent registration: {agent_to_save.url}");
    if agent_to_save.metrics is None: agent_to_save.metrics = AgentMetrics()
    agent_to_save.status = AgentStatus.ONLINE; agent_to_save.last_seen = time.time()
    if not await storage_inst.save_agent(agent_to_save): logger.error(f"Failed save agent {agent_to_save.name}"); raise HTTPException(status_code=500, detail="Failed save agent")
    if hasattr(request_obj.app.state, 'connection_manager'): background_tasks.add_task(request_obj.app.state.connection_manager.broadcast_event, "agent_registered" if status_message == "registered" else "agent_updated", {"agent_id": agent_to_save.agent_id, "name": agent_to_save.name, "url": agent_to_save.url, "status": agent_to_save.status.value})
    logger.info(f"Agent {status_message} done: {agent_to_save.name} ({agent_to_save.agent_id})"); return {"status": status_message, "agent_id": agent_to_save.agent_id, "url": agent_to_save.url}

# --- MCP Utilities (Shared) ---
# (Keep mcp_handle_initialize, mcp_handle_tools_list, mcp_handle_tools_call implementations
#  Ensure they return MCPResponseModel, not JSONResponse)
async def mcp_handle_initialize(request_id: Union[str, int], params: Dict[str, Any]) -> MCPResponseModel: # Returns Model
    client_info = params.get("clientInfo", {});
    client_protocol = params.get("protocolVersion", "Unknown") # Get client's requested version
    logger.info(f"MCP Initialize from {client_info.get('name', 'UnknownClient')} (ID: {request_id}), Client Protocol: {client_protocol}")

    # Respond with the older protocol version for compatibility with mcp-remote/Claude
    supported_hub_protocol = "2024-11-05" # <--- CHANGE THIS LINE
    logger.warning(f"Responding with protocol version {supported_hub_protocol} for compatibility (client requested {client_protocol})")

    return MCPResponseModel(id=request_id, result={
        "protocolVersion": supported_hub_protocol, # Use the compatibility version
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "AIRA Hub", "version": "1.0.0"}
    })
async def mcp_handle_tools_list(storage_inst: MongoDBStorage, request_id: Union[str, int]) -> MCPResponseModel: # Returns Model
    logger.info(f"MCP tools/list (ID: {request_id})")
    try:
        tools_data = await storage_inst.list_tools(); mcp_tools_list = [{"name": t.get("name"), "description": t.get("description", ""), "inputSchema": t.get("inputSchema", {}), "annotations": t.get("annotations")} for t in tools_data if t.get("name") and t.get("inputSchema") is not None]
        return MCPResponseModel(id=request_id, result={"tools": mcp_tools_list})
    except Exception as e: logger.error(f"Error tools/list {request_id}: {e}", exc_info=True); return MCPResponseModel(id=request_id, error={"code": -32603, "message": "Internal error fetching tools"})

async def mcp_handle_tools_call(storage_inst: MongoDBStorage, request_id: Union[str, int], params: Dict[str, Any]) -> MCPResponseModel: # Returns Model
    # (Keep implementation, ensuring it returns MCPResponseModel)
    logger.info(f"MCP tools/call (ID: {request_id}) params: {params}"); tool_name = params.get("name"); arguments = params.get("arguments", {})
    if not tool_name or not isinstance(tool_name, str): return MCPResponseModel(id=request_id, error={"code": -32602, "message": "Invalid params: 'name' missing/invalid"})
    if not isinstance(arguments, dict): return MCPResponseModel(id=request_id, error={"code": -32602, "message": "Invalid params: 'arguments' must be object"})
    try:
        agent = await storage_inst.get_agent_by_tool(tool_name)
        if not agent: return MCPResponseModel(id=request_id, error={"code": -32602, "message": f"Tool not found/agent offline: {tool_name}"})
        # Determine which agent URL to use: prefer streamable, fallback to plain mcp_url
        target_url = agent.mcp_stream_url if agent.mcp_stream_url else agent.mcp_url
        if not target_url: logger.error(f"Agent {agent.name} tool '{tool_name}' no mcp_stream_url or mcp_url."); return MCPResponseModel(id=request_id, error={"code": -32000, "message": f"Agent config error: No MCP endpoint for tool '{tool_name}'"})
        mcp_request_to_agent = {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": tool_name, "arguments": arguments}}
        logger.info(f"Forwarding tools/call '{tool_name}' (ID: {request_id}) to agent: {target_url}")
        async with httpx.AsyncClient(timeout=60.0) as http_client:
            try:
                # Using POST even for streamable, as agent might handle simple POSTs or stream back
                response = await http_client.post(target_url, json=mcp_request_to_agent)
                response.raise_for_status(); agent_response_body = response.json()
                if not (isinstance(agent_response_body, dict) and agent_response_body.get("jsonrpc") == "2.0"): raise ValueError("Agent response not valid JSON-RPC 2.0")
                if agent_response_body.get("id") != request_id: logger.warning(f"Agent {agent.name} tools/call ID mismatch. Expected {request_id}, Got {agent_response_body.get('id')}. Correcting."); agent_response_body["id"] = request_id;
                return MCPResponseModel(**agent_response_body)
            except httpx.HTTPStatusError as http_err: # (Keep error handling)
                logger.error(f"Agent {agent.name} HTTP error {http_err.response.status_code} tools/call '{tool_name}'. Body: {http_err.response.text[:200]}...");
                try:
                    error_body = http_err.response.json();
                    if isinstance(error_body, dict) and error_body.get("jsonrpc") == "2.0" and "error" in error_body: error_body["id"] = request_id; return MCPResponseModel(**error_body)
                except Exception: pass;
                return MCPResponseModel(id=request_id, error={"code": -32002, "message": f"Agent HTTP error: {http_err.response.status_code}"})
            except (httpx.RequestError, json.JSONDecodeError, ValueError) as req_err: # (Keep error handling)
                 logger.error(f"Error calling/parsing agent {agent.name} ({target_url}) tools/call '{tool_name}': {req_err}"); return MCPResponseModel(id=request_id, error={"code": -32003, "message": f"Network/parse error contacting agent for tool '{tool_name}'"})
    except Exception as e: logger.error(f"Unexpected internal error mcp_handle_tools_call '{tool_name}' (ID: {request_id}): {e}", exc_info=True); return MCPResponseModel(id=request_id, error={"code": -32603, "message": "Internal server error during tool call"})

# --- (Keep imports and other class/function definitions as they were) ---

# ----- MCP Utilities (Shared by Transports) -----
# (Keep mcp_handle_initialize, mcp_handle_tools_list, mcp_handle_tools_call)
# IMPORTANT: Ensure these functions now RETURN the MCPResponseModel,
# they should NOT return JSONResponse directly anymore.

# ----- MCP Protocol Endpoint (Streamable HTTP - Preferred) -----
STREAM_SEPARATOR = b'\r\n'

async def process_single_mcp_message(
    msg_dict: Dict,
    stream_id: str, # For logging
    storage_inst: MongoDBStorage,
    send_queue: asyncio.Queue,
    pending_tool_calls: Dict # Keep track of background tasks
) -> Optional[MCPResponseModel]:
    """Processes a single parsed JSON-RPC message dictionary."""
    response_model: Optional[MCPResponseModel] = None
    mcp_req: Optional[MCPRequestModel] = None # Define here for except block

    try:
        if not isinstance(msg_dict, dict) or msg_dict.get("jsonrpc") != "2.0":
            logger.warning(f"MCP Stream {stream_id}: Invalid JSON-RPC msg: {msg_dict}")
            return MCPResponseModel(id=msg_dict.get("id"), error={"code": -32600, "message": "Invalid Request"})

        mcp_req = MCPRequestModel(**msg_dict)
        req_id = mcp_req.id
        method = mcp_req.method
        # Ensure params is a dict for handlers, default to empty if missing or not dict
        params = mcp_req.params if isinstance(mcp_req.params, dict) else {}
        is_notification = req_id is None

        logger.info(f"MCP Stream {stream_id}: RECV method='{method}' id='{req_id}'")

        if method == "initialize":
            if is_notification:
                response_model = MCPResponseModel(id=None, error={"code": -32600, "message": "'initialize' must have ID"})
            else:
                response_model = await mcp_handle_initialize(req_id, params) # type: ignore
        elif method == "tools/list":
            if is_notification:
                response_model = MCPResponseModel(id=None, error={"code": -32600, "message": "'tools/list' must have ID"})
            else:
                response_model = await mcp_handle_tools_list(storage_inst, req_id) # type: ignore
        elif method == "tools/call":
            if is_notification:
                response_model = MCPResponseModel(id=None, error={"code": -32600, "message": "'tools/call' must have ID"})
            else:
                # Start tool call as background task
                tool_call_task = asyncio.create_task(mcp_handle_tools_call(storage_inst, req_id, params)) # type: ignore
                pending_tool_calls[req_id] = tool_call_task

                # Define callback to put result onto send queue when done
                def _tool_call_done_cb(fut: asyncio.Task, q: asyncio.Queue, call_id, stream_id_cb, pending_map):
                    try:
                        result_model = fut.result()
                        q.put_nowait(result_model)
                    except Exception as e_cb:
                        logger.error(f"MCP Stream {stream_id_cb}: Tool call task {call_id} failed: {e_cb}", exc_info=True)
                        q.put_nowait(MCPResponseModel(id=call_id, error={"code": -32603, "message": "Tool call internal error"}))
                    finally:
                        if call_id in pending_map:
                            del pending_map[call_id]

                tool_call_task.add_done_callback(
                    lambda fut: _tool_call_done_cb(fut, send_queue, req_id, stream_id, pending_tool_calls)
                )
                response_model = None # Indicate response will be sent later by callback
        elif method == "notifications/initialized":
            logger.info(f"MCP Stream {stream_id}: RECV notifications/initialized (id: {req_id})")
            # No response needed for notifications
        elif method == "notifications/cancelled":
            cancel_params = params if isinstance(params, dict) else {}
            req_id_to_cancel = cancel_params.get("requestId")
            logger.info(f"MCP Stream {stream_id}: RECV notifications/cancelled for request ID: {req_id_to_cancel}")
            if req_id_to_cancel in pending_tool_calls:
                logger.info(f"MCP Stream {stream_id}: Cancelling tool call {req_id_to_cancel}")
                pending_tool_calls[req_id_to_cancel].cancel()
                # No response needed for cancellation notification itself
            else:
                logger.warning(f"MCP Stream {stream_id}: Cancel for unknown/completed request ID: {req_id_to_cancel}")
        # --- Handle other non-implemented methods ---
        elif method in ["resources/list", "prompts/list"]:
            if is_notification:
                 response_model = MCPResponseModel(id=None, error={"code": -32600, "message": f"'{method}' must have ID"})
            else:
                key_name = method.split('/')[0]
                response_model = MCPResponseModel(id=req_id, result={key_name: []}) # Empty list
                logger.info(f"MCP Stream {stream_id}: {method} (id: {req_id}) - Not Implemented")
        else:
            logger.warning(f"MCP Stream {stream_id}: Unknown method '{method}' (id: {req_id})")
            if not is_notification:
                response_model = MCPResponseModel(id=req_id, error={"code": -32601, "message": f"Method not found: {method}"})
            # Ignore unknown notifications

    except (json.JSONDecodeError, ValidationError) as parse_err:
        logger.error(f"MCP Stream {stream_id}: Error parsing/validating message: {parse_err}. Raw Data Hint: {msg_dict}", exc_info=True)
        response_model = MCPResponseModel(id=msg_dict.get("id"), error={"code": -32700, "message": "Parse error/Invalid Request"})
    except Exception as e_msg_proc:
        logger.error(f"MCP Stream {stream_id}: Error processing message: {e_msg_proc}", exc_info=True)
        error_id_val = getattr(mcp_req, 'id', None) if mcp_req else msg_dict.get("id")
        response_model = MCPResponseModel(id=error_id_val, error={"code": -32603, "message": "Internal server error processing message"})

    return response_model


async def mcp_stream_handler(
    stream_id: str,
    initial_messages: List[Dict], # Messages from initial POST body
    request_stream_gen: AsyncGenerator[bytes, None], # For subsequent messages
    send_queue: asyncio.Queue,
    storage_inst: MongoDBStorage,
    app_state: Any
):
    logger.info(f"MCP Stream {stream_id}: Handler started.")
    pending_tool_calls: Dict[Union[str, int], asyncio.Task] = {} # Track background tasks
    buffer = b""

    try:
        # 1. Process initial messages from POST body
        logger.debug(f"MCP Stream {stream_id}: Processing {len(initial_messages)} initial messages.")
        for initial_msg_dict in initial_messages:
            response = await process_single_mcp_message(
                initial_msg_dict, stream_id, storage_inst, send_queue, pending_tool_calls
            )
            if response:
                await send_queue.put(response)

        # 2. Process subsequent messages from the stream
        logger.debug(f"MCP Stream {stream_id}: Starting to listen for subsequent messages.")
        async for chunk in request_stream_gen:
            if not chunk: continue
            logger.debug(f"MCP Stream {stream_id}: Received stream chunk: {len(chunk)} bytes")
            buffer += chunk

            while True: # Process multiple messages if buffered
                separator_pos = buffer.find(STREAM_SEPARATOR)
                if separator_pos == -1: break # Need more data

                message_bytes = buffer[:separator_pos]
                buffer = buffer[separator_pos + len(STREAM_SEPARATOR):]

                if not message_bytes.strip(): continue # Skip potential empty lines

                try:
                    msg_dict = json.loads(message_bytes.decode('utf-8'))
                    response = await process_single_mcp_message(
                        msg_dict, stream_id, storage_inst, send_queue, pending_tool_calls
                    )
                    if response:
                        await send_queue.put(response)
                except json.JSONDecodeError:
                     logger.error(f"MCP Stream {stream_id}: JSONDecodeError for subsequent msg: {message_bytes.decode('utf-8', errors='ignore')[:200]}...")
                     await send_queue.put(MCPResponseModel(id=None, error={"code": -32700, "message": "Parse error"}))
                except Exception as e_subsequent: # Catch errors in subsequent processing
                     logger.error(f"MCP Stream {stream_id}: Error processing subsequent message: {e_subsequent}", exc_info=True)
                     # Try to get ID from the invalid message if possible, otherwise use null
                     error_id = None
                     try: error_id = json.loads(message_bytes.decode('utf-8')).get("id")
                     except Exception: pass
                     await send_queue.put(MCPResponseModel(id=error_id, error={"code": -32603, "message": "Internal error processing message"}))


        logger.info(f"MCP Stream {stream_id}: Client request stream finished.")

    except asyncio.CancelledError:
        logger.info(f"MCP Stream {stream_id}: Handler task cancelled.")
    except Exception as e_handler_outer:
        logger.error(f"MCP Stream {stream_id}: Unhandled error in stream handler: {e_handler_outer}", exc_info=True)
        try: await send_queue.put(MCPResponseModel(id=None, error={"code": -32000, "message": "Hub stream processing error"}))
        except Exception: pass
    finally:
        # Cleanup logic (cancel pending tasks, remove from active streams)
        logger.info(f"MCP Stream {stream_id}: Handler stopping. Cancelling {len(pending_tool_calls)} pending tool calls.")
        for task_obj in list(pending_tool_calls.values()): # Use list copy for safe iteration
            if not task_obj.done():
                task_obj.cancel()
        if pending_tool_calls:
            await asyncio.gather(*pending_tool_calls.values(), return_exceptions=True)
        await send_queue.put(None) # Signal writer to close
        if stream_id in app_state.active_mcp_streams:
            del app_state.active_mcp_streams[stream_id]
        logger.info(f"MCP Stream {stream_id}: Handler finished cleanup.")


@app.post("/mcp/stream", tags=["MCP (Streamable HTTP - Preferred)"])
async def mcp_stream_preferred_endpoint(
    request: Request,
    storage_inst: MongoDBStorage = Depends(get_storage_dependency)
):
    stream_id = str(uuid.uuid4())
    client_host = request.client.host if request.client else "Unknown"
    logger.info(f"MCP Stream {stream_id}: New connection from {client_host}")

    # --- Process Initial Request Body ---
    initial_messages = []
    try:
        initial_body_bytes = await request.body()
        if initial_body_bytes.strip(): # Only process if body is not empty
             initial_body_str = initial_body_bytes.decode('utf-8')
             logger.debug(f"MCP Stream {stream_id}: Initial POST body: {initial_body_str}")
             # Body could be a single message or a batch (array)
             parsed_initial_body = json.loads(initial_body_str)
             if isinstance(parsed_initial_body, list):
                 initial_messages.extend(parsed_initial_body)
             elif isinstance(parsed_initial_body, dict):
                 initial_messages.append(parsed_initial_body)
             else:
                  logger.warning(f"MCP Stream {stream_id}: Invalid initial POST body type: {type(parsed_initial_body)}")
                  # Return error immediately? Or let handler deal with it? Let handler send error response.
                  initial_messages.append({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid initial request format"}}) # Send error via handler
        else:
            logger.debug(f"MCP Stream {stream_id}: Initial POST body is empty.")

    except json.JSONDecodeError:
        logger.error(f"MCP Stream {stream_id}: Invalid JSON in initial POST body.", exc_info=True)
        # Cannot proceed if initial body is invalid JSON
        return JSONResponse(status_code=400, content={"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error in initial request"}})
    except Exception as e_init:
         logger.error(f"MCP Stream {stream_id}: Error processing initial body: {e_init}", exc_info=True)
         return JSONResponse(status_code=500, content={"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": "Internal error processing initial request"}})


    # --- Start Background Handler and Response Stream ---
    send_queue = asyncio.Queue()
    handler_task = asyncio.create_task(
        mcp_stream_handler(
            stream_id,
            initial_messages, # Pass initial messages
            request.stream(), # Pass stream generator for subsequent messages
            send_queue,
            storage_inst,
            request.app.state
        )
    )
    request.app.state.active_mcp_streams[stream_id] = handler_task

    # --- Generator for StreamingResponse ---
    async def response_generator():
        # (Keep implementation from previous response)
        try:
            while True:
                item = await send_queue.get()
                if item is None: logger.info(f"MCP Stream {stream_id}: Closing response generator."); break
                if isinstance(item, MCPResponseModel):
                    try: json_resp_str = item.model_dump_json(exclude_none=True); logger.debug(f"MCP Stream {stream_id}: SEND: {json_resp_str}"); yield json_resp_str.encode('utf-8') + STREAM_SEPARATOR
                    except Exception as e_ser: logger.error(f"MCP Stream {stream_id}: Error serializing: {e_ser}", exc_info=True)
                else: logger.warning(f"MCP Stream {stream_id}: Unknown item in send_queue: {type(item)}")
                send_queue.task_done()
        except asyncio.CancelledError: logger.info(f"MCP Stream {stream_id}: Response generator cancelled.")
        except Exception as e_gen_outer: logger.error(f"MCP Stream {stream_id}: Error in response generator: {e_gen_outer}", exc_info=True)
        finally: logger.info(f"MCP Stream {stream_id}: Response generator finished.");
        if handler_task and not handler_task.done(): handler_task.cancel()

    return StreamingResponse(response_generator(), media_type="application/json-seq")


# --- A2A, Admin, UI, Custom Connect Endpoints ---
# (Keep remaining endpoints as they were)
# ... (rest of the endpoints) ...
@app.post("/a2a/discover", tags=["A2A"])
async def discover_a2a_agents_endpoint(request_obj: Request, query: DiscoverQuery, storage_inst: MongoDBStorage = Depends(get_storage_dependency)): # (Keep implementation)
    query_dict = {};
    if query.status: query_dict["status"] = query.status.value;
    if query.category: query_dict["category"] = query.category;
    if query.agent_tags: query_dict["tags"] = {"$in": query.agent_tags};
    if query.capabilities: query_dict["aira_capabilities"] = {"$in": query.capabilities};
    skill_query_parts = [];
    if query.skill_id: skill_query_parts.append({"a2a_skills.id": query.skill_id});
    if query.skill_tags: skill_query_parts.append({"a2a_skills.tags": {"$in": query.skill_tags}});
    if skill_query_parts: q_and_list = query_dict.get("$and", []); q_and_list.append({"aira_capabilities": "a2a"}); q_and_list.append({"$or": skill_query_parts}); query_dict["$and"] = q_and_list;
    total = await storage_inst.count_agents(query_dict); agents_list = await storage_inst.search_agents(query_dict, query.offset, query.limit); return {"total": total, "offset": query.offset, "limit": query.limit, "agents": agents_list}

@app.get("/a2a/skills", tags=["A2A"])
async def list_a2a_skills_endpoint(request_obj: Request, agent_id: Optional[str] = None, tag: Optional[str] = None, storage_inst: MongoDBStorage = Depends(get_storage_dependency)): # (Keep implementation)
    skills_list = []; query_dict = {"aira_capabilities": "a2a", "status": AgentStatus.ONLINE.value};
    if agent_id: query_dict["agent_id"] = agent_id;
    agents_found = await storage_inst.search_agents(query_dict);
    for agent_obj in agents_found:
        for skill in agent_obj.a2a_skills:
            if tag is None or (skill.tags and tag in skill.tags): skills_list.append({"id": skill.id, "name": skill.name, "description": skill.description, "agent": agent_obj.name, "agent_id": agent_obj.agent_id, "tags": skill.tags, "parameters": skill.parameters, "examples": skill.examples})
    return {"skills": skills_list}

@app.post("/a2a/tasks/send", tags=["A2A"], status_code=202, response_model=A2ATask)
async def a2a_send_task_endpoint(request_obj: Request, storage_inst: MongoDBStorage = Depends(get_storage_dependency)): # (Keep implementation)
    data = await request_obj.json(); agent_id = data.get("agent_id"); skill_id = data.get("skill_id"); task_message = data.get("message"); session_id = data.get("session_id");
    if not all([agent_id, skill_id, task_message, isinstance(task_message, dict)]): raise HTTPException(status_code=400, detail="agent_id, skill_id, and valid 'message' are required")
    agent = await storage_inst.get_agent(agent_id);
    if not agent: raise HTTPException(status_code=404, detail="Agent not found")
    if agent.status != AgentStatus.ONLINE: raise HTTPException(status_code=400, detail=f"Agent {agent.name} offline")
    if "a2a" not in agent.aira_capabilities: raise HTTPException(status_code=400, detail=f"Agent {agent.name} no A2A support")
    if not any(s.id == skill_id for s in agent.a2a_skills): raise HTTPException(status_code=404, detail=f"Skill {skill_id} not found on agent {agent.name}")
    task_obj = A2ATask(agent_id=agent_id, skill_id=skill_id, session_id=session_id, original_message=task_message, current_status=A2ATaskStatusUpdate(state=A2ATaskState.SUBMITTED), history=[task_message])
    if not await storage_inst.save_a2a_task(task_obj): raise HTTPException(status_code=500, detail="Failed save task")
    logger.info(f"A2A Task {task_obj.id} submitted agent {agent.name} ({agent_id}), skill {skill_id}.")
    logger.warning(f"A2A Task {task_obj.id}: Forwarding to agent {agent_id} NOT IMPLEMENTED.")
    return task_obj

@app.get("/a2a/tasks/{task_id}", tags=["A2A"], response_model=A2ATask)
async def a2a_get_task_endpoint(request_obj: Request, task_id: str, storage_inst: MongoDBStorage = Depends(get_storage_dependency)): # (Keep implementation)
    task_obj = await storage_inst.get_a2a_task(task_id);
    if not task_obj: raise HTTPException(status_code=404, detail="A2A Task not found")
    # Mock state transition (keep implementation)
    if task_obj.current_status.state == A2ATaskState.SUBMITTED and (datetime.now(timezone.utc) - task_obj.created_at).total_seconds() > 5:
        logger.info(f"A2A Task {task_obj.id}: Mock transition to WORKING."); task_obj.current_status = A2ATaskStatusUpdate(state=A2ATaskState.WORKING, message="Processing (mock)."); task_obj.history.append({"role": "system", "parts": [{"type": "status_update", "text": task_obj.current_status.message}]}); await storage_inst.save_a2a_task(task_obj);
    return task_obj

@app.get("/analytics/summary", tags=["Analytics"])
async def analytics_summary_endpoint(request_obj: Request, storage_inst: MongoDBStorage = Depends(get_storage_dependency)): # (Keep implementation)
    agents_list = await storage_inst.list_agents(); status_counts = {s.value: 0 for s in AgentStatus}; capability_counts = {}; total_tools_count = 0; total_skills_count = 0; tag_counts_map = {}
    for agent_obj in agents_list: status_counts[agent_obj.status.value] = status_counts.get(agent_obj.status.value, 0) + 1;
    for cap in agent_obj.aira_capabilities: capability_counts[cap] = capability_counts.get(cap, 0) + 1;
    total_tools_count += len(agent_obj.mcp_tools); total_skills_count += len(agent_obj.a2a_skills);
    for tag_item in agent_obj.tags: tag_counts_map[tag_item] = tag_counts_map.get(tag_item, 0) + 1;
    for skill in agent_obj.a2a_skills:
        for skill_tag in skill.tags: tag_counts_map[skill_tag] = tag_counts_map.get(skill_tag, 0) + 1;
    top_tags_dict = dict(sorted(tag_counts_map.items(), key=lambda item: item[1], reverse=True)[:10]); now = time.time(); agents_added_today_count = len([a for a in agents_list if (now - a.created_at) < 86400]);
    num_a2a_tasks = await storage_inst.db.tasks.count_documents({}) if storage_inst.db else 0; num_completed_a2a_tasks = await storage_inst.db.tasks.count_documents({"current_status.state": A2ATaskState.COMPLETED.value}) if storage_inst.db else 0;
    return {"total_agents": len(agents_list), "status_counts": status_counts, "capability_counts": capability_counts, "total_tools": total_tools_count, "total_skills": total_skills_count, "top_tags": top_tags_dict, "agents_added_today": agents_added_today_count, "total_a2a_tasks_recorded": num_a2a_tasks, "completed_a2a_tasks_recorded": num_completed_a2a_tasks}

@app.get("/analytics/activity", tags=["Analytics"])
async def analytics_activity_endpoint(request_obj: Request, days: int = 7, storage_inst: MongoDBStorage = Depends(get_storage_dependency)): # (Keep implementation)
    activity_data_list = []; now_ts = time.time(); day_seconds = 86400; agents_all = await storage_inst.list_agents();
    for i in range(days):
        range_end_ts = now_ts - (i * day_seconds); range_start_ts = range_end_ts - day_seconds; day_date_str = datetime.fromtimestamp(range_start_ts).strftime("%Y-%m-%d");
        new_registrations_count = len([a for a in agents_all if range_start_ts <= a.created_at < range_end_ts]);
        active_agents_approx_count = len([a for a in agents_all if a.last_seen >= range_start_ts or (a.status == AgentStatus.ONLINE and a.last_seen < range_start_ts and (now_ts - a.last_seen) <= DEFAULT_HEARTBEAT_TIMEOUT)])
        a2a_tasks_created_count = 0;
        if storage_inst.db: a2a_tasks_created_count = await storage_inst.db.tasks.count_documents({"created_at": {"$gte": datetime.fromtimestamp(range_start_ts, timezone.utc), "$lt": datetime.fromtimestamp(range_end_ts, timezone.utc)}})
        activity_data_list.append({"date": day_date_str, "new_agent_registrations": new_registrations_count, "approx_active_agents": active_agents_approx_count, "a2a_tasks_created": a2a_tasks_created_count})
    return {"activity": list(reversed(activity_data_list))}

@app.post("/admin/sync_agents", tags=["Admin"])
async def admin_sync_agents_endpoint(request_obj: Request, storage_inst: MongoDBStorage = Depends(get_storage_dependency)): # (Keep implementation)
    data = await request_obj.json(); hub_urls = data.get("hub_urls", []);
    if not hub_urls or not isinstance(hub_urls, list): raise HTTPException(status_code=400, detail="'hub_urls' list required")
    results_map = {};
    async with httpx.AsyncClient(timeout=30.0) as http_client:
        for hub_url_str in hub_urls:
            try:
                sync_url_str = urllib.parse.urljoin(hub_url_str.rstrip('/') + '/', "agents"); logger.info(f"Syncing from {sync_url_str}"); response = await http_client.get(sync_url_str);
                if response.status_code != 200: logger.warning(f"Failed get agents {hub_url_str}: HTTP {response.status_code}"); results_map[hub_url_str] = {"status": "error", "message": f"Failed: HTTP {response.status_code}"}; continue
                remote_data_dict = response.json(); remote_agents_list_data = remote_data_dict.get("agents", []); registered_c, skipped_c, failed_c = 0, 0, 0
                for agent_data_dict in remote_agents_list_data:
                    try:
                        if not agent_data_dict.get('url') or not agent_data_dict.get('name'): logger.warning(f"Skip remote agent {hub_url_str}, missing URL/Name: {agent_data_dict.get('url')}"); failed_c += 1; continue
                        agent_to_create = AgentRegistration(url=agent_data_dict['url'], name=agent_data_dict['name'], description=agent_data_dict.get('description'), version=agent_data_dict.get('version', "1.0.0"), mcp_tools=[MCPTool(**t) for t in agent_data_dict.get('mcp_tools', [])], a2a_skills=[A2ASkill(**s) for s in agent_data_dict.get('a2a_skills', [])], aira_capabilities=agent_data_dict.get('aira_capabilities', []), tags=agent_data_dict.get('tags', []), category=agent_data_dict.get('category'), provider=agent_data_dict.get('provider'), mcp_url=agent_data_dict.get('mcp_url'), mcp_sse_url=agent_data_dict.get('mcp_sse_url'), mcp_stream_url=agent_data_dict.get('mcp_stream_url'))
                        if await storage_inst.get_agent_by_url(agent_to_create.url): skipped_c += 1; logger.debug(f"Agent {agent_to_create.name} exists, skip sync."); continue
                        await storage_inst.save_agent(agent_to_create); registered_c += 1; logger.info(f"Synced new agent: {agent_to_create.name}")
                    except Exception as e_agent_proc: agent_name_preview = agent_data_dict.get('name', agent_data_dict.get('url', 'Unknown')); logger.error(f"Error processing remote agent '{agent_name_preview}' from {hub_url_str}: {e_agent_proc}"); failed_c += 1
                results_map[hub_url_str] = {"status": "success", "registered_new": registered_c, "skipped_existing": skipped_c, "failed_to_process": failed_c, "total_remote": len(remote_agents_list_data)}
            except httpx.RequestError as e_req_outer: logger.warning(f"Network error sync {hub_url_str}: {e_req_outer}"); results_map[hub_url_str] = {"status": "error", "message": f"Network error: {e_req_outer}"}
            except Exception as e_hub_outer: logger.error(f"Unexpected error sync {hub_url_str}: {e_hub_outer}", exc_info=True); results_map[hub_url_str] = {"status": "error", "message": f"Internal processing error: {e_hub_outer}"}
    return {"results": results_map}

@app.post("/admin/cleanup", tags=["Admin"])
async def admin_cleanup_endpoint(request_obj: Request, storage_inst: MongoDBStorage = Depends(get_storage_dependency)): # (Keep implementation)
    data = await request_obj.json(); agent_threshold_sec = data.get("agent_threshold", DEFAULT_HEARTBEAT_TIMEOUT * 3); session_threshold_sec = data.get("session_threshold", 3600); now = time.time(); agent_removed_count = 0;
    agents_all = await storage_inst.list_agents(); agent_ids_to_remove_list = [a.agent_id for a in agents_all if (now - a.last_seen) > agent_threshold_sec]
    if agent_ids_to_remove_list:
        delete_results = await asyncio.gather(*[storage_inst.delete_agent(aid) for aid in agent_ids_to_remove_list], return_exceptions=True); agent_removed_count = sum(1 for r in delete_results if r is True);
        for r_idx, r_val in enumerate(delete_results):
             if isinstance(r_val, Exception): logger.error(f"Error deleting agent {agent_ids_to_remove_list[r_idx]}: {r_val}", exc_info=True)
    session_removed_count = 0;
    if hasattr(app.state, 'mcp_session_manager'): session_removed_count = app.state.mcp_session_manager.cleanup_stale_sessions(session_threshold_sec)
    return {"status": "success", "agents_removed": agent_removed_count, "sessions_removed": session_removed_count}

@app.post("/admin/broadcast", tags=["Admin"])
async def admin_broadcast_endpoint(request_obj: Request, background_tasks: BackgroundTasks): # (Keep implementation)
    if not hasattr(request_obj.app.state, 'connection_manager'): raise HTTPException(status_code=503, detail="Connection manager unavailable")
    try:
        data = await request_obj.json(); message_text = data.get("message"); event_type_str = data.get("event_type", "admin_message");
        if not message_text or not isinstance(message_text, str): raise HTTPException(status_code=400, detail="'message' string required")
        broadcast_data_dict = {"message": message_text, "timestamp": time.time()}; background_tasks.add_task(request_obj.app.state.connection_manager.broadcast_event, event_type_str, broadcast_data_dict)
        recipients_queued_count = len(request_obj.app.state.connection_manager.get_all_connections()); logger.info(f"Admin broadcast '{event_type_str}' queued for {recipients_queued_count} clients."); return {"status": "success", "recipients_queued": recipients_queued_count}
    except HTTPException: raise
    except Exception as e: logger.error(f"Error admin broadcast: {e}", exc_info=True); raise HTTPException(status_code=500, detail=f"Internal error: {e}")

@app.get("/ui", tags=["UI"])
async def ui_dashboard_endpoint(request: Request): # (Keep implementation)
    return JSONResponse(content={"message": "AIRA Hub UI not implemented yet. Refer to /docs."})

@app.post("/connect/stream", tags=["Custom Agent Connect"])
async def connect_stream_custom_endpoint(request_obj: Request, agent_url: str, name: str, aira_capabilities: Optional[str] = None, storage_inst: MongoDBStorage = Depends(get_storage_dependency)): # (Keep implementation)
    logger.warning("/connect/stream is custom, non-standard."); capabilities_list = aira_capabilities.split(",") if aira_capabilities else []
    async def event_generator_custom():
        async with httpx.AsyncClient(timeout=None) as http_client_custom:
            try:
                logger.info(f"Custom Connect: Proxying SSE from agent: {agent_url}")
                async with http_client_custom.stream("GET", agent_url, headers={"Accept": "text/event-stream"}) as response_stream:
                    if response_stream.status_code != 200: err_msg = f"Custom Connect: Agent stream failed HTTP {response_stream.status_code}"; logger.error(err_msg); yield f"event: error\ndata: {json.dumps({'error': err_msg})}\n\n"; return
                    existing_agent = await storage_inst.get_agent_by_url(agent_url); agent_id_for_stream: str
                    if not existing_agent: agent_obj_new = AgentRegistration(url=agent_url, name=name, description=f"Registered via custom SSE {agent_url}", aira_capabilities=capabilities_list, mcp_sse_url=agent_url); await storage_inst.save_agent(agent_obj_new); agent_id_for_stream = agent_obj_new.agent_id; logger.info(f"Custom Connect: Registered new agent via SSE: {agent_obj_new.name} ({agent_id_for_stream})")
                    else: agent_id_for_stream = existing_agent.agent_id; existing_agent.status = AgentStatus.ONLINE; existing_agent.last_seen = time.time(); existing_agent.mcp_sse_url = agent_url; await storage_inst.save_agent(existing_agent); logger.info(f"Custom Connect: Updated agent via SSE: {existing_agent.name} ({agent_id_for_stream})")
                    async for line_text in response_stream.aiter_lines():
                        if line_text.startswith("event: endpoint"):
                            try:
                                data_line_text = await response_stream.aiter_lines().__anext__();
                                if data_line_text.startswith("data:"):
                                    endpoint_url_str = data_line_text[len("data:"):].strip();
                                    try:
                                        endpoint_url_parsed = json.loads(endpoint_url_str);
                                        if isinstance(endpoint_url_parsed, str):
                                            agent_to_update_mcp = await storage_inst.get_agent(agent_id_for_stream);
                                            if agent_to_update_mcp:
                                                resolved_mcp_url = endpoint_url_parsed;
                                                if not (endpoint_url_parsed.startswith("http://") or endpoint_url_parsed.startswith("https://")): base_agent_url_parts = urllib.parse.urlparse(agent_url); base_url_str = f"{base_agent_url_parts.scheme}://{base_agent_url_parts.netloc}"; resolved_mcp_url = urllib.parse.urljoin(base_url_str, endpoint_url_parsed); logger.info(f"Custom Connect: Resolved relative MCP URL for {agent_to_update_mcp.name}: {resolved_mcp_url}");
                                                agent_to_update_mcp.mcp_url = resolved_mcp_url; await storage_inst.save_agent(agent_to_update_mcp); logger.info(f"Custom Connect: Updated agent {agent_to_update_mcp.name} MCP URL: {agent_to_update_mcp.mcp_url}")
                                        else: logger.warning(f"Custom Connect: Endpoint data not string: {endpoint_url_str}")
                                    except json.JSONDecodeError: logger.warning(f"Custom Connect: Endpoint data not JSON: {endpoint_url_str}")
                                    except Exception as e_update: logger.error(f"Custom Connect: Error updating agent MCP URL for {agent_id_for_stream}: {e_update}", exc_info=True)
                                    yield line_text + "\n"; yield data_line_text + "\n\n"
                                else: yield line_text + "\n";
                            except StopAsyncIteration: logger.warning("Custom Connect: Stream ended after endpoint event."); yield line_text + "\n\n"; break
                        else: yield line_text + "\n";
                        if not line_text and not line_text.startswith("data:") and not line_text.startswith(":"): yield "\n";
                    logger.info(f"Custom Connect: Agent stream ended for {agent_url}")
            except httpx.RequestError as e_req_custom: logger.error(f"Custom Connect: Network error proxying SSE {agent_url}: {e_req_custom}", exc_info=True); yield f"event: error\ndata: {json.dumps({'error': f'Network error agent: {str(e_req_custom)}'})}\n\n"
            except Exception as e_custom_outer: logger.error(f"Custom Connect: Unexpected error proxying SSE {agent_url}: {e_custom_outer}", exc_info=True); yield f"event: error\ndata: {json.dumps({'error': f'Error proxying stream: {str(e_custom_outer)}'})}\n\n"
    sse_resp_headers = {"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}; return Response(event_generator_custom(), headers=sse_resp_headers, media_type="text/event-stream") # Changed to Response to allow generator

@app.post("/connect/stream/init", tags=["Custom Agent Connect"])
async def connect_stream_init_custom_endpoint(request_obj: Request, background_tasks: BackgroundTasks, storage_inst: MongoDBStorage = Depends(get_storage_dependency)): # (Keep implementation)
    logger.warning("/connect/stream/init custom, non-standard. Prefer /register.");
    try:
        data = await request_obj.json(); agent_payload_dict = {"url": data.get("url"), "name": data.get("name"), "description": data.get("description"), "version": data.get("version", "1.0.0"), "mcp_tools": data.get("mcp_tools", []), "a2a_skills": data.get("a2a_skills", []), "aira_capabilities": data.get("aira_capabilities", []), "tags": data.get("tags", []), "category": data.get("category"), "provider": data.get("provider"), "mcp_url": data.get("mcp_url"), "mcp_sse_url": data.get("mcp_sse_url"), "mcp_stream_url": data.get("mcp_stream_url")}
        if not agent_payload_dict["url"] or not agent_payload_dict["name"]: raise HTTPException(status_code=400, detail="URL/Name required for /connect/stream/init")
        try: validated_mcp_tools = [MCPTool(**t) for t in agent_payload_dict.pop("mcp_tools", [])]; validated_a2a_skills = [A2ASkill(**s) for s in agent_payload_dict.pop("a2a_skills", [])]
        except Exception as p_val_err: raise HTTPException(status_code=400, detail=f"Invalid tools/skills data: {p_val_err}")
        agent_payload_obj = AgentRegistration(**{k: v for k, v in agent_payload_dict.items() if v is not None}, mcp_tools=validated_mcp_tools, a2a_skills=validated_a2a_skills)
        existing_agent = await storage_inst.get_agent_by_url(agent_payload_obj.url); agent_to_save = agent_payload_obj; status_msg = "initialized_new"
        if existing_agent: agent_to_save.agent_id = existing_agent.agent_id; agent_to_save.created_at = existing_agent.created_at; status_msg = "initialized_updated"; logger.info(f"Custom Init: Update agent {agent_to_save.agent_id} for {agent_to_save.url}")
        else: logger.info(f"Custom Init: Create new agent for {agent_to_save.url}")
        agent_to_save.status = AgentStatus.ONLINE; agent_to_save.last_seen = time.time();
        if not await storage_inst.save_agent(agent_to_save): raise HTTPException(status_code=500, detail="Failed save agent via custom init")
        if hasattr(request_obj.app.state, 'connection_manager'): background_tasks.add_task(request_obj.app.state.connection_manager.broadcast_event, "agent_updated", {"agent_id": agent_to_save.agent_id, "name": agent_to_save.name, "url": agent_to_save.url, "status": agent_to_save.status.value})
        logger.info(f"Custom Init: Agent {status_msg} for: {agent_to_save.name} ({agent_to_save.agent_id})"); return JSONResponse(content={"status": status_msg, "agent_id": agent_to_save.agent_id})
    except HTTPException: raise
    except Exception as e: logger.error(f"Error /connect/stream/init: {e}", exc_info=True); raise HTTPException(status_code=500, detail=f"Internal error: {e}")


# ----- Main Entry Point -----
if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8015))
    host = os.environ.get("HOST", "0.0.0.0")
    logger.info(f"Starting AIRA Hub on {host}:{port}")
    uvicorn.run(
        "aira_hub:app",
        host=host, port=port,
        reload=os.environ.get("DEBUG", "false").lower() == "true",
        log_level="info" # Uvicorn's log level
    )
