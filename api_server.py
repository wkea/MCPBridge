import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Union, Literal, Tuple, AsyncGenerator
from enum import Enum

import uvicorn
from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator, field_validator
from fastapi.responses import StreamingResponse

from mcp_client import Configuration, Server, Tool

# 配置日志
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

app = FastAPI(
    title="MCP API服务器",
    description="MCP客户端API接口，允许其他LLM客户端通过HTTP接口访问MCP功能",
    version="1.0.0",
)

# 添加CORS支持
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 在生产环境中应该限制为特定域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 全局变量
config = Configuration()
servers: List[Server] = []
tools_by_name: Dict[str, Tool] = {}
all_tools: List[Tool] = []
initialized = False
initialization_lock = asyncio.Lock()

# API密钥验证
API_KEY = os.getenv("API_KEY")
if not API_KEY:
    raise ValueError("API_KEY环境变量未设置")

async def verify_api_key(
    x_api_key: str = Header(None),
    authorization: str = Header(None)
):
    """验证API密钥，同时支持X-API-Key和Authorization Bearer格式
    
    Args:
        x_api_key: X-API-Key请求头
        authorization: Authorization请求头（Bearer格式）
        
    Returns:
        验证通过的API密钥
        
    Raises:
        HTTPException: 验证失败时抛出异常
    """
    # 从Authorization Bearer格式提取API密钥
    bearer_key = None
    if authorization and authorization.startswith("Bearer "):
        bearer_key = authorization.split("Bearer ")[1].strip()
    
    # 优先使用X-API-Key，其次使用Bearer Token
    api_key = x_api_key or bearer_key
    
    if not api_key:
        raise HTTPException(status_code=401, detail="缺少API密钥，请提供X-API-Key或Authorization Bearer")
    
    if api_key != API_KEY:
        raise HTTPException(status_code=403, detail="API密钥无效")
    
    return api_key

# 模型映射配置
MODEL_MAPPINGS: Dict[str, Tuple[str, str]] = {}
DEFAULT_MODEL = os.getenv("REQUEST_MODEL_ID", "Seraphina")

# 加载模型映射配置
def load_model_mappings():
    """从环境变量加载模型映射配置"""
    global MODEL_MAPPINGS
    
    MODEL_MAPPINGS = {
        DEFAULT_MODEL: ("deepseek", os.getenv("MODEL", "deepseek-chat"))
    }
    logging.info(f"加载模型映射: {DEFAULT_MODEL} -> deepseek:{os.getenv('MODEL', 'deepseek-chat')}")


# DeepSeek客户端实现
class DeepSeekClient:
    """DeepSeek API客户端实现"""
    
    def __init__(self, api_key: str = None, api_base: str = None) -> None:
        """初始化DeepSeek客户端
        
        Args:
            api_key: DeepSeek API密钥
            api_base: DeepSeek API基础URL
        """
        self.api_key = api_key or os.getenv("MODEL_API_KEY")
        self.api_base = api_base or os.getenv("MODEL_API_BASE", "https://api.deepseek.com/v1")
        self.model = os.getenv("MODEL", "deepseek-chat")
        self.api_version = os.getenv("MODEL_API_VERSION", "2024-02-15")
        
        if not self.api_key:
            raise ValueError("MODEL_API_KEY 未设置")
    
    async def get_streaming_response(self, messages: List[Dict[str, str]], fake_streaming: bool = False) -> AsyncGenerator[str, None]:
        """从DeepSeek获取流式响应
        
        Args:
            messages: 消息列表
            fake_streaming: 是否使用伪流模式（等待工具执行完毕再流式返回）
            
        Returns:
            流式响应生成器
        """
        import httpx
        
        try:
            if fake_streaming:
                # 伪流模式：先获取完整响应，然后模拟流式返回
                complete_response = await self.get_response(messages, parse_json=False)
                
                # 解析JSON响应
                try:
                    response_json = json.loads(complete_response)
                    content = response_json.get("say", complete_response)
                    
                    # 模拟流式返回
                    async for chunk in self._generate_fake_stream(content):
                        yield chunk
                    return
                except Exception as e:
                    logging.error(f"解析JSON响应失败: {e}")
                    raise
            
            # 正常流模式
            url = f"{self.api_base}/chat/completions"
            
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "api-version": self.api_version
            }
            
            payload = {
                "messages": messages,
                "model": self.model,
                "temperature": 0.7,
                "max_tokens": 4096,
                "stream": True,
                "response_format": {"type": "json_object"}
            }
            
            async with httpx.AsyncClient(timeout=60.0) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    response.raise_for_status()
                    
                    # 收集完整的JSON字符串
                    json_buffer = ""
                    
                    async for line in response.aiter_lines():
                        if line.strip() and line.startswith("data: "):
                            if line.strip() == "data: [DONE]":
                                yield "data: [DONE]\n\n"
                                continue
                                
                            try:
                                data = json.loads(line[6:])
                                if data.get("choices") and len(data["choices"]) > 0:
                                    delta = data["choices"][0].get("delta", {})
                                    if "content" in delta:
                                        content = delta["content"]
                                        json_buffer += content
                                        
                                        chunk = {
                                            "id": data.get("id", f"chatcmpl-{id(line)}"),
                                            "object": "chat.completion.chunk",
                                            "created": int(asyncio.get_event_loop().time()),
                                            "model": self.model,
                                            "choices": [
                                                {
                                                    "index": 0,
                                                    "delta": {"content": content},
                                                    "finish_reason": data["choices"][0].get("finish_reason")
                                                }
                                            ]
                                        }
                                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                            except json.JSONDecodeError:
                                continue
        except httpx.RequestError as e:
            logging.error(f"请求DeepSeek失败: {e}")
            raise
        except Exception as e:
            logging.error(f"流式响应处理失败: {e}")
            raise
    
    async def _generate_fake_stream(self, content: str):
        """生成伪流式响应
        
        Args:
            content: 要分段发送的内容
            
        Returns:
            流式响应生成器
        """
        # 模拟流式返回
        for i in range(0, len(content), 4):
            chunk_text = content[i:i+4]
            chunk = {
                "id": f"chatcmpl-{id(chunk_text)}",
                "object": "chat.completion.chunk",
                "created": int(asyncio.get_event_loop().time()),
                "model": self.model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": chunk_text},
                        "finish_reason": None if i + 4 < len(content) else "stop"
                    }
                ]
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            await asyncio.sleep(0.01)  # 添加短暂延迟模拟真实流式体验
        
        # 发送结束标记
        yield "data: [DONE]\n\n"
    
    async def get_response(self, messages: List[Dict[str, str]], parse_json: bool = True) -> str:
        """从DeepSeek获取非流式响应
        
        Args:
            messages: 消息列表
            parse_json: 是否解析JSON响应并仅返回say字段
            
        Returns:
            LLM响应文本
        """
        import httpx
        
        url = f"{self.api_base}/chat/completions"
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "api-version": self.api_version
        }
        
        payload = {
            "messages": messages,
            "model": self.model,
            "temperature": 0.7,
            "max_tokens": 4096,
            "stream": False,
            "response_format": {"type": "json_object"}
        }
        
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                result = response.json()
                content = result["choices"][0]["message"]["content"]
                
                if parse_json:
                    try:
                        json_content = json.loads(content)
                        if "say" in json_content:
                            return json_content
                        return content
                    except json.JSONDecodeError:
                        return content
                return content
        except (httpx.RequestError, KeyError, json.JSONDecodeError) as e:
            logging.error(f"DeepSeek API请求或响应解析失败: {e}")
            raise ValueError(f"DeepSeek响应处理错误: {str(e)}")
    
    async def execute_with_tool(self, messages: List[Dict[str, str]]) -> Tuple[str, bool, str]:
        """执行带工具调用的请求
        
        Args:
            messages: 消息列表
            
        Returns:
            (最终响应文本, 是否使用了工具, 第一段响应)
        """
        # 获取初始LLM响应
        response_obj = await self.get_response(messages)
        
        # 解析可能的工具调用
        first_response = ""
        final_response = ""
        tool_used = False
        
        if isinstance(response_obj, dict) and "say" in response_obj:
            first_response = response_obj["say"]
            final_response = first_response
            
            # 检查是否包含工具调用
            if "use" in response_obj and isinstance(response_obj["use"], dict):
                tool_info = response_obj["use"]
                tool_name = tool_info.get("tool")
                params = tool_info.get("params", {})
                
                if tool_name:
                    try:
                        # 查找服务器并执行工具
                        tool_executed = False
                        tool_result = None
                        
                        # 检查该工具是否在工具列表中
                        target_tool = tools_by_name.get(tool_name)
                        
                        if target_tool:
                            # 首先尝试在提供该工具的服务器上执行
                            target_server_name = target_tool.server_name
                            
                            if target_server_name:
                                # 找到目标服务器
                                target_server = next((s for s in servers if s.name == target_server_name), None)
                                
                                if target_server:
                                    try:
                                        logging.info(f"在指定服务器 {target_server_name} 上执行工具 {tool_name}...")
                                        tool_result = await target_server.execute_tool(tool_name, params)
                                        tool_executed = True
                                        tool_used = True
                                        logging.info(f"工具 {tool_name} 在服务器 {target_server_name} 上执行成功")
                                    except Exception as e:
                                        logging.warning(f"在服务器 {target_server_name} 上执行工具 {tool_name} 失败: {e}")
                        
                        # 如果尚未成功执行，尝试在所有服务器上执行
                        if not tool_executed:
                            logging.info(f"在所有可用服务器上尝试执行工具 {tool_name}...")
                            for server in servers:
                                try:
                                    logging.info(f"尝试在服务器 {server.name} 上执行工具 {tool_name}...")
                                    tool_result = await server.execute_tool(tool_name, params)
                                    tool_executed = True
                                    tool_used = True
                                    logging.info(f"工具 {tool_name} 在服务器 {server.name} 上执行成功")
                                    break
                                except Exception as e:
                                    logging.warning(f"在服务器 {server.name} 上执行工具 {tool_name} 失败: {e}")
                                    continue
                        
                        if tool_executed and tool_result is not None:
                            # 将工具结果转换为可序列化格式
                            tool_result_serializable = self._convert_to_serializable(tool_result)
                            
                            # 将工具结果添加到消息中
                            new_messages = messages.copy()
                            new_messages.append({"role": "assistant", "content": json.dumps({"say": first_response})})
                            new_messages.append({"role": "user", "content": f"工具 '{tool_name}' 的执行结果: {json.dumps(tool_result_serializable, ensure_ascii=False)}"})
                            
                            # 获取新的LLM响应
                            new_response = await self.get_response(new_messages)
                            if isinstance(new_response, dict) and "say" in new_response:
                                final_response = f"{first_response}\n{new_response['say']}"
                            else:
                                final_response = f"{first_response}\n{new_response}"
                        else:
                            final_response = f"{first_response}\n无法执行工具 '{tool_name}'。没有服务器能够处理此工具。"
                    
                    except Exception as e:
                        logging.error(f"处理工具调用失败: {e}")
                        final_response = f"{first_response}\n处理工具调用时出错: {str(e)}"
        else:
            # 非JSON响应，尝试解析为普通文本
            first_response = response_obj if isinstance(response_obj, str) else str(response_obj)
            final_response = first_response
        
        return final_response, tool_used, first_response
    
    def _convert_to_serializable(self, obj):
        """将对象转换为可序列化的格式
        
        Args:
            obj: 要转换的对象
            
        Returns:
            可序列化的对象（字典、列表等）
        """
        if hasattr(obj, '__dict__'):
            # 如果对象有__dict__属性，转换为字典
            return {k: self._convert_to_serializable(v) for k, v in obj.__dict__.items() 
                    if not k.startswith('_')}
        elif isinstance(obj, dict):
            # 递归处理字典中的值
            return {k: self._convert_to_serializable(v) for k, v in obj.items()}
        elif isinstance(obj, (list, tuple)):
            # 递归处理列表或元组中的值
            return [self._convert_to_serializable(item) for item in obj]
        elif isinstance(obj, (str, int, float, bool, type(None))):
            # 基本类型可以直接序列化
            return obj
        else:
            # 其他类型转换为字符串
            try:
                return str(obj)
            except:
                return "不可序列化的对象"


# 数据模型
class Message(BaseModel):
    role: str = Field(..., description="消息角色 (system/user/assistant)")
    content: str = Field(..., description="消息内容")
    
    @field_validator('role')
    @classmethod
    def validate_role(cls, v):
        if v not in ['system', 'user', 'assistant', 'function']:
            raise ValueError('角色必须是 system, user, assistant 或 function')
        return v


# 依赖项：确保服务器已初始化
async def get_initialized():
    """确保服务器已初始化"""
    global initialized, servers, tools_by_name, all_tools
    
    if not initialized:
        async with initialization_lock:
            if not initialized:
                try:
                    logging.info("初始化MCP服务器...")
                    # 加载服务器配置
                    config_file = os.getenv("CONFIG_FILE", "servers_config.json")
                    if os.path.exists(config_file):
                        servers_config = Configuration.load_config(config_file)
                        if "mcpServers" in servers_config:
                            # 初始化服务器
                            for server_name, server_config in servers_config["mcpServers"].items():
                                server = Server(server_name, server_config)
                                await server.initialize()
                                servers.append(server)
                                logging.info(f"服务器 {server_name} 初始化成功")
                                
                                # 获取工具列表
                                server_tools = await server.list_tools()
                                all_tools.extend(server_tools)
                                
                                for tool in server_tools:
                                    tools_by_name[tool.name] = tool
                                    logging.info(f"添加工具: {tool.name} (来自服务器 {server_name})")
                    
                    # 加载模型映射
                    load_model_mappings()
                    initialized = True
                    logging.info("MCP服务器初始化完成")
                except Exception as e:
                    logging.error(f"MCP服务器初始化失败: {e}")
                    raise HTTPException(status_code=500, detail=f"服务器初始化失败: {str(e)}")
    
    return initialized


# API路由
@app.get("/")
async def root():
    return {"message": "MCP API服务器正在运行"}


@app.get("/status")
async def status():
    """提供服务器状态信息"""
    return {
        "status": "ready" if initialized else "initializing",
        "servers_count": len(servers),
        "tools_count": len(all_tools),
        "default_model": DEFAULT_MODEL,
        "available_tools": [
            {"name": tool.name, "description": tool.description} 
            for tool in all_tools
        ] if initialized else [],
        "available_models": list(MODEL_MAPPINGS.keys()) if initialized else []
    }


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    x_api_key: str = Depends(verify_api_key)
):
    """处理聊天请求，包括工具调用"""
    try:
        # 确保服务器已初始化
        if not initialized:
            logging.warning("服务器尚未完成初始化，可能导致功能不正常")
        
        # 解析请求体
        body = await request.json()
        
        # 提取模型名称和消息
        model_name = body.get("model", DEFAULT_MODEL)
        messages = body.get("messages", [])
        stream = body.get("stream", False)
        
        # 创建DeepSeek客户端
        client = DeepSeekClient()
        
        # 转换消息格式
        processed_messages = [{"role": msg.get("role"), "content": msg.get("content")} for msg in messages]
        
        # 检查是否需要添加系统消息
        has_system_message = any(msg["role"] == "system" for msg in processed_messages)
        
        # 创建工具调用相关的系统提示
        tools_info = "\n".join([tool.format_for_llm() for tool in all_tools])
        tools_system_prompt = f"""
以下是你可以使用的工具:

{tools_info}

你必须始终以JSON格式回复，格式如下：

不使用工具时:
{{
  "say": "你的回复内容"
}}

需要使用工具时:
{{
  "say": "告诉用户你正在使用工具的信息",
  "use": {{
    "tool": "工具名称",
    "params": {{
      "参数1": "值1",
      "参数2": "值2"
    }}
  }}
}}

只有在需要调用工具时才包含"use"字段。确保JSON格式正确。
"""
        
        if has_system_message:
            # 找到系统消息并追加工具调用相关提示
            for msg in processed_messages:
                if msg["role"] == "system":
                    # 在原有系统提示后追加工具调用相关提示
                    msg["content"] = f"{msg['content']}\n\n{tools_system_prompt}"
                    break
        else:
            # 没有系统消息，添加新的系统消息
            system_prompt = f"系统指示：{tools_system_prompt}"
            processed_messages.insert(0, {"role": "system", "content": system_prompt})
        
        if stream:
            # 使用流式响应，但分段发送
            logging.info("流式请求：使用分段伪流模式")
            
            # 创建一个StreamingResponse对象，通过工具调用生成器产生响应
            return StreamingResponse(
                phased_streaming_response(client, processed_messages),
                media_type="text/event-stream"
            )
        else:
            # 非流式响应，直接执行带工具调用的请求
            final_response, tool_used, _ = await client.execute_with_tool(processed_messages)
            
            # 转换为OpenAI格式响应
            return {
                "id": f"chatcmpl-{id(final_response)}",
                "object": "chat.completion",
                "created": int(asyncio.get_event_loop().time()),
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": final_response
                        },
                        "finish_reason": "stop"
                    }
                ],
                "usage": {
                    "prompt_tokens": -1,  # 不提供真实token计数
                    "completion_tokens": -1,
                    "total_tokens": -1
                },
                "tool_used": tool_used
            }
    
    except Exception as e:
        logging.error(f"处理聊天请求时出错: {e}")
        raise HTTPException(status_code=500, detail=f"处理聊天请求失败: {str(e)}")


async def phased_streaming_response(client: DeepSeekClient, messages: List[Dict[str, str]]) -> AsyncGenerator[str, None]:
    """分阶段执行伪流式响应
    
    第一阶段: 发送初始响应
    第二阶段: 调用工具并发送后续响应
    
    Args:
        client: DeepSeek客户端实例
        messages: 消息列表
        
    Returns:
        流式响应生成器
    """
    try:
        # 生成一个统一的响应ID
        response_id = f"chatcmpl-{id(messages)}"
        
        # 第一阶段：获取初始AI响应
        response_obj = await client.get_response(messages)
        
        # 提取第一段响应内容
        first_response, tool_name, tool_params = _parse_initial_response(response_obj)
        
        # 伪流式返回第一段响应（不发送DONE）
        for chunk in generate_chunks(response_id, first_response, send_done=False):
            await asyncio.sleep(0.05)
            yield chunk
        
        # 如果需要调用工具
        if tool_name:
            # 调用工具并获取后续响应
            second_response = await _execute_tool_and_get_response(
                client, tool_name, tool_params, messages, first_response
            )
            
            # 伪流式返回第二段响应（发送DONE）
            for chunk in generate_chunks(response_id, second_response, send_done=True):
                await asyncio.sleep(0.02)
                yield chunk
        else:
            # 如果没有工具调用，发送结束标记
            yield "data: [DONE]\n\n"
    
    except Exception as e:
        logging.error(f"分阶段流式响应失败: {e}")
        # 发送一个错误消息给客户端
        error_chunk = {
            "id": f"chatcmpl-error",
            "object": "chat.completion.chunk",
            "created": int(asyncio.get_event_loop().time()),
            "model": "mcp-tool-enhanced",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": f"\n处理请求时出错: {str(e)}"},
                    "finish_reason": "stop"
                }
            ]
        }
        yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"


def _parse_initial_response(response_obj) -> Tuple[str, Optional[str], Dict[str, Any]]:
    """解析初始响应，提取文本内容和工具调用信息
    
    Args:
        response_obj: 响应对象
        
    Returns:
        (响应文本, 工具名称或None, 工具参数)
    """
    first_response = ""
    tool_name = None
    tool_params = {}
    
    if isinstance(response_obj, dict) and "say" in response_obj:
        first_response = response_obj["say"]
        
        # 检查是否包含工具调用
        if "use" in response_obj and isinstance(response_obj["use"], dict):
            tool_info = response_obj["use"]
            tool_name = tool_info.get("tool")
            tool_params = tool_info.get("params", {})
            logging.info(f"检测到工具调用: {tool_name}")
            first_response = f"{first_response}\n| 正在执行工具 '{tool_name}'...\n"
    else:
        # 非JSON响应，使用原始文本
        first_response = response_obj if isinstance(response_obj, str) else str(response_obj)
    
    return first_response, tool_name, tool_params


async def _execute_tool_and_get_response(
    client: DeepSeekClient, 
    tool_name: str, 
    tool_params: Dict[str, Any],
    messages: List[Dict[str, str]],
    first_response: str
) -> str:
    """执行工具并获取后续AI响应
    
    Args:
        client: DeepSeek客户端
        tool_name: 工具名称
        tool_params: 工具参数
        messages: 原始消息列表
        first_response: 初始响应
        
    Returns:
        后续响应文本
    """
    second_response = ""
    tool_used = False
    tool_result = None
    
    # 检查该工具是否在工具列表中
    target_tool = tools_by_name.get(tool_name)
    
    if target_tool:
        # 首先尝试在提供该工具的服务器上执行
        target_server_name = target_tool.server_name
        
        if target_server_name:
            # 找到目标服务器
            target_server = next((s for s in servers if s.name == target_server_name), None)
            
            if target_server:
                try:
                    logging.info(f"在指定服务器 {target_server_name} 上执行工具 {tool_name}...")
                    tool_result = await target_server.execute_tool(tool_name, tool_params)
                    tool_used = True
                    logging.info(f"工具 {tool_name} 在服务器 {target_server_name} 上执行成功")
                except Exception as e:
                    logging.warning(f"在服务器 {target_server_name} 上执行工具 {tool_name} 失败: {e}")
    
    # 如果尚未成功执行，尝试在所有服务器上执行
    if not tool_used:
        logging.info(f"在所有可用服务器上尝试执行工具 {tool_name}...")
        for server in servers:
            try:
                logging.info(f"尝试在服务器 {server.name} 上执行工具 {tool_name}...")
                tool_result = await server.execute_tool(tool_name, tool_params)
                tool_used = True
                logging.info(f"工具 {tool_name} 在服务器 {server.name} 上执行成功")
                break
            except Exception as e:
                logging.warning(f"在服务器 {server.name} 上执行工具 {tool_name} 失败: {e}")
                continue
    
    # 获取后续AI响应
    if tool_used and tool_result is not None:
        # 将工具结果转换为可序列化格式
        tool_result_serializable = client._convert_to_serializable(tool_result)
        
        # 将工具结果添加到消息中
        new_messages = messages.copy()
        new_messages.append({"role": "assistant", "content": json.dumps({"say": first_response})})
        new_messages.append({"role": "user", "content": f"工具 '{tool_name}' 的执行结果: {json.dumps(tool_result_serializable, ensure_ascii=False)}"})
        
        # 获取新的LLM响应
        new_response = await client.get_response(new_messages)
        if isinstance(new_response, dict) and "say" in new_response:
            second_response = new_response['say']
        else:
            second_response = str(new_response)
    else:
        # 工具调用失败
        second_response = f"无法执行工具 '{tool_name}'。没有服务器能够处理此工具。"
    
    return second_response


def generate_chunks(response_id: str, content: str, send_done: bool = True) -> List[str]:
    """将内容分割成SSE块
    
    Args:
        response_id: 响应ID
        content: 要发送的内容
        send_done: 是否发送[DONE]标记
        
    Returns:
        SSE块列表
    """
    # 按字符分块发送（模拟真实的流式响应）
    chunk_size = 4  # 每次发送4个字符
    chunks = []
    
    # 分块处理内容
    for i in range(0, len(content), chunk_size):
        chunk_text = content[i:i+chunk_size]
        finish_reason = "stop" if i + chunk_size >= len(content) and send_done else None
        
        # 创建响应块
        response_chunk = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": int(asyncio.get_event_loop().time()),
            "model": "mcp-tool-enhanced",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": chunk_text},
                    "finish_reason": finish_reason
                }
            ]
        }
        
        # 序列化为JSON并添加SSE格式前缀
        chunks.append(f"data: {json.dumps(response_chunk, ensure_ascii=False)}\n\n")
    
    # 如果需要发送结束标记
    if send_done:
        chunks.append("data: [DONE]\n\n")
    
    return chunks


# 添加启动事件
@app.on_event("startup")
async def startup_event():
    """服务器启动时执行初始化操作"""
    logging.info("API服务器启动，开始初始化MCP服务器...")
    await get_initialized()
    
    # 显示初始化状态
    tools_count = len(all_tools)
    servers_count = len(servers)
    if initialized:
        logging.info(f"==== MCP服务器初始化成功 ====")
        logging.info(f"已加载 {servers_count} 个服务器")
        logging.info(f"已注册 {tools_count} 个工具")
        logging.info(f"默认模型: {DEFAULT_MODEL}")
        logging.info("===========================")
    else:
        logging.warning("==== MCP服务器初始化失败 ====")


@app.on_event("shutdown")
async def shutdown_event():
    """清理资源"""
    logging.info("正在关闭API服务器...")
    for server in servers:
        try:
            await server.cleanup()
        except Exception as e:
            logging.error(f"清理服务器 {server.name} 时出错: {e}")


def start():
    """启动API服务器"""
    # 加载配置
    port = int(os.getenv("API_PORT", "8080"))
    host = os.getenv("API_HOST", "0.0.0.0")
    
    # 启动服务器
    logging.info(f"启动API服务器于 {host}:{port}")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    start() 
