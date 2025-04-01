import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client

# 配置日志
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


async def check_and_install_dependency(package_name: str) -> bool:
    """检查NPM包是否已安装，如果未安装则尝试安装
    
    Args:
        package_name: 需要检查的NPM包名称
        
    Returns:
        安装成功返回True，失败返回False
    """
    logging.info(f"检查依赖包 {package_name} 是否已安装...")
    
    # 检查npx命令是否存在
    npx_cmd = shutil.which("npx")
    if not npx_cmd:
        logging.error("未找到npx命令，请先安装Node.js和npm")
        return False
    
    # 检查包是否已安装
    try:
        check_cmd = f"{npx_cmd} --no {package_name} --version"
        result = subprocess.run(
            check_cmd, 
            shell=True, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE, 
            text=True,
            timeout=10
        )
        
        # 如果退出代码为0，表示包已安装
        if result.returncode == 0:
            logging.info(f"依赖包 {package_name} 已安装")
            return True
    except Exception as e:
        logging.warning(f"检查依赖包时出错: {e}")
    
    # 如果到这里，说明包未安装或检查失败，尝试安装
    logging.info(f"正在安装依赖包 {package_name}...")
    try:
        install_cmd = f"{npx_cmd} -y {package_name} --quiet"
        result = subprocess.run(
            install_cmd, 
            shell=True, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE, 
            text=True,
            timeout=180  # 安装可能需要较长时间
        )
        
        if result.returncode == 0:
            logging.info(f"成功安装依赖包 {package_name}")
            return True
        else:
            logging.error(f"安装依赖包 {package_name} 失败: {result.stderr}")
            return False
    except Exception as e:
        logging.error(f"安装依赖包过程中出错: {e}")
        return False


class Configuration:
    """管理MCP客户端的配置和环境变量"""

    def __init__(self) -> None:
        """初始化配置和环境变量"""
        self.load_env()
    
    @staticmethod
    def load_env() -> None:
        """从.env文件加载环境变量"""
        load_dotenv()

    @staticmethod
    def load_config(file_path: str) -> Dict[str, Any]:
        """从JSON文件加载服务器配置
        
        参数:
            file_path: JSON配置文件的路径
            
        返回:
            包含服务器配置的字典
            
        异常:
            FileNotFoundError: 如果配置文件不存在
            JSONDecodeError: 如果配置文件不是有效的JSON
        """
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)


class Server:
    """管理MCP服务器连接和工具执行"""

    def __init__(self, name: str, config: Dict[str, Any]) -> None:
        """初始化服务器连接
        
        参数:
            name: 服务器名称
            config: 服务器配置
        """
        self.name: str = name
        self.config: Dict[str, Any] = config
        self.stdio_context: Optional[Any] = None
        self.session: Optional[ClientSession] = None
        self._cleanup_lock: asyncio.Lock = asyncio.Lock()
        self.exit_stack: AsyncExitStack = AsyncExitStack()
        
        # 设置依赖信息 - 支持明确指定所需依赖
        self.dependencies: List[str] = config.get("dependencies", [])
        if "command" in config and config["command"] == "npx":
            # 添加从命令行参数中推断的依赖
            args = config.get("args", [])
            if args:
                if args[0] == "-y" and len(args) > 1:
                    inferred_dependency = args[1]
                else:
                    inferred_dependency = args[0]
                
                # 如果依赖还没有在列表中，则添加
                if inferred_dependency and inferred_dependency not in self.dependencies:
                    self.dependencies.append(inferred_dependency)

    async def initialize(self) -> None:
        """初始化服务器连接"""
        # 检查是否为SSE服务器
        if "url" in self.config:
            # SSE服务器连接
            url = self.config["url"]
            headers = self.config.get("headers", {})
            timeout = self.config.get("timeout", 5)
            sse_read_timeout = self.config.get("sse_read_timeout", 300)  # 5分钟
            
            try:
                logging.info(f"初始化SSE服务器 {self.name}，连接到 {url}")
                transport = await self.exit_stack.enter_async_context(
                    sse_client(
                        url, headers=headers, timeout=timeout, 
                        sse_read_timeout=sse_read_timeout
                    )
                )
                read, write = transport
                session = await self.exit_stack.enter_async_context(
                    ClientSession(read, write)
                )
                await session.initialize()
                self.session = session
                logging.info(f"SSE服务器 {self.name} 初始化成功")
            except Exception as e:
                logging.error(f"初始化SSE服务器 {self.name} 时出错: {e}")
                await self.cleanup()
                raise
        else:
            # 标准stdio服务器连接
            command = self.config["command"]
            
            # 对于npx命令，检查并安装依赖
            if command == "npx" and self.dependencies:
                logging.info(f"检查服务器 {self.name} 所需的依赖项...")
                
                # 检查并安装所有依赖
                for dependency in self.dependencies:
                    # 解析依赖名称和版本要求
                    if "@" in dependency:
                        package_name = dependency.split("@")[0]
                        version_spec = dependency
                    else:
                        package_name = dependency
                        version_spec = dependency
                    
                    # 检查包是否存在，不存在则安装
                    dependency_installed = await check_and_install_dependency(version_spec)
                    if not dependency_installed:
                        error_msg = f"无法安装所需依赖 {version_spec}，服务器 {self.name} 初始化失败"
                        logging.error(error_msg)
                        await self.cleanup()
                        raise ValueError(error_msg)
                
                logging.info(f"服务器 {self.name} 所有依赖项检查完成")
            
            # 查找可执行命令的完整路径
            cmd_path = (
                shutil.which("npx")
                if command == "npx"
                else shutil.which(command)
            )
            
            if cmd_path is None:
                error_msg = f"命令 '{command}' 未找到，请确保已安装"
                logging.error(error_msg)
                raise ValueError(error_msg)

            # 构建服务器参数
            server_params = StdioServerParameters(
                command=cmd_path,
                args=self.config["args"],
                env={**os.environ, **self.config["env"]}
                if self.config.get("env")
                else None,
            )
            
            try:
                logging.info(f"初始化stdio服务器 {self.name}")
                stdio_transport = await self.exit_stack.enter_async_context(
                    stdio_client(server_params)
                )
                read, write = stdio_transport
                session = await self.exit_stack.enter_async_context(
                    ClientSession(read, write)
                )
                await session.initialize()
                self.session = session
                logging.info(f"stdio服务器 {self.name} 初始化成功")
            except Exception as e:
                logging.error(f"初始化stdio服务器 {self.name} 时出错: {e}")
                await self.cleanup()
                raise

    async def list_tools(self) -> List[Any]:
        """列出服务器可用的工具
        
        返回:
            可用工具列表
            
        异常:
            RuntimeError: 如果服务器未初始化
        """
        if not self.session:
            raise RuntimeError(f"服务器 {self.name} 未初始化")

        tools_response = await self.session.list_tools()
        tools = []

        for item in tools_response:
            if isinstance(item, tuple) and item[0] == "tools":
                for tool in item[1]:
                    tools.append(Tool(tool.name, tool.description, tool.inputSchema, self.name))

        return tools

    async def execute_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        retries: int = 2,
        delay: float = 1.0,
    ) -> Any:
        """执行工具，并支持重试机制
        
        参数:
            tool_name: 要执行的工具名称
            arguments: 工具参数
            retries: 重试次数
            delay: 重试之间的延迟（秒）
            
        返回:
            工具执行结果
            
        异常:
            RuntimeError: 如果服务器未初始化
            Exception: 如果在所有重试后工具执行失败
        """
        if not self.session:
            raise RuntimeError(f"服务器 {self.name} 未初始化")

        attempt = 0
        while attempt < retries:
            try:
                logging.info(f"执行工具 {tool_name}...")
                result = await self.session.call_tool(tool_name, arguments)
                return result

            except Exception as e:
                attempt += 1
                logging.warning(
                    f"执行工具时出错: {e}。尝试 {attempt}/{retries}。"
                )
                if attempt < retries:
                    logging.info(f"{delay} 秒后重试...")
                    await asyncio.sleep(delay)
                else:
                    logging.error("达到最大重试次数。失败。")
                    raise

    async def cleanup(self) -> None:
        """清理服务器资源"""
        async with self._cleanup_lock:
            try:
                await self.exit_stack.aclose()
                self.session = None
                self.stdio_context = None
                logging.info(f"服务器 {self.name} 清理完成")
            except Exception as e:
                logging.error(f"清理服务器 {self.name} 时出错: {e}")


class Tool:
    """表示带有属性和格式化的工具"""

    def __init__(
        self, name: str, description: str, input_schema: Dict[str, Any], server_name: str = ""
    ) -> None:
        """初始化工具
        
        参数:
            name: 工具名称
            description: 工具描述
            input_schema: 工具输入模式
            server_name: 提供该工具的服务器名称
        """
        self.name: str = name
        self.description: str = description
        self.input_schema: Dict[str, Any] = input_schema
        self.server_name: str = server_name  # 记录工具来自哪个服务器

    def format_for_llm(self) -> str:
        """为LLM格式化工具信息
        
        返回:
            工具的格式化描述
        """
        # 提取参数信息
        params = ""
        if (
            isinstance(self.input_schema, dict)
            and "properties" in self.input_schema
            and isinstance(self.input_schema["properties"], dict)
        ):
            for param_name, param_info in self.input_schema["properties"].items():
                param_type = param_info.get("type", "unknown")
                param_desc = param_info.get("description", "无描述")
                params += f"\n  - {param_name} ({param_type}): {param_desc}"

        # 格式化工具信息
        return f"工具名称: {self.name}\n描述: {self.description}\n参数:{params}" 