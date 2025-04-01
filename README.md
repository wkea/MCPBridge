# MCPBridge

MCPBridge是一个轻量级服务，旨在让所有LLM客户端能够兼容MCP服务器工具。它提供HTTP接口用于访问MCP协议功能，允许各种AI客户端与多种AI模型交互并利用强大的工具能力。

## 主要特性

- 提供HTTP API接口，兼容OpenAI API格式，支持任何LLM客户端接入
- 桥接您的LLM客户端与MCP工具服务器，无需修改客户端代码
- 支持与大语言模型交互（目前支持DeepSeek）
- 集成MCP协议，使普通LLM客户端能够使用MCP工具功能
- 支持流式响应，提供更好的用户体验
- 分段式工具调用，先展示初始响应，再显示工具执行结果
- 内置API密钥验证保护，防止未授权访问
- 服务器启动时自动初始化MCP服务器和工具
- 自动检查和安装工具所需的依赖
- 提供状态检查API，方便监控服务器状态
- 简单易用的配置方式

## 安装指南

### 前提条件

- Python 3.8 或更高版本
- Node.js 和 npm (用于支持npx工具调用)
- 访问DeepSeek API的密钥

### 安装步骤

1. 克隆或下载代码仓库

2. 安装依赖包:
```bash
pip install -r requirements.txt
```

3. 复制环境变量示例文件并配置:
```bash
cp .env.example .env
```

4. 编辑 `.env` 文件，配置必要的参数:
   - 设置本地API密钥
   - 配置模型API密钥
   - 根据需要调整其他参数

## 配置说明

配置文件 `.env` 主要包含以下部分:

### 本地API服务器设置
```
API_PORT=55545             # 本地服务器端口
API_HOST=127.0.0.1         # 本地服务器IP地址
API_KEY=your_api_key_here  # 本地API访问密钥，用于验证请求
REQUEST_MODEL_ID=Seraphina # 本地模型ID，可自定义
```

### MCP服务器设置
```
SSE_API_KEY=your_sse_api_key_here  # 如果使用SSE服务器，配置此密钥
```

### 模型设置
```
MODEL_API_KEY=your_model_api_key_here  # DeepSeek API密钥
MODEL_API_BASE=https://api.deepseek.com/v1  # API基础URL
MODEL=deepseek-chat  # 使用的模型
```

## 运行方法

启动API服务器:
```bash
python api_server.py
```

默认情况下，服务器将在配置的端口上运行。服务器启动时会自动初始化MCP服务器和可用工具，无需等待第一个请求到来才进行初始化。您可以通过浏览器访问 `http://127.0.0.1:55545/` 来确认服务器是否正常运行。

您还可以通过访问 `http://127.0.0.1:55545/status` 来查看服务器的当前状态，包括初始化状态、可用工具和模型等信息。

## MCP工具服务器配置

MCP服务器配置位于 `servers_config.json` 文件中，支持两种类型的服务器:

### SSE服务器
```json
"sse-server": {
  "url": "http://localhost:8000/sse",
  "headers": {
    "Authorization": "Bearer ${SSE_API_KEY}"
  },
  "timeout": 5,
  "sse_read_timeout": 300
}
```

### STDIO服务器(NPX工具)
```json
"desktop-automation": {
  "name": "桌面操作",
  "isActive": true,
  "command": "npx",
  "args": [
    "-y",
    "mcp-desktop-automation"
  ],
  "dependencies": [
    "mcp-desktop-automation"
  ]
}
```

对于STDIO服务器，API会自动检查并安装配置的依赖项。您可以通过 `dependencies` 字段指定依赖列表，支持版本要求(如 `package@latest`)。

## API接口

### 状态检查

- **端点**: `/status`
- **方法**: GET
- **响应示例**:
```json
{
  "status": "ready",
  "servers_count": 1,
  "tools_count": 5,
  "default_model": "Seraphina",
  "available_tools": [
    {"name": "get_weather", "description": "获取指定城市的天气信息"},
    {"name": "search_web", "description": "在网络上搜索信息"}
  ],
  "available_models": ["Seraphina"]
}
```

### 聊天接口

- **端点**: `/v1/chat/completions`
- **方法**: POST
- **请求头**:
  - `Content-Type: application/json`
  - `X-API-Key: your_api_key_here`

- **请求体示例**:
```json
{
  "model": "Seraphina",
  "messages": [
    {"role": "user", "content": "你好，能帮我查询一下今天的天气吗？"}
  ],
  "stream": true
}
```

- **响应示例**:
```json
{
  "id": "chatcmpl-123456789",
  "object": "chat.completion",
  "created": 1677858242,
  "model": "Seraphina",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "你好！我很乐意帮你查询今天的天气。让我使用天气查询工具...\n查询结果显示，今天天气晴朗，气温25℃，适合户外活动。"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": -1,
    "completion_tokens": -1,
    "total_tokens": -1
  },
  "tool_used": true
}
```

## 工具调用

系统支持通过MCP协议进行工具调用。当模型需要使用工具时，会返回包含工具调用指令的JSON响应，格式如下:

```json
{
  "say": "我需要查询天气信息",
  "use": {
    "tool": "get_weather",
    "params": {
      "city": "北京"
    }
  }
}
```

服务器会自动处理工具调用，并将结果返回给模型生成最终回复。

## 与现有LLM客户端集成

MCPBridge设计为即插即用的解决方案，可以轻松集成到现有的LLM客户端系统中：

1. **标准OpenAI客户端** - 将API端点修改为MCPBridge地址即可
2. **自定义客户端** - 只需实现兼容OpenAI格式的API调用
3. **UI界面** - 任何使用OpenAI接口的ChatUI都可以直接连接到MCPBridge

无需对您现有的客户端代码进行大量修改，只需调整API地址和密钥配置，即可让您的客户端具备MCP工具能力。

## 示例用法

### 使用curl发送请求

```bash
curl -X POST http://127.0.0.1:55545/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your_api_key_here" \
  -d '{
    "model": "Seraphina",
    "messages": [
      {"role": "user", "content": "你好，能简单介绍下自己吗？"}
    ]
  }'
```

### 使用Python发送请求

```python
import requests

url = "http://127.0.0.1:55545/v1/chat/completions"
headers = {
    "Content-Type": "application/json",
    "X-API-Key": "your_api_key_here"
}
data = {
    "model": "Seraphina",
    "messages": [
        {"role": "user", "content": "你好，能简单介绍下自己吗？"}
    ]
}

response = requests.post(url, json=data, headers=headers)
print(response.json())
```

## 常见问题

### 服务器启动失败
- 检查端口是否被占用
- 确认所有必要的环境变量已正确设置
- 检查Python依赖是否安装完整

### API请求失败
- 确认API密钥正确设置并在请求中包含
- 检查请求格式是否正确
- 查看服务器日志获取详细错误信息

### 工具执行问题
- 确保MCP服务器配置正确
- 检查相关工具的依赖是否正确安装
- 查看日志了解具体错误原因

## 开发和贡献

欢迎通过Pull Request或Issues提供反馈和改进建议。

## 许可

本项目基于Apache-2.0许可协议开源。

