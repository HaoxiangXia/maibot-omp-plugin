"""OMP Agent 插件 — 基于 maibot-plugin-sdk

为麦麦提供调用宿主机 OMP (AI 编程与系统 Harness) 的能力，
支持通过用户前缀命令（@Command /omp）直接交互与任务调度。
内置 Bubblewrap (bwrap) 沙盒隔离：仅 data/omp_workspace/ 目录可写，全盘强制只读。
全面基于 Ephemeral RPC-UI 流式双向交互协议驱动，支持 /omp stop 优雅中断、/omp a <答复> 决策注入、
/omp resume 续写会话（严格互斥保护），以及轻量 OMP 原生扩展 Socket 外部会话毫秒级监听。
除即时启动回执与最终结果交付外，其余状态与系统信息全部采用 QQ 合并转发卡片形式发送。
"""

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase
from pathlib import Path
from pydantic import model_validator
from typing import Any, Dict, List, Optional, Tuple

import asyncio
import base64
import datetime
import json
import logging
import os
import re
import shutil
import time

logger = logging.getLogger("maibot.plugins.omp_agent")

_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _clean_omp_output(raw_text: str) -> str:
    """清洗 OMP 输出中的 ANSI 转义序列与终端进度提示。

    Args:
        raw_text: OMP 进程的标准输出或标准错误文本。

    Returns:
        str: 过滤整理后的纯文本内容。
    """
    cleaned = _ANSI_ESCAPE_RE.sub("", raw_text).replace("\r", "")
    lines = cleaned.splitlines()
    filtered_lines: List[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped in {"Working...", "Thinking...", "Starting...", "Done."}:
            continue
        filtered_lines.append(line)
    return "\n".join(filtered_lines).strip()


class PluginSectionConfig(PluginConfigBase):
    """插件基础设置。"""

    __ui_label__ = "基础设置"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用 OMP 插件功能",
        json_schema_extra={
            "label": "启用插件",
            "hint": "总开关：关闭后将停用所有 /omp 相关指令与工具调用",
        },
    )
    config_version: str = Field(
        default="1.2.0",
        description="配置版本号",
        json_schema_extra={"label": "配置版本", "hidden": True},
    )


class RuntimeSectionConfig(PluginConfigBase):
    """OMP 核心运行环境与模型设置。"""

    __ui_label__ = "运行环境与模型"
    __ui_icon__ = "terminal"
    __ui_order__ = 1

    omp_path: str = Field(
        default="/home/xiang/.bun/bin/omp",
        description="宿主机 OMP 可执行文件绝对路径",
        json_schema_extra={
            "label": "OMP 程序路径",
            "placeholder": "/home/xiang/.bun/bin/omp",
            "hint": "宿主机 OMP 真实二进制路径，留空将尝试从 PATH 环境变量中搜索",
        },
    )
    default_model: str = Field(
        default="gemini-3.8-flash",
        description="指定默认使用的执行模型名称",
        json_schema_extra={
            "label": "默认执行模型",
            "placeholder": "gemini-3.8-flash",
            "hint": "可选推荐: gemini-3.8-flash, gpt-4o, claude-3-7-sonnet 等（留空使用 OMP 自带配置）",
        },
    )
    default_cwd: str = Field(
        default="/home/xiang/MaiBot/data/omp_workspace",
        description="沙盒内唯一挂载的可写工作空间目录",
        json_schema_extra={
            "label": "沙盒工作目录",
            "placeholder": "/home/xiang/MaiBot/data/omp_workspace",
            "hint": "AI 在沙盒中执行任务、读写与产出代码的专属工作区",
        },
    )


class SecuritySectionConfig(PluginConfigBase):
    """Bubblewrap 内核沙盒安全隔离与网络代理设置。"""

    __ui_label__ = "沙盒安全与网络代理"
    __ui_icon__ = "shield"
    __ui_order__ = 2

    use_sandbox: bool = Field(
        default=True,
        description="是否启用 Bubblewrap 内核沙盒强隔离",
        json_schema_extra={
            "label": "沙盒隔离保护",
            "hint": "开启后全盘只读保护，高危凭据目录（~/.ssh, ~/.gnupg）自动挂载盲区遮蔽",
        },
    )
    bwrap_path: str = Field(
        default="/usr/bin/bwrap",
        description="Bubblewrap (bwrap) 可执行文件绝对路径",
        json_schema_extra={
            "label": "bwrap 程序路径",
            "placeholder": "/usr/bin/bwrap",
            "hint": "宿主机 bwrap 真实路径，留空将尝试从 PATH 中自动搜索",
        },
    )
    proxy_url: str = Field(
        default="http://127.0.0.1:7897",
        description="网络代理地址（免 TUN 模式直连），留空则不注入代理变量",
        json_schema_extra={
            "label": "网络代理地址",
            "placeholder": "http://127.0.0.1:7897",
            "hint": "用于沙盒内直连 Gemini/OpenAI，支持 HTTP/SOCKS 协议",
        },
    )


class ExecutionSectionConfig(PluginConfigBase):
    """任务执行、超时与双向交互策略设置。"""

    __ui_label__ = "任务执行与超时策略"
    __ui_icon__ = "clock"
    __ui_order__ = 3

    auto_approve: bool = Field(
        default=True,
        description="是否免交互自动授权工具调用（--auto-approve）",
        json_schema_extra={
            "label": "自动授权工具调用",
            "hint": "推荐开启：允许 OMP 自动执行读写与命令；关闭则要求所有工具必须手动确认",
        },
    )
    save_session: bool = Field(
        default=True,
        description="是否保存会话记录，以支持 /omp resume 与宿主机终端继续对话",
        json_schema_extra={
            "label": "持久化保存会话",
            "hint": "开启后会话落盘保存，可使用 /omp resume 继续上下文或终端 omp -r 恢复",
        },
    )
    tool_hang_timeout_seconds: int = Field(
        default=300,
        description="单个工具调用最大等待时间（秒），超过此时间未完成判定为工具卡死并主动 abort",
        json_schema_extra={
            "label": "单工具卡死超时（秒）",
            "placeholder": "300",
            "hint": "单个命令或工具执行超过该时长无结果时，判定为执行卡死并主动中止",
        },
    )
    idle_timeout_seconds: int = Field(
        default=300,
        description="全协议数据流最大静默时间（秒），超过此时间无任何思考或工具事件判定为通道/推理死锁并主动 abort",
        json_schema_extra={
            "label": "协议静默死锁超时（秒）",
            "placeholder": "300",
            "hint": "连续无任何数据流入时判定为网络或模型推理死锁并主动中止，默认 300 秒 (5 分钟)",
        },
    )
    max_total_seconds: int = Field(
        default=3600,
        description="单任务最大允许执行总时长（秒，默认 1 小时），用于极端模型死循环熔断兜底",
        json_schema_extra={
            "label": "单任务总时长熔断上限（秒）",
            "placeholder": "3600",
            "hint": "正常任务持续有动作不打断；超过此绝对上限触发防死循环熔断保护",
        },
    )
    max_tool_calls: int = Field(
        default=100,
        description="单任务最大调用工具总次数，用于防死循环熔断兜底",
        json_schema_extra={
            "label": "最大工具调用次数上限",
            "placeholder": "100",
            "hint": "防止模型在同一错误中无限循环调用工具",
        },
    )
    ask_timeout_seconds: int = Field(
        default=120,
        description="遇到 ask 工具决策确认时的等待超时时间（秒），超时将主动 abort 任务",
        json_schema_extra={
            "label": "决策交互超时（秒）",
            "placeholder": "120",
            "hint": "等待发起人回复决策答复的最大时长，超时自动下发 abort 并告警",
        },
    )
    progress_notify_seconds: int = Field(
        default=300,
        description="任务超过该时长未结束时主动发送进度提醒（秒），默认 300 秒（5分钟）",
        json_schema_extra={
            "label": "进展报告周期（秒）",
            "placeholder": "300",
            "hint": "长任务执行中，每隔该时长以合并转发卡片发送当前具体动作与进度战报，默认 300 秒 (5 分钟)",
        },
    )
    timeout_seconds: int = Field(
        default=3600,
        description="向后兼容旧版字段",
        json_schema_extra={"hidden": True},
    )
    max_reply_length: int = Field(
        default=1500,
        description="直接消息回复的最大文本长度，超出部分将被截断并提示导出网页查看",
        json_schema_extra={
            "label": "回复文本长度上限",
            "placeholder": "1500",
            "hint": "防止过长代码与分析结果刷屏",
        },
    )
    export_html: bool = Field(
        default=False,
        description="每次任务完成后是否自动调用 --export 生成 HTML 交互网页并上传",
        json_schema_extra={
            "label": "自动导出 HTML 网页",
            "hint": "默认关闭：仅在执行 /omp export 时按需生成并发送，避免刷屏",
        },
    )


class NotificationSectionConfig(PluginConfigBase):
    """外部 OMP 任务监听与事件推送设置。"""

    __ui_label__ = "外部任务监听与通知"
    __ui_icon__ = "bell"
    __ui_order__ = 4

    watch_external_sessions: bool = Field(
        default=True,
        description="是否在后台监听非 Bot 启动的外部 OMP 任务并在需要注意时推送",
        json_schema_extra={
            "label": "监听外部 OMP 任务",
            "hint": "通过原生扩展毫秒级感知宿主机终端执行的 OMP 任务",
        },
    )
    bridge_socket_path: str = Field(
        default="/tmp/maibot-omp-bridge.sock",
        description="用于接收 OMP 外部扩展事件推送的 Unix Domain Socket 绝对路径",
        json_schema_extra={
            "label": "IPC 通信 Socket 路径",
            "placeholder": "/tmp/maibot-omp-bridge.sock",
            "hint": "OMP 扩展通过该本地套接字向 MaiBot 汇报生命周期与交互事件",
        },
    )
    notify_user_id: str = Field(
        default="214953496",
        description="接收外部任务提醒的目标 QQ 号（私聊推送），留空则不推送",
        json_schema_extra={
            "label": "通知接收人 QQ",
            "placeholder": "214953496",
            "hint": "仅用于接收外部 OMP 的关键状态私聊提醒（ask 确认、报错、完成）",
        },
    )
    export_external_html: bool = Field(
        default=False,
        description="外部任务完成后是否自动导出 HTML 网页文件并私聊推送",
        json_schema_extra={
            "label": "外部任务自动导出 HTML",
            "hint": "默认关闭：外部任务完成时仅推送简明纯文本通知",
        },
    )


class OmpPluginConfig(PluginConfigBase):
    """OMP Agent 插件完整配置（5 模块卡片化规范）。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    runtime: RuntimeSectionConfig = Field(default_factory=RuntimeSectionConfig)
    security: SecuritySectionConfig = Field(default_factory=SecuritySectionConfig)
    execution: ExecutionSectionConfig = Field(default_factory=ExecutionSectionConfig)
    notification: NotificationSectionConfig = Field(default_factory=NotificationSectionConfig)

    @model_validator(mode="before")
    @classmethod
    def _migrate_flat_config(cls, data: Any) -> Any:
        """平滑迁移老旧扁平配置至分节嵌套结构。"""
        if not isinstance(data, dict):
            return data
        if any(k in data for k in ["plugin", "runtime", "security", "execution", "notification"]):
            return data

        omp = data.get("omp", {}) if isinstance(data.get("omp"), dict) else {}
        for section in ["plugin", "runtime", "security", "execution", "notification"]:
            data.setdefault(section, {})

        if "enabled" in data:
            data["plugin"].setdefault("enabled", data["enabled"])
        if "enabled" in omp:
            data["plugin"].setdefault("enabled", omp["enabled"])

        for k in ["omp_path", "default_model", "default_cwd"]:
            if k in omp:
                data["runtime"].setdefault(k, omp[k])
        for k in ["use_sandbox", "bwrap_path", "proxy_url"]:
            if k in omp:
                data["security"].setdefault(k, omp[k])
        for k in [
            "auto_approve",
            "save_session",
            "timeout_seconds",
            "tool_hang_timeout_seconds",
            "idle_timeout_seconds",
            "max_total_seconds",
            "max_tool_calls",
            "ask_timeout_seconds",
            "progress_notify_seconds",
            "max_reply_length",
            "export_html",
        ]:
            if k in omp:
                data["execution"].setdefault(k, omp[k])
            if k in omp:
                data["notification"].setdefault(k, omp[k])
        return data

    @property
    def omp(self) -> Any:
        """向后兼容属性代理视图。"""
        class _LegacyOmpView:
            def __init__(self, root: "OmpPluginConfig") -> None:
                self._root = root

            @property
            def enabled(self) -> bool:
                return self._root.plugin.enabled

            @property
            def omp_path(self) -> str:
                return self._root.runtime.omp_path

            @property
            def default_model(self) -> str:
                return self._root.runtime.default_model

            @property
            def default_cwd(self) -> str:
                return self._root.runtime.default_cwd

            @property
            def use_sandbox(self) -> bool:
                return self._root.security.use_sandbox

            @property
            def bwrap_path(self) -> str:
                return self._root.security.bwrap_path

            @property
            def proxy_url(self) -> str:
                return self._root.security.proxy_url

            @property
            def auto_approve(self) -> bool:
                return self._root.execution.auto_approve

            @property
            def save_session(self) -> bool:
                return self._root.execution.save_session

            @property
            def timeout_seconds(self) -> int:
                return self._root.execution.timeout_seconds

            @property
            def tool_hang_timeout_seconds(self) -> int:
                return self._root.execution.tool_hang_timeout_seconds

            @property
            def idle_timeout_seconds(self) -> int:
                return self._root.execution.idle_timeout_seconds

            @property
            def max_total_seconds(self) -> int:
                return self._root.execution.max_total_seconds

            @property
            def max_tool_calls(self) -> int:
                return self._root.execution.max_tool_calls

            @property
            def ask_timeout_seconds(self) -> int:
                return self._root.execution.ask_timeout_seconds

            @property
            def progress_notify_seconds(self) -> int:
                return self._root.execution.progress_notify_seconds

            @property
            def max_reply_length(self) -> int:
                return self._root.execution.max_reply_length

            @property
            def export_html(self) -> bool:
                return self._root.execution.export_html

            @property
            def watch_external_sessions(self) -> bool:
                return self._root.notification.watch_external_sessions

            @property
            def bridge_socket_path(self) -> str:
                return self._root.notification.bridge_socket_path

            @property
            def notify_user_id(self) -> str:
                return self._root.notification.notify_user_id

            @property
            def export_external_html(self) -> bool:
                return self._root.notification.export_external_html

        return _LegacyOmpView(self)


class OmpAgentPlugin(MaiBotPlugin):
    """OMP Agent 插件核心实现类。"""

    config_model = OmpPluginConfig

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._default_config_instance: Optional[OmpPluginConfig] = None
        self._active_task: Optional[Dict[str, Any]] = None
        self._active_session_id: Optional[str] = None
        self._external_sessions: Dict[str, Dict[str, Any]] = {}
        self._ipc_server: Optional[asyncio.AbstractServer] = None
        self._ipc_task: Optional[asyncio.Task[None]] = None
        self._pending_ask_waiter: Optional[Dict[str, Any]] = None

    @property
    def effective_config(self) -> OmpPluginConfig:
        """获取当前有效的结构化配置。"""
        try:
            cfg = self.config
            if isinstance(cfg, OmpPluginConfig):
                return cfg
        except Exception:
            pass
        if self._default_config_instance is None:
            self._default_config_instance = OmpPluginConfig()
        return self._default_config_instance

    def _resolve_omp_binary(self) -> Optional[str]:
        """解析 OMP 二进制路径。"""
        cfg_path = self.effective_config.runtime.omp_path.strip()
        if cfg_path and os.path.isfile(cfg_path) and os.access(cfg_path, os.X_OK):
            return cfg_path
        which_path = shutil.which("omp")
        if which_path and os.path.isfile(which_path) and os.access(which_path, os.X_OK):
            return which_path
        bun_default = os.path.expanduser("~/.bun/bin/omp")
        if os.path.isfile(bun_default) and os.access(bun_default, os.X_OK):
            return bun_default
        return None

    def _resolve_bwrap_binary(self) -> Optional[str]:
        """解析 Bubblewrap (bwrap) 可执行文件路径。"""
        cfg_path = self.effective_config.security.bwrap_path.strip()
        if cfg_path and os.path.isfile(cfg_path) and os.access(cfg_path, os.X_OK):
            return cfg_path
        which_path = shutil.which("bwrap")
        if which_path and os.path.isfile(which_path) and os.access(which_path, os.X_OK):
            return which_path
        for candidate in ["/usr/bin/bwrap", "/bin/bwrap", "/usr/local/bin/bwrap"]:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None

    def _resolve_workspace_dir(self) -> Path:
        """解析并确保沙盒工作空间目录存在。"""
        cfg_cwd = self.effective_config.runtime.default_cwd.strip()
        if cfg_cwd:
            path = Path(cfg_cwd).expanduser().resolve()
        else:
            path = Path.cwd() / "data" / "omp_workspace"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _resolve_workspace_session_dir(self) -> Optional[Path]:
        """计算当前可写工作空间在 OMP 会话存储下的专用目录。"""
        workspace_path = self._resolve_workspace_dir()
        sessions_root = Path.home() / ".omp" / "agent" / "sessions"
        if not sessions_root.is_dir():
            return None
        ws_str = str(workspace_path.resolve())
        normalized_name = ws_str.replace("/", "-").replace("\\", "-")
        target_dir = sessions_root / normalized_name
        return target_dir if target_dir.is_dir() else None

    def _is_session_locked(self, session_id: str) -> bool:
        """检查指定会话当前是否被占用（跨环境强互斥锁定）。"""
        if not session_id:
            return False
        clean_id = session_id.strip()
        if self._active_session_id and (
            self._active_session_id.startswith(clean_id) or clean_id.startswith(self._active_session_id)
        ):
            return True
        for ext_id, info in self._external_sessions.items():
            if ext_id.startswith(clean_id) or clean_id.startswith(ext_id):
                if info.get("status") in {"working", "waiting_input"}:
                    return True
        return False

    def _deploy_extension_bridge(self) -> None:
        """自动部署或同步 OMP 轻量 Extension Bridge 脚本。"""
        target_dir = Path.home() / ".omp" / "agent" / "extensions"
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            target_file = target_dir / "maibot-bridge.ts"
            packaged_file = Path(__file__).parent / "maibot-bridge.ts"
            if packaged_file.is_file():
                src_content = packaged_file.read_text(encoding="utf-8")
                if not target_file.is_file() or target_file.read_text(encoding="utf-8") != src_content:
                    target_file.write_text(src_content, encoding="utf-8")
                    logger.info(f"已同步更新 OMP 扩展 Bridge 至 {target_file}")
            elif not target_file.is_file():
                logger.warning("未找到 packaged maibot-bridge.ts，尝试保留现有扩展配置")
        except Exception as exc:
            logger.warning(f"部署 OMP Extension Bridge 失败 (非阻塞): {exc}")

    async def _start_ipc_server(self) -> None:
        """启动 Unix Domain Socket 服务端以接收外部 OMP 事件推流。"""
        sock_path_str = self.effective_config.notification.bridge_socket_path.strip()
        if not sock_path_str:
            return
        sock_path = Path(sock_path_str)
        try:
            if sock_path.exists():
                sock_path.unlink()
            sock_path.parent.mkdir(parents=True, exist_ok=True)
            self._ipc_server = await asyncio.start_unix_server(
                self._handle_ipc_client, path=str(sock_path)
            )
            logger.info(f"OMP Extension IPC Server 已在 {sock_path} 启动监听")
        except Exception as exc:
            logger.error(f"启动 OMP Extension IPC 服务端失败: {exc}")
    @staticmethod
    def _extract_session_preview_and_status(session_path_str: str) -> Tuple[str, bool]:
        """从会话 JSONL 文件中提取最近一次助手输出的前 100 个字符及是否中止标志。"""
        try:
            p = Path(session_path_str)
            if not p.is_file():
                return "", False
            last_text = ""
            with p.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line_str = line.strip()
                    if not line_str:
                        continue
                    try:
                        d = json.loads(line_str)
                        if d.get("type") == "message":
                            msg = d.get("message", {})
                            if msg.get("role") == "assistant":
                                content = msg.get("content", [])
                                text_found = ""
                                if isinstance(content, list):
                                    for part in content:
                                        if isinstance(part, dict) and part.get("type") == "text":
                                            t = str(part.get("text", "")).strip()
                                            if t:
                                                text_found = t
                                elif isinstance(content, str) and content.strip():
                                    text_found = content.strip()

                                if text_found:
                                    last_text = text_found
                    except Exception:
                        pass
            clean = " ".join(last_text.split()).strip()
            is_aborted = (
                clean == "** Error: Request was aborted**"
                or clean == "Request was aborted"
                or clean.startswith("** Error:")
            )
            return clean[:100], is_aborted
        except Exception:
            return "", False

    @staticmethod
    def _extract_session_preview(session_path_str: str) -> str:
        text, _ = OmpAgentPlugin._extract_session_preview_and_status(session_path_str)
        return text


    async def _handle_ipc_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """处理来自 OMP Extension Bridge 的流式事件帧。"""
        try:
            while not reader.at_eof():
                line = await reader.readline()
                if not line:
                    break
                try:
                    payload = json.loads(line.decode("utf-8").strip())
                except Exception:
                    continue

                if payload.get("isBotChild") is True:
                    continue

                payload_cwd = str(payload.get("cwd", "")).strip()
                if payload_cwd:
                    try:
                        if Path(payload_cwd).resolve() == self._resolve_workspace_dir().resolve():
                            continue
                    except Exception:
                        pass

                event_type = str(payload.get("type", ""))
                session_id = str(payload.get("sessionId", "")).strip()
                if not session_id and payload.get("sessionPath"):
                    p_str = str(payload["sessionPath"])
                    stem = Path(p_str).stem
                    session_id = stem.split("_")[-1] if "_" in stem else stem

                if not session_id:
                    session_id = f"ext-{payload.get('pid', 'proc')}"
                notify_uid = str(self.effective_config.notification.notify_user_id or "").strip()

                if event_type in {"session_start", "agent_start"}:
                    self._external_sessions[session_id] = {
                        "status": "working",
                        "pid": payload.get("pid"),
                        "cwd": payload.get("cwd"),
                        "timestamp": payload.get("timestamp", time.time()),
                    }
                elif event_type == "ask_start":
                    self._external_sessions[session_id] = {
                        "status": "waiting_input",
                        "pid": payload.get("pid"),
                        "cwd": payload.get("cwd"),
                        "timestamp": payload.get("timestamp", time.time()),
                    }
                    if notify_uid and self.effective_config.notification.watch_external_sessions:
                        ask_input = payload.get("input", {})
                        prompt_txt = ""
                        if isinstance(ask_input, dict):
                            prompt_txt = str(ask_input.get("prompt") or ask_input.get("message") or "")
                        notice = (
                            f"🔔 【外部 OMP 人工介入提醒】\n"
                            f"宿主机终端会话 [{session_id[:8]}] 正在等待决策确认！\n"
                        )
                        if prompt_txt:
                            notice += f"• 提问内容: {prompt_txt[:100]}...\n"
                        notice += "👉 请返回宿主机终端查看并完成回复。"
                        sent = await self._safe_send_private_text(notice, notify_uid)
                        logger.info(f"外部 OMP 决策提醒推送至 {notify_uid}: 结果={sent}")
                elif event_type == "ask_end":
                    if session_id in self._external_sessions:
                        self._external_sessions[session_id]["status"] = "working"
                elif event_type == "agent_end":
                    status = str(payload.get("status", "completed")).lower()
                    error_msg = payload.get("error")
                    self._external_sessions.pop(session_id, None)

                    if notify_uid and self.effective_config.notification.watch_external_sessions:
                        preview_txt = str(payload.get("content") or "").strip()
                        session_aborted_by_meta = False
                        if payload.get("sessionPath"):
                            file_preview, session_aborted_by_meta = self._extract_session_preview_and_status(str(payload["sessionPath"]))
                            if not preview_txt:
                                preview_txt = file_preview

                        if len(preview_txt) > 100:
                            preview_txt = preview_txt[:100]

                        # 严格判定任务是否被中断取消：
                        is_aborted = (
                            status in {"aborted", "abort", "cancelled", "canceled"}
                            or str(error_msg or "").strip().lower() == "request was aborted"
                            or session_aborted_by_meta
                            or preview_txt.strip() in {"** Error: Request was aborted**", "Request was aborted"}
                            or preview_txt.strip().startswith("** Error:")
                        )
                        is_error = (status == "error" or bool(error_msg)) and not is_aborted

                        if is_aborted:
                            err_notice = (
                                "⚠️ 【外部 OMP 任务已中断/取消】\n"
                                f"会话 ID: {session_id[:8]}\n"
                                "• 状态: Request was aborted (任务已被取消或模型请求中断)\n"
                                f"• 建议在终端输入 omp -r {session_id[:8]} 继续或排查。"
                            )
                            sent = await self._safe_send_private_text(err_notice, notify_uid)
                            logger.info(f"外部 OMP 中断通知推送至 {notify_uid}: 结果={sent}")
                        elif is_error:
                            err_summary = str(error_msg or "未知异常")[:100]
                            err_notice = (
                                f"❌ 【外部 OMP 任务异常中断】\n"
                                f"会话 ID: {session_id[:8]}\n"
                                f"• 错误摘要: {err_summary}\n"
                                f"• 建议在终端输入 omp -r {session_id[:8]} 排查。"
                            )
                            sent = await self._safe_send_private_text(err_notice, notify_uid)
                            logger.info(f"外部 OMP 异常推送至 {notify_uid}: 结果={sent}")
                        else:
                            content_line = f"• 内容摘要: {preview_txt}...\n" if preview_txt else ""
                            succ_notice = (
                                f"✅ 【外部 OMP 任务执行完成】\n"
                                f"会话 ID: {session_id[:8]}\n"
                                f"{content_line}"
                                f"• 运行状态: 已顺利完成并落盘\n"
                                f"📌 终端恢复: omp -r {session_id[:8]}"
                            )
                            sent = await self._safe_send_private_text(succ_notice, notify_uid)
                            logger.info(f"外部 OMP 完成通知推送至 {notify_uid}: 结果={sent}")
        except Exception as exc:
            logger.debug(f"IPC Client 处理异常: {exc}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _safe_send_text(self, text: str, stream_id: str) -> bool:
        """向指定聊天流安全发送单条普通文本消息。"""
        if not stream_id or not text or not hasattr(self, "_ctx") or self._ctx is None:
            return False
        try:
            return bool(await self.ctx.send.text(text, stream_id))
        except Exception as exc:
            logger.error(f"发送文本消息失败 (stream_id={stream_id}): {exc}")
            return False

    def _resolve_private_stream_id(self, user_id: str) -> Optional[str]:
        """查询数据库获取目标 QQ 用户已存在的真实私聊 stream_id。"""
        clean_uid = str(user_id or "").strip()
        if not clean_uid:
            return None
        db_path = Path("data") / "MaiBot.db"
        if db_path.is_file():
            try:
                import sqlite3
                with sqlite3.connect(db_path) as con:
                    cur = con.cursor()
                    cur.execute(
                        "SELECT session_id FROM mai_messages WHERE user_id=? AND (group_id IS NULL OR group_id='') ORDER BY id DESC LIMIT 1",
                        (clean_uid,),
                    )
                    row = cur.fetchone()
                    if row and row[0]:
                        return str(row[0])
            except Exception as exc:
                logger.debug(f"查询私聊历史 session_id 失败: {exc}")
        return None

    async def _safe_send_private_text(self, text: str, user_id: str) -> bool:
        """向指定 QQ 用户私聊发送单条文本消息。"""
        clean_uid = str(user_id or "").strip()
        if not clean_uid or not text or not hasattr(self, "_ctx") or self._ctx is None:
            return False

        # 策略 1: 优先查询与该用户已建立的真实私聊 stream_id 并通过核心接口发送
        real_stream = self._resolve_private_stream_id(clean_uid)
        if real_stream:
            try:
                sent = await self.ctx.send.text(text, real_stream)
                if sent:
                    return True
            except Exception as exc:
                logger.debug(f"通过真实私聊流发送失败 (stream_id={real_stream}): {exc}")

        # 策略 2: 调用 NapCat 适配器 send_private_msg 结构化消息段动作
        if clean_uid.isdigit():
            try:
                resp = await self.ctx.api.call(
                    "adapter.napcat.message.send_private_msg",
                    params={
                        "user_id": int(clean_uid),
                        "message": [{"type": "text", "data": {"text": text}}],
                    },
                )
                if isinstance(resp, dict) and (resp.get("status") in {"ok", "async"} or resp.get("retcode") == 0):
                    return True
            except Exception as exc:
                logger.debug(f"通过 NapCat 结构化发送私聊失败: {exc}")

            try:
                resp = await self.ctx.api.call(
                    "adapter.napcat.message.send_private_msg",
                    params={"user_id": int(clean_uid), "message": text},
                )
                if isinstance(resp, dict) and (resp.get("status") in {"ok", "async"} or resp.get("retcode") == 0):
                    return True
            except Exception as exc:
                logger.debug(f"通过 NapCat 文本模式发送私聊失败: {exc}")

        # 策略 3: 尝试备用私聊流名格式
        for candidate_stream in [f"qq_private_{clean_uid}", f"private_{clean_uid}", clean_uid]:
            try:
                res = await self.ctx.send.text(text, candidate_stream)
                if res:
                    return True
            except Exception:
                continue

        logger.warning(f"未能将私聊消息送达用户 {clean_uid}")
        return False

    async def _safe_send_forward(
        self,
        nodes_or_texts: List[str],
        stream_id: str,
        title: str = "OMP Agent 消息汇总",
    ) -> bool:
        """安全向指定聊天流发送 QQ 合并转发卡片。"""
        if not stream_id or not nodes_or_texts or not hasattr(self, "_ctx") or self._ctx is None:
            return False

        fallback_texts: List[str] = []
        forward_nodes: List[Dict[str, Any]] = []
        bot_qq = "2854196310"
        bot_name = "OMP Agent"

        for item in nodes_or_texts:
            content_str = str(item).strip()
            if not content_str:
                continue
            fallback_texts.append(content_str)
            forward_nodes.append(
                {
                    "type": "node",
                    "data": {
                        "name": bot_name,
                        "uin": bot_qq,
                        "content": content_str,
                    },
                }
            )

        if not forward_nodes:
            return False

        clean_stream = str(stream_id).strip()
        group_match = re.search(r"\b(\d{5,12})\b", clean_stream)
        group_id = int(group_match.group(1)) if group_match else None

        try:
            if group_id:
                resp = await self.ctx.api.call(
                    "adapter.napcat.message.send_group_forward_msg",
                    params={"group_id": group_id, "messages": forward_nodes},
                )
                if isinstance(resp, dict) and resp.get("success") is not False:
                    return True
        except Exception as exc:
            logger.debug(f"通过 NapCat 发送群合并转发失败: {exc}")

        fallback_body = f"📋 【{title}】\n\n" + "\n\n───────────────\n\n".join(fallback_texts)
        return await self._safe_send_text(fallback_body, stream_id)

    async def _safe_send_file(
        self,
        file_path: Path,
        stream_id: str,
        group_id: str = "",
        user_id: str = "",
    ) -> bool:
        """安全向指定聊天流发送产出文件。"""
        if not file_path.is_file():
            return False

        abs_file = str(file_path.resolve())
        file_name = file_path.name
        clean_grp = str(group_id or "").strip()
        clean_usr = str(user_id or "").strip()

        if hasattr(self, "_ctx") and self._ctx is not None:
            file_candidates: List[str] = []
            shared_dir = Path("/home/xiang/MaiBot/data/MaiMBot/exports")
            try:
                shared_dir.mkdir(parents=True, exist_ok=True)
                container_file = shared_dir / file_name
                shutil.copy2(file_path, container_file)
                file_candidates.append(f"/MaiMBot/data/exports/{file_name}")
            except Exception as copy_err:
                logger.debug(f"复制文件到容器共享目录失败: {copy_err}")

            file_candidates.append(abs_file)

            try:
                if file_path.stat().st_size <= 15 * 1024 * 1024:
                    raw_b64 = base64.b64encode(file_path.read_bytes()).decode("ascii")
                    file_candidates.append(f"base64://{raw_b64}")
            except Exception as b64_err:
                logger.debug(f"读取文件转 Base64 失败: {b64_err}")

            for ref in file_candidates:
                try:
                    if clean_grp and clean_grp.isdigit():
                        resp = await self.ctx.api.call(
                            "adapter.napcat.file.upload_group_file",
                            params={"group_id": int(clean_grp), "file": ref, "name": file_name},
                        )
                        if isinstance(resp, dict) and resp.get("success") is not False:
                            return True
                    elif clean_usr and clean_usr.isdigit():
                        resp = await self.ctx.api.call(
                            "adapter.napcat.file.upload_private_file",
                            params={"user_id": int(clean_usr), "file": ref, "name": file_name},
                        )
                        if isinstance(resp, dict) and resp.get("success") is not False:
                            return True
                except Exception as exc:
                    logger.debug(f"尝试使用引用上传文件失败: {exc}")

        return False

    async def _export_session_to_html(self, session_id: str) -> Optional[Path]:
        """使用 OMP 内置的 --export 功能将指定会话导出为独立 HTML 文件。"""
        omp_binary = self._resolve_omp_binary()
        if not omp_binary or not session_id:
            return None

        ws_dir = self._resolve_workspace_session_dir()
        matched_files = list(ws_dir.glob(f"*{session_id}*.jsonl")) if ws_dir and ws_dir.is_dir() else []
        if not matched_files:
            sessions_root = Path.home() / ".omp" / "agent" / "sessions"
            matched_files = list(sessions_root.glob(f"*/*{session_id}*.jsonl")) if sessions_root.is_dir() else []
        if not matched_files:
            return None

        session_jsonl = matched_files[0]
        try:
            proc = await asyncio.create_subprocess_exec(
                omp_binary,
                f"--export={session_jsonl}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=30.0)
            candidate_html = session_jsonl.with_suffix(".html")
            if candidate_html.is_file():
                return candidate_html
            expected_name = f"{session_jsonl.stem}.html"
            cur_html = Path.cwd() / expected_name
            if cur_html.is_file():
                return cur_html
        except Exception as exc:
            logger.error(f"导出 OMP 会话为 HTML 文件失败: {exc}")
        return None

    def _list_recent_sessions(self, limit: int = 5) -> List[Dict[str, Any]]:
        """从工作空间专有目录读取最近的 OMP 会话记录列表。"""
        target_dir = self._resolve_workspace_session_dir()
        if not target_dir or not target_dir.is_dir():
            return []

        jsonl_files = list(target_dir.glob("*.jsonl"))
        if not jsonl_files:
            return []

        jsonl_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        results: List[Dict[str, Any]] = []

        for p in jsonl_files[:limit]:
            stem = p.stem
            session_id = stem.split("_")[-1] if "_" in stem else stem
            mtime = p.stat().st_mtime
            dt_str = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")

            summary = ""
            try:
                with p.open("r", encoding="utf-8") as f:
                    for line in f:
                        data = json.loads(line)
                        if data.get("type") == "message" and data.get("message", {}).get("role") == "user":
                            content = data["message"].get("content", "")
                            if isinstance(content, str):
                                summary = content.strip().replace("\n", " ")
                            elif isinstance(content, list):
                                for part in content:
                                    if isinstance(part, dict) and part.get("type") == "text":
                                        summary = part.get("text", "").strip().replace("\n", " ")
                                        break
                            if summary:
                                break
            except Exception:
                summary = ""

            results.append({
                "session_id": session_id,
                "file_path": p,
                "updated_at": dt_str,
                "summary": summary,
            })

        return results

    def _match_session_id(self, key: str, sessions: List[Dict[str, Any]]) -> Optional[str]:
        """根据序号或前缀匹配会话 ID。"""
        clean_key = key.strip()
        if not clean_key:
            return None
        if clean_key.isdigit():
            idx = int(clean_key) - 1
            if 0 <= idx < len(sessions):
                return sessions[idx]["session_id"]
        for s in sessions:
            if s["session_id"].startswith(clean_key) or clean_key.startswith(s["session_id"]):
                return s["session_id"]
        return None

    def _snapshot_workspace(self, workspace_path: Path) -> Dict[str, float]:
        """获取当前工作空间内所有文件的相对路径和修改时间戳快照。"""
        snapshot: Dict[str, float] = {}
        if not workspace_path.is_dir():
            return snapshot
        try:
            for p in workspace_path.rglob("*"):
                if p.is_file():
                    rel = str(p.relative_to(workspace_path))
                    if any(part.startswith(".") for part in p.parts):
                        continue
                    snapshot[rel] = p.stat().st_mtime
        except Exception as exc:
            logger.debug(f"快照工作空间失败: {exc}")
        return snapshot

    def _detect_generated_files(
        self,
        workspace_path: Path,
        before_snapshot: Dict[str, float],
        start_time: float,
    ) -> List[Path]:
        """比对快照，识别本次 OMP 执行所新增或实质修改的文件。"""
        current_snapshot = self._snapshot_workspace(workspace_path)
        generated: List[Path] = []
        for rel_path, mtime in current_snapshot.items():
            if rel_path not in before_snapshot or mtime > before_snapshot[rel_path]:
                if mtime >= start_time - 1.0:
                    full_p = workspace_path / rel_path
                    if full_p.is_file() and full_p.stat().st_size > 0:
                        generated.append(full_p)
        generated.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return generated

    async def _run_omp_task(
        self,
        prompt: str,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        resume_session_id: Optional[str] = None,
    ) -> Tuple[bool, str, float, Optional[str], List[Path]]:
        """在 Bubblewrap 沙盒中通过 Ephemeral RPC-UI 流式双向驱动 OMP。"""
        omp_binary = self._resolve_omp_binary()
        if not omp_binary:
            return False, "未找到有效的 OMP 可执行文件路径", 0.0, None, []

        workspace_path = self._resolve_workspace_dir()
        use_sandbox = self.effective_config.security.use_sandbox
        bwrap_binary = self._resolve_bwrap_binary()
        selected_model = self.effective_config.runtime.default_model.strip()
        proxy_url = self.effective_config.security.proxy_url.strip()
        tool_hang_timeout_seconds = self.effective_config.execution.tool_hang_timeout_seconds
        idle_timeout_seconds = self.effective_config.execution.idle_timeout_seconds
        max_total_seconds = self.effective_config.execution.max_total_seconds
        max_tool_calls = self.effective_config.execution.max_tool_calls
        progress_notify_seconds = self.effective_config.execution.progress_notify_seconds
        ask_timeout_seconds = self.effective_config.execution.ask_timeout_seconds
        # 跨环境强互斥锁定检查
        if resume_session_id and self._is_session_locked(resume_session_id):
            return (
                False,
                f"会话 {resume_session_id[:8]} 当前正在执行中（外部终端或已有任务），同一时间不允许并发接入或打断。请等待其完成后再试。",
                0.0,
                None,
                [],
            )

        cmd: List[str] = []
        env = os.environ.copy()
        env["MAIBOT_OMP_INTERNAL"] = "1"
        proxy_env_keys: Tuple[str, ...] = (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "PI_PROXY",
        )
        if proxy_url:
            env["HTTP_PROXY"] = proxy_url
            env["HTTPS_PROXY"] = proxy_url
            env["ALL_PROXY"] = proxy_url
            env["http_proxy"] = proxy_url
            env["https_proxy"] = proxy_url
            env["all_proxy"] = proxy_url
            env["PI_PROXY"] = proxy_url
            env["NO_PROXY"] = "127.0.0.1,localhost,::1"
            env["no_proxy"] = "127.0.0.1,localhost,::1"
        else:
            # 未指定代理时主动清理继承自宿主环境的代理变量（包括 OMP 特有的 PI_PROXY），防止死代理导致网络被拒
            for key in proxy_env_keys:
                env.pop(key, None)

        if use_sandbox and bwrap_binary:
            cmd.extend([
                bwrap_binary,
                "--ro-bind", "/", "/",
                "--dev", "/dev",
                "--proc", "/proc",
                "--tmpfs", "/tmp",
            ])
            ssh_home = Path.home() / ".ssh"
            if ssh_home.is_dir():
                cmd.extend(["--tmpfs", str(ssh_home)])
            gnupg_home = Path.home() / ".gnupg"
            if gnupg_home.is_dir():
                cmd.extend(["--tmpfs", str(gnupg_home)])

            omp_home = Path.home() / ".omp"
            if omp_home.is_dir():
                cmd.extend(["--bind", str(omp_home), str(omp_home)])

            cmd.extend(["--bind", str(workspace_path), str(workspace_path)])
            cmd.extend(["--chdir", str(workspace_path)])

            if proxy_url:
                cmd.extend([
                    "--setenv", "HTTP_PROXY", proxy_url,
                    "--setenv", "HTTPS_PROXY", proxy_url,
                    "--setenv", "http_proxy", proxy_url,
                    "--setenv", "https_proxy", proxy_url,
                    "--setenv", "ALL_PROXY", proxy_url,
                    "--setenv", "all_proxy", proxy_url,
                    "--setenv", "PI_PROXY", proxy_url,
                    "--setenv", "NO_PROXY", "127.0.0.1,localhost,::1",
                    "--setenv", "no_proxy", "127.0.0.1,localhost,::1",
                ])
            else:
                for key in proxy_env_keys:
                    cmd.extend(["--unsetenv", key])
            cmd.extend(["--setenv", "MAIBOT_OMP_INTERNAL", "1"])
            cmd.extend([omp_binary, "--mode", "rpc-ui"])
        else:
            cmd.extend([omp_binary, "--mode", "rpc-ui"])

        if not self.effective_config.execution.save_session:
            cmd.append("--no-session")
        if self.effective_config.execution.auto_approve:
            cmd.append("--auto-approve")
        if selected_model:
            cmd.append(f"--model={selected_model}")
        if resume_session_id:
            cmd.append(f"--resume={resume_session_id}")
        cmd.append(f"--cwd={workspace_path}")

        start_monotonic = time.monotonic()
        start_wall_time = time.time()
        before_snapshot = self._snapshot_workspace(workspace_path)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workspace_path),
                env=env,
                limit=64 * 1024 * 1024,
            )
        except Exception as exc:
            elapsed = time.monotonic() - start_monotonic
            return False, f"启动 OMP RPC 子进程失败: {exc}", elapsed, None, []

        progress_tracker: Dict[str, Any] = {
            "tool_counts": {},
            "active_tool": None,
            "last_assistant_intent": "",
            "total_tool_calls": 0,
            "last_heartbeat_time": time.monotonic(),
            "last_progress_notify_time": time.monotonic(),
        }

        self._active_task = {
            "proc": proc,
            "prompt": prompt,
            "start_time": time.time(),
            "pid": proc.pid,
            "stream_id": stream_id,
            "user_id": user_id,
            "group_id": group_id,
            "session_id": resume_session_id,
            "status": "running",
            "ask_future": None,
            "progress_tracker": progress_tracker,
        }
        try:
            await self._safe_send_text("⏳ OMP 任务已在沙盒 RPC 模式中启动... 正在处理中，请稍候。", stream_id)

            collected_assistant_text: List[str] = []
            chunk_buffers: Dict[str, List[Dict[str, Any]]] = {}
            active_session_id: Optional[str] = resume_session_id
            task_success = False
            task_error_desc = ""

            loop = asyncio.get_running_loop()
            async def send_rpc(msg: Dict[str, Any]) -> None:
                if proc.stdin and not proc.stdin.is_closing():
                    payload_bytes = json.dumps(msg).encode("utf-8") + b"\n"
                    proc.stdin.write(payload_bytes)
                    await proc.stdin.drain()

            while True:
                now = time.monotonic()

                # 1. 单次工具调用悬挂超时检测 (Tool Hang Watchdog)
                active_tool = progress_tracker.get("active_tool")
                if active_tool:
                    tool_elapsed = now - float(active_tool.get("start_time", now))
                    if tool_elapsed > tool_hang_timeout_seconds:
                        t_name = str(active_tool.get("name", "unknown"))
                        t_sum = str(active_tool.get("summary", ""))
                        task_error_desc = f"工具 [{t_name}: {t_sum}] 连续执行超过 {tool_hang_timeout_seconds} 秒未返回，判定为工具执行卡死，已主动终止任务"
                        await send_rpc({"id": "tool-hang-abort", "type": "abort"})
                        break

                # 2. 全协议通道静默无数据超时检测 (Idle Heartbeat Watchdog)
                idle_elapsed = now - float(progress_tracker.get("last_heartbeat_time", now))
                if idle_elapsed > idle_timeout_seconds:
                    task_error_desc = f"任务数据流已连续静默超过 {idle_timeout_seconds} 秒无任何响应，判定为通道死锁，已主动终止任务"
                    await send_rpc({"id": "idle-abort", "type": "abort"})
                    break

                # 3. 单任务防死循环熔断兜底 (Absolute Safety Limits)
                if (now - start_monotonic) > max_total_seconds:
                    task_error_desc = f"任务执行时长达到单任务绝对上限（{max_total_seconds // 60} 分钟），触发防死循环熔断保护"
                    await send_rpc({"id": "max-time-abort", "type": "abort"})
                    break

                if int(progress_tracker.get("total_tool_calls", 0)) > max_tool_calls:
                    task_error_desc = f"任务累计调用工具达到上限（{max_tool_calls} 次），触发防死循环熔断保护"
                    await send_rpc({"id": "max-tools-abort", "type": "abort"})
                    break

                # 4. 周期性发送阶段性进展报告卡片 (Periodic Progress Notification)
                if (now - float(progress_tracker.get("last_progress_notify_time", now))) >= progress_notify_seconds:
                    progress_tracker["last_progress_notify_time"] = now
                    elapsed_min = round((now - start_monotonic) / 60, 1)

                    tool_stats_str = ""
                    tool_counts: Dict[str, int] = progress_tracker.get("tool_counts", {})
                    if tool_counts:
                        sorted_tools = sorted(tool_counts.items(), key=lambda x: x[1], reverse=True)
                        tool_items = [f"{t}: {c}次" for t, c in sorted_tools[:5]]
                        tool_stats_str = "• 动作统计: " + " | ".join(tool_items) + f" (累计 {progress_tracker.get('total_tool_calls', 0)} 次)\n"

                    if active_tool:
                        active_sec = int(now - float(active_tool.get("start_time", now)))
                        action_desc = f"• 当前动作: ⚡ [{active_tool.get('name')}] {active_tool.get('summary')} (已执行 {active_sec}s)"
                    else:
                        action_desc = "• 当前动作: 💭 模型正在思考与编排下一步指令"

                    intent_text = str(progress_tracker.get("last_assistant_intent", "")).strip().replace("\n", " ")
                    if len(intent_text) > 80:
                        intent_text = intent_text[:80] + "..."
                    intent_desc = f"• 最近意图: {intent_text}\n" if intent_text else ""

                    progress_nodes = [
                        f"⏱️ 【OMP 任务阶段性进展报告】 (已执行 {elapsed_min} 分钟)",
                        action_desc,
                        f"{tool_stats_str}{intent_desc}💡 任务仍在健康推进中；如需中断可随时发送 /omp stop",
                    ]
                    await self._safe_send_forward(progress_nodes, stream_id, title="OMP 任务运行进展")

                # 5. 读取下一行 stdio 数据（带短超时驱动看门狗循环）
                try:
                    line_bytes = await asyncio.wait_for(proc.stdout.readline(), timeout=1.0)
                except asyncio.TimeoutError:
                    if proc.returncode is not None:
                        break
                    continue

                if not line_bytes:
                    break

                # 收到任何数据，立即刷新心跳
                progress_tracker["last_heartbeat_time"] = time.monotonic()

                line_str = line_bytes.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                try:
                    frame = json.loads(line_str)
                except Exception:
                    continue

                # Protocol v2 chunk 解包拼接重组
                if frame.get("type") == "rpc_chunk":
                    chunk_id = str(frame.get("chunkId", ""))
                    if chunk_id:
                        chunk_buffers.setdefault(chunk_id, []).append(frame)
                        total_count = frame.get("count", 1)
                        if len(chunk_buffers[chunk_id]) >= total_count:
                            sorted_chunks = sorted(chunk_buffers[chunk_id], key=lambda c: c.get("index", 0))
                            combined_b64 = "".join(str(c.get("data", "")) for c in sorted_chunks)
                            chunk_buffers.pop(chunk_id, None)
                            try:
                                raw_json = base64.b64decode(combined_b64).decode("utf-8")
                                frame = json.loads(raw_json)
                            except Exception as reassemble_err:
                                logger.debug(f"RPC Chunk 重组解码失败: {reassemble_err}")
                                continue
                        else:
                            continue

                frame_type = str(frame.get("type", ""))

                # 1. 握手帧与协议升级
                if frame_type == "ready":
                    await send_rpc({
                        "id": "proto-v2",
                        "type": "negotiate_protocol",
                        "protocolVersion": 2,
                    })
                    await send_rpc({
                        "id": "proto-ask",
                        "type": "set_ask_dialog",
                        "enabled": True,
                    })
                    await send_rpc({
                        "id": "task-prompt",
                        "type": "prompt",
                        "message": prompt,
                        "prompt": prompt,
                    })
                elif frame_type == "response":
                    if frame.get("id") == "task-prompt" and not frame.get("success"):
                        err_text = str(frame.get("error") or "OMP 拒绝接收任务指令")
                        task_error_desc = f"指令下发失败: {err_text}"
                        logger.error(f"OMP 拒绝执行 prompt: {err_text}")
                        break

                # 捕获任何包含会话 ID 的协议帧
                if "sessionId" in frame and frame["sessionId"]:
                    active_session_id = str(frame["sessionId"])
                    self._active_session_id = active_session_id
                    if self._active_task:
                        self._active_task["session_id"] = active_session_id
                # 2. 状态与会话生命周期
                elif frame_type == "session_start":
                    sid = frame.get("sessionId")
                    if sid:
                        active_session_id = str(sid)
                        self._active_session_id = active_session_id
                        if self._active_task:
                            self._active_task["session_id"] = active_session_id

                elif frame_type == "message_end":
                    msg_obj = frame.get("message", {})
                    if isinstance(msg_obj, dict) and msg_obj.get("role") == "assistant":
                        c = msg_obj.get("content", "")
                        if isinstance(c, str):
                            collected_assistant_text.append(c)
                        elif isinstance(c, list):
                            for part in c:
                                if isinstance(part, dict) and part.get("type") == "text":
                                    collected_assistant_text.append(part.get("text", ""))
                elif frame_type == "tool_call":
                    tool_name = str(frame.get("toolName", "unknown"))
                    tool_input = frame.get("input", {})
                    summary = ""
                    if isinstance(tool_input, dict):
                        if "command" in tool_input:
                            summary = str(tool_input["command"]).strip().replace("\n", " ")[:60]
                        elif "path" in tool_input:
                            summary = str(tool_input["path"]).strip()
                        elif "pattern" in tool_input:
                            summary = str(tool_input["pattern"]).strip()[:40]
                        elif "query" in tool_input:
                            summary = str(tool_input["query"]).strip()[:40]
                        else:
                            for v in tool_input.values():
                                if isinstance(v, (str, int, float, bool)):
                                    summary = str(v)[:40]
                                    break
                    progress_tracker["active_tool"] = {
                        "name": tool_name,
                        "summary": summary,
                        "start_time": time.monotonic(),
                    }
                    tool_counts_dict: Dict[str, int] = progress_tracker.setdefault("tool_counts", {})
                    tool_counts_dict[tool_name] = tool_counts_dict.get(tool_name, 0) + 1
                    progress_tracker["total_tool_calls"] = int(progress_tracker.get("total_tool_calls", 0)) + 1

                elif frame_type == "tool_result":
                    progress_tracker["active_tool"] = None

                elif frame_type == "message_update":
                    delta = frame.get("delta", {})
                    if isinstance(delta, dict):
                        text_chunk = delta.get("text") or delta.get("thinking") or ""
                        if isinstance(text_chunk, str) and text_chunk.strip():
                            progress_tracker["last_assistant_intent"] = text_chunk.strip()

                # 3. 双向交互 ask 窗口处理与 120 秒超时主动 abort
                elif frame_type == "extension_ui_request":
                    method = str(frame.get("method", ""))
                    # 展示型请求无须交互，直接忽略
                    if method in {"notify", "setStatus", "setWidget", "setTitle", "set_editor_text"}:
                        continue

                    req_id = frame.get("id")
                    title_text = str(frame.get("title") or "OMP 任务决策询问")
                    msg_text = str(frame.get("message") or frame.get("prompt") or "")

                    questions = frame.get("questions", [])
                    if method == "ask" and questions:
                        q_lines: List[str] = []
                        for q in questions:
                            q_text = str(q.get("question") or "")
                            options = q.get("options", [])
                            opt_labels = [str(opt.get("label", "")) for opt in options if opt.get("label")]
                            if opt_labels:
                                q_lines.append(f"{q_text}\n  选项: {' / '.join(opt_labels)}")
                            else:
                                q_lines.append(q_text)
                        if q_lines:
                            msg_text = "\n\n".join(q_lines)

                    if not msg_text:
                        msg_text = "OMP 需要您的介入确认以继续推进。"

                    ask_nodes = [
                        "⚠️ 【OMP 任务需要决策确认】",
                        f"• 决策类型: {method}",
                        f"• 内容说明:\n{msg_text}",
                        "💡 决策回复指引：",
                        f"• 请任务发起人在 {ask_timeout_seconds} 秒内进行答复；",
                        "• 群聊方式：引用本卡片回复，或发送 /omp a <您的选择或答复>",
                        "• 私聊方式：直接发送 /omp a <您的选择或答复>",
                        "⏱️ 超时纪律：超时未收到答复将主动终止任务以保护沙盒。",
                    ]
                    await self._safe_send_forward(ask_nodes, stream_id, title=title_text)

                    ask_fut: asyncio.Future[str] = loop.create_future()
                    if self._active_task:
                        self._active_task["status"] = "waiting_user_input"
                        self._active_task["ask_future"] = ask_fut

                    try:
                        user_reply = await asyncio.wait_for(ask_fut, timeout=float(ask_timeout_seconds))
                        user_reply_clean = user_reply.strip()

                        if method == "ask":
                            answers_list = []
                            for q in questions:
                                qid = str(q.get("id", ""))
                                opts = [str(opt.get("label", "")) for opt in q.get("options", [])]
                                if user_reply_clean in opts:
                                    answers_list.append({"id": qid, "selectedOptions": [user_reply_clean]})
                                else:
                                    answers_list.append({"id": qid, "selectedOptions": [], "customInput": user_reply_clean})
                            resp_payload = {
                                "type": "extension_ui_response",
                                "id": req_id,
                                "answers": answers_list,
                            }
                        elif method == "confirm":
                            confirmed = user_reply_clean.lower() in {"yes", "y", "true", "确认", "是", "1"}
                            resp_payload = {
                                "type": "extension_ui_response",
                                "id": req_id,
                                "confirmed": confirmed,
                            }
                        else:
                            resp_payload = {
                                "type": "extension_ui_response",
                                "id": req_id,
                                "value": user_reply_clean,
                            }

                        await send_rpc(resp_payload)
                    except asyncio.TimeoutError:
                        await send_rpc({
                            "id": "timeout-abort",
                            "type": "abort",
                        })
                        abort_nodes = [
                            "⏱️ 【OMP 决策超时主动终止】",
                            f"在设定的 {ask_timeout_seconds} 秒内未收到发起人的确认回复。",
                            "🛡️ 已向 OMP 发送 abort 软终止信号，沙盒环境与会话锁已安全复位。",
                        ]
                        await self._safe_send_forward(abort_nodes, stream_id, title="OMP 决策超时通知")
                        task_error_desc = f"决策确认超时（超过 {ask_timeout_seconds} 秒），任务已主动终止"
                        break
                    finally:
                        if self._active_task:
                            self._active_task["status"] = "running"
                            self._active_task["ask_future"] = None
                # 4. 任务结束标志
                elif frame_type == "prompt_result":
                    status = str(frame.get("status", "completed"))
                    if status in {"completed", "success"}:
                        task_success = True
                    else:
                        task_error_desc = str(frame.get("error") or f"任务结束状态: {status}")
                    break

            try:
                if proc.stdin and not proc.stdin.is_closing():
                    proc.stdin.close()
                await asyncio.wait_for(proc.wait(), timeout=10.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

            elapsed = round(time.monotonic() - start_monotonic, 1)
            generated_files = self._detect_generated_files(workspace_path, before_snapshot, start_wall_time)
            final_text = "\n\n".join(collected_assistant_text).strip()
            lower_final = final_text.lower()
            if (
                "request was aborted" in lower_final
                or lower_final.startswith("** error:")
                or "the operation was aborted" in lower_final
            ):
                task_success = False
                task_error_desc = final_text or "模型请求被中断 (Request was aborted)"

            if not final_text and not task_success and task_error_desc:
                final_text = task_error_desc
            return task_success, final_text, elapsed, active_session_id, generated_files

        except Exception as exc:
            elapsed = round(time.monotonic() - start_monotonic, 1)
            return False, f"OMP RPC 执行过程异常: {exc}", elapsed, None, []
        finally:
            self._active_task = None
            self._active_session_id = None

    async def _reply_task_result(
        self,
        success: bool,
        output: str,
        duration: float,
        session_id: Optional[str],
        generated_files: List[Path],
        stream_id: str,
        group_id: str = "",
        user_id: str = "",
    ) -> Tuple[bool, str, bool]:
        """将 OMP 任务产出按照智能分流规则回传。"""
        if not success:
            err_msg = output.strip() or "OMP 未返回明确报错"
            nodes = [
                "❌ OMP 任务执行失败！",
                f"• 耗时: {duration} 秒\n• 会话 ID: {session_id[:8] if session_id else '未生成'}",
                f"• 错误摘要:\n{err_msg[:800]}",
            ]
            await self._safe_send_forward(nodes, stream_id, title="OMP 执行失败通知")
            return False, "OMP 执行失败", True

        workspace_path = self._resolve_workspace_dir()
        max_reply_len = self.effective_config.execution.max_reply_length
        cleaned_output = output.strip()

        # 分流 1: 单文件生成 -> 原生 QQ 文件卡片
        if len(generated_files) == 1:
            target_file = generated_files[0]
            rel_name = str(target_file.relative_to(workspace_path))
            file_size_kb = round(target_file.stat().st_size / 1024, 1)
            upload_success = await self._safe_send_file(
                target_file, stream_id, group_id=group_id, user_id=user_id
            )
            if upload_success:
                desc_text = (
                    f"🎉 OMP 任务执行成功（耗时 {duration} 秒）！\n"
                    f"📦 已生成业务产出文件: {rel_name} ({file_size_kb} KB)\n"
                    f"📌 终端恢复: omp -r {session_id[:8]}"
                    if session_id
                    else ""
                )
                await self._safe_send_text(desc_text, stream_id)
                return True, "单文件已成功上传", True

        # 分流 2: 多文件生成 -> 合并转发卡片汇总
        if len(generated_files) > 1:
            summary_nodes = [
                f"🎉 OMP 任务执行成功（耗时 {duration} 秒）！\n"
                f"📦 本次共生成/实质修改了 {len(generated_files)} 个业务文件："
            ]
            for idx, gf in enumerate(generated_files, 1):
                rel_p = str(gf.relative_to(workspace_path))
                sz_kb = round(gf.stat().st_size / 1024, 1)
                summary_nodes.append(f"[{idx}] {rel_p} ({sz_kb} KB)")
                asyncio.create_task(
                    self._safe_send_file(gf, stream_id, group_id=group_id, user_id=user_id)
                )

            if cleaned_output:
                preview = cleaned_output[:max_reply_len]
                if len(cleaned_output) > max_reply_len:
                    preview += "\n\n... (执行输出过长已截断)"
                summary_nodes.append(f"📋 执行分析摘要:\n{preview}")

            if session_id:
                summary_nodes.append(f"📌 终端恢复: omp -r {session_id[:8]}")

            await self._safe_send_forward(summary_nodes, stream_id, title="OMP 多文件交付清单")
            return True, "多文件清单已发送", True

        # 分流 3: 无文件生成 -> 纯文本结论直接发送
        if len(cleaned_output) > max_reply_len:
            display_output = (
                cleaned_output[:max_reply_len]
                + f"\n\n... (回复超过 {max_reply_len} 字已截断，可输入 /omp export 导出网页查看)"
            )
        else:
            display_output = cleaned_output or "任务执行完毕，无文本输出。"

        if session_id:
            display_output += f"\n\n📌 终端恢复: omp -r {session_id[:8]}"

        await self._safe_send_text(display_output, stream_id)
        return True, "执行结果已发送", True

    @Command(
        "omp",
        description="直接下发任务给 OMP 执行或查询 OMP 运行状态",
        pattern=r"^/omp(?:\s+(?P<args>.*))?$",
        permission="public",
        timeout_ms=1200000,
    )
    async def handle_omp(
        self,
        event: Any = None,
        args: Optional[str] = None,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> Tuple[bool, str, bool]:
        """处理 /omp 用户显式命令。除启动与成功交付外，全部采用合并转发发送。"""
        stream_id = str(stream_id or getattr(event, "stream_id", "") or kwargs.get("stream_id", "") or "").strip()
        raw_user_id = str(user_id or getattr(event, "user_id", "") or kwargs.get("user_id", "") or "").strip()
        raw_group_id = str(group_id or getattr(event, "group_id", "") or kwargs.get("group_id", "") or "").strip()

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}
        if not args:
            args = str(matched_groups.get("args") or kwargs.get("args") or "").strip()
        if not args:
            raw_text = str(kwargs.get("text") or getattr(event, "text", "") or "").strip()
            match = re.match(r"^/omp(?:\s+(?P<args>.*))?$", raw_text, re.DOTALL)
            if match:
                args = str(match.group("args") or "").strip()

        args = (args or "").strip()
        # 分支 1: 帮助菜单
        if not args or args == "help":
            help_nodes = [
                (
                    "🤖 OMP (AI 编程与系统 Harness) 指令指南\n"
                    "提供 Bubblewrap 内核沙盒强隔离与全新 Ephemeral RPC-UI 流式双向交互。\n\n"
                    "【常用指令】\n"
                    "• /omp <任务描述> : 在沙盒中向 OMP 下发任务\n"
                    "• /omp a <回答>   : 回复 OMP 决策提问（群聊亦可直接引用卡片回复）\n"
                    "• /omp stop       : 向任务下发 abort 软中断并安全复位沙盒\n"
                    "• /omp resume     : 查看最近 5 个会话列表（互斥锁保护，拒绝打断）\n"
                    "• /omp resume <序号/ID> <内容> : 沿用历史会话继续对话\n"
                    "• /omp export     : 将最近一次会话导出为独立 HTML 交互网页\n"
                    "• /omp status     : 查看当前沙盒环境、RPC 状态与活动会话\n"
                    "• /omp help       : 查看本帮助指南"
                ),
                (
                    "🛡️ 【沙盒安全与互斥锁纪律】\n"
                    "• 内核沙盒: 全盘强制只读，仅 data/omp_workspace/ 目录可写\n"
                    "• 会话互斥: 同一时间同一个 Session 绝不允许打断或并发执行\n"
                    "• 决策窗口: ask 提问仅限发起人应答，120 秒超时主动终止并告警"
                ),
            ]
            await self._safe_send_forward(help_nodes, stream_id, title="OMP Agent 帮助说明")
            return True, "已显示帮助信息", True

        # 分支 2: 交互决策回复 (/omp a <回答> 或 /omp answer <回答>)
        if args.startswith("a ") or args.startswith("answer "):
            answer_text = args.split(maxsplit=1)[1].strip() if " " in args else ""
            if not self._active_task or self._active_task.get("status") != "waiting_user_input":
                await self._safe_send_forward(
                    ["ℹ️ 当前没有正在等待决策确认的 OMP 任务。"], stream_id, title="OMP 决策提示"
                )
                return False, "无等待决策任务", True

            initiator_uid = str(self._active_task.get("user_id", "")).strip()
            if initiator_uid and raw_user_id and raw_user_id != initiator_uid:
                await self._safe_send_forward(
                    ["⚠️ 仅任务发起人有权回复当前的决策询问。"], stream_id, title="OMP 权限拦截"
                )
                return False, "非发起人拦截", True

            ask_fut = self._active_task.get("ask_future")
            if ask_fut and not ask_fut.done():
                ask_fut.set_result(answer_text)
                await self._safe_send_forward(
                    [f"✅ 已成功向 OMP 注入决策答复：\n{answer_text}"], stream_id, title="OMP 决策注入成功"
                )
                return True, "已注入决策答复", True

            return False, "决策注入失败", True

        # 分支 3: 查询运行状态
        if args == "status":
            omp_path = self._resolve_omp_binary()
            bwrap_path = self._resolve_bwrap_binary()
            workspace_path = self._resolve_workspace_dir()

            if self._active_task is not None:
                task_prompt = self._active_task["prompt"].strip().replace("\n", " ")
                if len(task_prompt) > 40:
                    task_prompt = task_prompt[:40] + "..."
                elapsed = round(time.time() - self._active_task["start_time"], 1)
                task_status_desc = self._active_task.get("status", "running")
                tracker = self._active_task.get("progress_tracker", {})
                active_tool = tracker.get("active_tool") if isinstance(tracker, dict) else None
                if active_tool:
                    act_sec = int(time.monotonic() - float(active_tool.get("start_time", time.monotonic())))
                    cur_act = f"⚡ [{active_tool.get('name')}] {active_tool.get('summary')} (已执行 {act_sec}s)"
                elif task_status_desc == "waiting_user_input":
                    cur_act = "⏳ 等待发起人决策答复"
                else:
                    cur_act = "💭 思考与指令执行中"

                total_tools = tracker.get("total_tool_calls", 0) if isinstance(tracker, dict) else 0

                task_node = (
                    "【当前活动任务】\n"
                    f"• 状态: {'⏳ 等待发起人决策中' if task_status_desc == 'waiting_user_input' else '⚡ 正在沙盒 RPC 执行'}\n"
                    f"• 当前动作: {cur_act}\n"
                    f"• 累计工具调用: {total_tools} 次\n"
                    f"• PID: {self._active_task['pid']}\n"
                    f"• 会话: {self._active_session_id[:8] if self._active_session_id else '新会话'}\n"
                    f"• 发起人: {self._active_task['user_id'] or '未知'}\n"
                    f"• 已运行时长: {elapsed} 秒\n"
                    f"• 任务描述: {task_prompt}"
                )
            else:
                task_node = "【当前活动任务】\n• 状态: 空闲 (无正在运行的 OMP 任务)"

            proxy_desc = self.effective_config.security.proxy_url or "未启用"
            env_node = (
                "【环境与沙盒】\n"
                f"• 通信架构: Ephemeral RPC-UI (Protocol v2 分片支持)\n"
                f"• OMP 路径: {omp_path or '❌ 未检测到'}\n"
                f"• 沙盒保护: {'✅ Bubblewrap 内核隔离' if self.effective_config.security.use_sandbox else '⚠️ 已禁用'}\n"
                f"• bwrap 路径: {bwrap_path or '未找到'}\n"
                f"• 可写工作区: {workspace_path}\n"
                f"• 默认模型: {self.effective_config.runtime.default_model}\n"
                f"• 代理地址: {proxy_desc}\n"
                f"• 外部活动会话: {len(self._external_sessions)} 个"
            )

            hang_sec = self.effective_config.execution.tool_hang_timeout_seconds
            idle_sec = self.effective_config.execution.idle_timeout_seconds
            notify_sec = self.effective_config.execution.progress_notify_seconds
            max_sec = self.effective_config.execution.max_total_seconds
            max_calls = self.effective_config.execution.max_tool_calls
            ask_sec = self.effective_config.execution.ask_timeout_seconds
            control_node = (
                "【执行与看门狗控制】\n"
                f"• 决策窗口超时: {ask_sec} 秒 (超时主动 abort 终止)\n"
                f"• 进展汇报周期: 每 {notify_sec} 秒发送阶段性战报\n"
                f"• 单工具卡死看门狗: {hang_sec} 秒无响应主动 abort\n"
                f"• 协议静默死锁看门狗: {idle_sec} 秒无数据主动 abort\n"
                f"• 防死循环熔断兜底: 最长 {max_sec // 60} 分钟 / 最多 {max_calls} 次工具调用\n"
                f"• 外部监听: {'Unix Socket IPC (50ms 旁路)' if self.effective_config.notification.watch_external_sessions else '关闭'}"
            )
            status_nodes = [task_node, env_node, control_node]
            await self._safe_send_forward(status_nodes, stream_id, title="OMP Agent 运行状态报告")
            return True, "已显示状态信息", True

        # 分支 4: 优雅停止任务 (/omp stop)
        if args == "stop":
            if self._active_task is not None:
                proc = self._active_task["proc"]
                task_prompt = self._active_task["prompt"].strip().replace("\n", " ")
                if len(task_prompt) > 40:
                    task_prompt = task_prompt[:40] + "..."
                elapsed = round(time.time() - self._active_task["start_time"], 1)

                try:
                    if proc.stdin and not proc.stdin.is_closing():
                        proc.stdin.write(json.dumps({"id": "stop-cmd", "type": "abort"}).encode("utf-8") + b"\n")
                        await proc.stdin.drain()
                    await asyncio.sleep(0.5)
                    if proc.returncode is None:
                        proc.terminate()
                        await asyncio.sleep(0.3)
                        if proc.returncode is None:
                            proc.kill()
                except Exception:
                    pass

                self._active_task = None
                self._active_session_id = None
                stop_nodes = [
                    "🛑 已成功向 OMP 下发中断信号并终止任务！",
                    f"• 中断任务: {task_prompt}\n• 运行时长: {elapsed} 秒\n• 沙盒状态: 进程已终止，工作空间与互斥锁已复位",
                ]
                await self._safe_send_forward(stop_nodes, stream_id, title="OMP 任务中断通知")
                return True, "已停止 OMP 任务", True
            else:
                stop_nodes = ["ℹ️ 当前没有正在运行的 OMP 任务。"]
                await self._safe_send_forward(stop_nodes, stream_id, title="OMP 任务中断通知")
                return True, "无运行中任务", True

        # 分支 5: 会话列表与安全续写 (/omp resume)
        if args == "resume" or args.startswith("resume "):
            recent_sessions = self._list_recent_sessions(limit=5)
            if not recent_sessions:
                await self._safe_send_forward(
                    ["⚠️ 暂无可用的 OMP 历史会话，请先执行一次 /omp 任务。"],
                    stream_id,
                    title="OMP 历史会话",
                )
                return False, "未找到会话", True

            resume_args = args[len("resume") :].strip()
            if not resume_args:
                resume_nodes = [
                    (
                        "📂 OMP 最近历史会话列表（最近 5 次）\n\n"
                        "💡 沿历史会话继续对话：\n"
                        "发送：/omp resume <序号或ID> <新任务/继续说明>\n"
                        "例如：/omp resume 1 帮我在这个基础上再增加一个导出按钮\n"
                        "⚠️ 注意：同一时间一个会话仅允许一人运行，禁止并发打断。"
                    )
                ]
                for index, session in enumerate(recent_sessions, 1):
                    summary = session["summary"] or "(无提示词记录)"
                    if len(summary) > 45:
                        summary = summary[:45] + "..."
                    is_busy = self._is_session_locked(session["session_id"])
                    status_flag = " [🔒 占用中]" if is_busy else " [🟢 空闲]"
                    resume_nodes.append(
                        f"[{index}] 会话 ID: {session['session_id'][:8]}{status_flag}\n"
                        f"• 时间: {session['updated_at']}\n"
                        f"• 初始任务: {summary}\n"
                        f"• 续接示例: /omp resume {session['session_id'][:8]} <新任务>"
                    )
                await self._safe_send_forward(resume_nodes, stream_id, title="OMP 历史会话列表")
                return True, "已列出会话列表", True

            resume_parts = resume_args.split(maxsplit=1)
            resume_key = resume_parts[0]
            resume_prompt = resume_parts[1].strip() if len(resume_parts) > 1 else ""
            if not resume_prompt:
                await self._safe_send_forward(
                    [
                        "⚠️ 请在会话标识后补充要继续的内容。\n"
                        "例如：/omp resume 1 在刚才生成的网页上加一个本地持久化存储功能"
                    ],
                    stream_id,
                    title="OMP 会话续写提示",
                )
                return False, "缺少续写内容", True

            target_session_id = self._match_session_id(resume_key, recent_sessions)
            if not target_session_id:
                await self._safe_send_forward(
                    [
                        f"⚠️ 未找到匹配的会话：{resume_key}\n"
                        "请发送 /omp resume 查看最近 5 个可用会话列表。"
                    ],
                    stream_id,
                    title="未找到匹配会话",
                )
                return False, "未找到会话", True

            success, output, duration, session_id, generated_files = await self._run_omp_task(
                prompt=resume_prompt,
                stream_id=stream_id,
                user_id=raw_user_id,
                group_id=raw_group_id,
                resume_session_id=target_session_id,
            )
            return await self._reply_task_result(
                success=success,
                output=output,
                duration=duration,
                session_id=session_id,
                generated_files=generated_files,
                stream_id=stream_id,
                group_id=raw_group_id,
                user_id=raw_user_id,
            )

        # 分支 6: 手动导出 HTML (/omp export)
        if args == "export" or args.startswith("export "):
            target_id = ""
            export_args = args[len("export") :].strip()

            ws_dir = self._resolve_workspace_session_dir()
            if not ws_dir or not ws_dir.is_dir():
                await self._safe_send_forward(
                    ["⚠️ 当前工作空间暂无 OMP 历史会话，请先执行一次 /omp 任务。"],
                    stream_id,
                    title="OMP 会话导出失败",
                )
                return False, "未找到会话", True

            recent_sessions = self._list_recent_sessions(limit=5)
            if export_args:
                target_id = self._match_session_id(export_args, recent_sessions) or export_args
            else:
                jsonls = list(ws_dir.glob("*.jsonl"))
                if not jsonls:
                    await self._safe_send_forward(
                        ["⚠️ 当前工作空间暂无 OMP 历史会话，请先执行一次 /omp 任务。"],
                        stream_id,
                        title="OMP 会话导出失败",
                    )
                    return False, "未找到会话", True
                latest_session = max(jsonls, key=lambda p: p.stat().st_mtime)
                stem = latest_session.stem
                target_id = stem.split("_")[-1] if "_" in stem else stem

            html_path = await self._export_session_to_html(target_id)
            if html_path:
                sent = await self._safe_send_file(
                    html_path, stream_id, group_id=raw_group_id, user_id=raw_user_id
                )
                status_desc = "📄 交互式网页文件已导出并上传！" if sent else "📄 交互式网页文件已导出并保存在宿主机！"
                export_nodes = [
                    status_desc,
                    f"• 会话 ID: {target_id[:8]}",
                    f"• 文件名: {html_path.name}\n• 本地路径: {html_path}",
                    "💡 提示：在浏览器中打开该文件即可离线查看完整代码高亮、思考过程与工具调用。",
                ]
                await self._safe_send_forward(export_nodes, stream_id, title="OMP 会话导出完成")
                return True, "已导出并发送 HTML 文件", True
            else:
                await self._safe_send_forward(
                    [f"❌ 导出会话 {target_id[:8]} 的 HTML 文件失败，请检查该会话是否存在。"],
                    stream_id,
                    title="OMP 会话导出失败",
                )
                return False, "导出失败", True

        # 分支 7: 执行新任务
        if not self.effective_config.plugin.enabled:
            await self._safe_send_forward(
                ["❌ OMP 插件当前处于禁用状态，请在配置中开启后再试。"],
                stream_id,
                title="OMP 插件状态",
            )
            return False, "OMP 插件已禁用", True

        success, output, duration, session_id, generated_files = await self._run_omp_task(
            prompt=args,
            stream_id=stream_id,
            user_id=raw_user_id,
            group_id=raw_group_id,
        )
        return await self._reply_task_result(
            success=success,
            output=output,
            duration=duration,
            session_id=session_id,
            generated_files=generated_files,
            stream_id=stream_id,
            group_id=raw_group_id,
            user_id=raw_user_id,
        )

    async def on_load(self) -> None:
        """插件加载初始化。"""
        logger.info("OMP Agent 插件正在加载 (v1.2.0 - Ephemeral RPC-UI)")
        self._deploy_extension_bridge()
        if self.effective_config.notification.watch_external_sessions:
            await self._start_ipc_server()

    async def on_unload(self) -> None:
        """处理插件卸载。"""
        logger.info("OMP Agent 插件正在卸载")
        if self._ipc_server is not None:
            self._ipc_server.close()
            await self._ipc_server.wait_closed()
            self._ipc_server = None
        sock_path_str = self.effective_config.notification.bridge_socket_path.strip()
        if sock_path_str:
            sock_p = Path(sock_path_str)
            if sock_p.exists():
                try:
                    sock_p.unlink()
                except Exception:
                    pass
        if self._active_task is not None:
            proc = self._active_task.get("proc")
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass
            self._active_task = None
            self._active_session_id = None

    async def on_config_update(
        self, scope: str, config_data: Dict[str, Any], version: str
    ) -> None:
        """处理配置动态热重载更新。"""
        del scope, config_data, version
        logger.info("OMP Agent 插件检测到配置变更，重新应用...")
        if self._ipc_server is not None:
            self._ipc_server.close()
            await self._ipc_server.wait_closed()
            self._ipc_server = None
        if self.effective_config.notification.watch_external_sessions:
            await self._start_ipc_server()
        logger.info("OMP Agent 插件配置已重载完成")


def create_plugin() -> OmpAgentPlugin:
    """创建 OMP Agent 插件实例。"""
    return OmpAgentPlugin()
