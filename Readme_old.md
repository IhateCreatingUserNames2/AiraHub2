

markdown
# Aira Hub

A platform for hosting and managing MCP (Multi-Client Protocol) servers.

****** THIS SETUP USES MONGODB CLOUD TO STORE DATA ******** 

## Demo
Currently running at: [https://airahub2.onrender.com/](https://airahub2.onrender.com/)

## Installation & Usage

### Hosting Aira Hub
To host your own Aira Hub instance:
```
python aira_hub.py
Managing MCP Servers
Use the agent manager to run MCP servers and broadcast them to Aira Hub:

bash
Populate mcp_servers.json with MCP SERVERS CONFIGURATIONS, CHECK THE TEMPLATE. ASK chatGPT to format it for new MCP Servers or adjust... 
python agent_manager.py
Connecting Clients
Connect your MCP client to Aira Hub. Example configuration for Claude:

Online Demo Configuration
json
{ 
  "mcpServers": { 
    "aira-hub": { 
      "command": "npx", 
      "args": [ 
        "mcp-remote",
        "https://airahub2.onrender.com/mcp/stream"
      ]
    }
  } 
}
Localhost Configuration
json
[[ 
  { 
    "mcpServers": { 
      "aira-hub": { 
        "command": "npx", 
        "args": [ 
          "mcp-remote",
          "http://localhost:8017/mcp/stream"
        ]
      }
    } 
  } 
]]


```
Features
Centralized hub for MCP servers

Easy server management through agent_manager

Remote client connectivity
"""


####

Running Thru NGROK 
![image](https://github.com/user-attachments/assets/54aab81d-c454-43cc-8ae0-013a23315605)

![image](https://github.com/user-attachments/assets/50d9c998-495a-4475-8c4b-1b0eac6cb1b1)

####
![image](https://github.com/user-attachments/assets/7e1c6f80-06e6-47ba-bc09-5f71afe0498c)


![image](https://github.com/user-attachments/assets/72226304-6a0e-47e6-b788-19db2fe9f63d)

![image](https://github.com/user-attachments/assets/50b31f8e-7a46-4d2c-b072-0b596dfaf1db)
