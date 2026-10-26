"""OMP Agent 插件 — 基于 maibot-plugin-sdk

为麦麦提供调用宿主机 OMP (AI 编程与系统 Harness) 的能力，
支持通过用户前缀命令（@Command /omp）直接交互与任务调度。
内置 Bubblewrap (bwrap) 沙盒隔离：仅 data/omp_workspace/ 目录可写，全盘强制只读。
支持 /omp stop 中断任务、/omp resume 续写会话，以及自动导出并发送 HTML 交互网页。
除即时启动回执与最终结果交付外，其余状态与系统信息全部采用 QQ 合并转发卡片形式发送。
"""

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase
from pathlib import Path
from pydantic import model_validator
from typing import Any, Dict, List, Optional, Set, Tuple

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
        default="1.1.0",
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
            "label": "Bubblewrap 路径",
            "placeholder": "/usr/bin/bwrap",
            "hint": "Linux 内核沙盒隔离工具路径",
        },
    )
    proxy_url: str = Field(
        default="http://127.0.0.1:7897",
        description="OMP 网络代理地址（免 TUN 模式直连本地代理端口）",
        json_schema_extra={
            "label": "代理服务器",
            "placeholder": "http://127.0.0.1:7897",
            "hint": "留空表示直连，设置后自动透传进沙盒与子进程，无需开启系统 TUN 模式",
        },
    )


class ExecutionSectionConfig(PluginConfigBase):
    """任务执行行为、硬超时与消息回复策略设置。"""

    __ui_label__ = "任务执行与超时策略"
    __ui_icon__ = "cpu"
    __ui_order__ = 3

    auto_approve: bool = Field(
        default=True,
        description="是否自动授权工具调用（免交互全自动执行）",
        json_schema_extra={
            "label": "自动授权调用",
            "hint": "关闭时执行到外部工具调用将在终端触发人工确认等待",
        },
    )
    save_session: bool = Field(
        default=True,
        description="是否保存会话记录以便在终端使用 omp -r 恢复或查看",
        json_schema_extra={
            "label": "保存历史会话",
            "hint": "执行完成后在群聊中附带终端 omp -r <id> 会话恢复指令",
        },
    )
    timeout_seconds: int = Field(
        default=900,
        ge=60,
        le=3600,
        description="单次执行最大硬超时时间（秒）",
        json_schema_extra={
            "label": "任务硬超时 (秒)",
            "hint": "超过该时长强行中止进程，默认 900 秒 (15 分钟)",
        },
    )
    progress_notify_seconds: int = Field(
        default=300,
        ge=30,
        le=3600,
        description="长任务执行进度提醒间隔（秒）",
        json_schema_extra={
            "label": "长任务提醒间隔 (秒)",
            "hint": "连续执行超过该时长未结束时自动发送合并转发进度卡片，默认 300 秒 (5 分钟)",
        },
    )
    max_reply_length: int = Field(
        default=1500,
        ge=200,
        le=5000,
        description="群聊/私聊直接返回的最大字符截断长度",
        json_schema_extra={
            "label": "文本最大截断字数",
            "hint": "防止长篇终端输出触发 QQ 消息过长风控，默认 1500 字",
        },
    )
    export_html: bool = Field(
        default=False,
        description="任务完成后是否自动导出 HTML 网页文件并发送",
        json_schema_extra={
            "label": "自动导出 HTML 网页",
            "hint": "默认关闭，开启后每次 /omp 任务完成都会自动导出完整交互网页并发送",
        },
    )


class NotificationSectionConfig(PluginConfigBase):
    """外部 OMP 任务监听与消息推送通知设置。"""

    __ui_label__ = "外部任务监听与通知"
    __ui_icon__ = "bell"
    __ui_order__ = 4

    watch_external_sessions: bool = Field(
        default=True,
        description="是否在后台监听非 Bot 启动的外部 OMP 任务并在需要注意时推送",
        json_schema_extra={
            "label": "监听外部 OMP 任务",
            "hint": "监听宿主机其他终端里运行的 OMP 任务（仅在人工决策、报错、完成时私聊提醒）",
        },
    )
    notify_user_id: str = Field(
        default="",
        description="外部 OMP 任务关注事件推送的目标 QQ 号（私聊推送，留空不推送）",
        json_schema_extra={
            "label": "外部任务推送 QQ 号",
            "placeholder": "如 2777087651",
            "hint": "接收外部任务重要决策与完成提醒的管理员 QQ 号，留空不推送",
        },
    )
    export_external_html: bool = Field(
        default=False,
        description="外部任务完成后是否自动导出 HTML 网页文件并私聊推送",
        json_schema_extra={
            "label": "外部任务导出 HTML",
            "hint": "默认关闭，开启后外部 OMP 任务完成时也会在私聊中附带上传 HTML 网页卡片",
        },
    )


class OmpPluginConfig(PluginConfigBase):
    """OMP 插件顶层配置模型。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    runtime: RuntimeSectionConfig = Field(default_factory=RuntimeSectionConfig)
    security: SecuritySectionConfig = Field(default_factory=SecuritySectionConfig)
    execution: ExecutionSectionConfig = Field(default_factory=ExecutionSectionConfig)
    notification: NotificationSectionConfig = Field(default_factory=NotificationSectionConfig)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_omp(cls, data: Any) -> Any:
        """平滑迁移旧版扁平 [omp] 配置块到 4 个语义配置节。"""
        if isinstance(data, dict) and "omp" in data and isinstance(data["omp"], dict):
            omp = data["omp"]
            data.setdefault("runtime", {})
            data.setdefault("security", {})
            data.setdefault("execution", {})
            data.setdefault("notification", {})
            for k in ["omp_path", "default_model", "default_cwd"]:
                if k in omp:
                    data["runtime"].setdefault(k, omp[k])
            for k in ["use_sandbox", "bwrap_path", "proxy_url"]:
                if k in omp:
                    data["security"].setdefault(k, omp[k])
            for k in ["auto_approve", "save_session", "timeout_seconds", "progress_notify_seconds", "max_reply_length", "export_html"]:
                if k in omp:
                    data["execution"].setdefault(k, omp[k])
            for k in ["watch_external_sessions", "notify_user_id"]:
                if k in omp:
                    data["notification"].setdefault(k, omp[k])
            if "export_external_html" in omp:
                data["notification"].setdefault("export_external_html", omp["export_external_html"])
        return data

    @property
    def omp(self) -> Any:
        """向后兼容属性代理，允许旧代码通过 self.effective_config.omp.xxx 无感访问。"""
        class _LegacyOmpView:
            def __init__(self, root: "OmpPluginConfig"):
                self._root = root

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
            def progress_notify_seconds(self) -> int:
                return self._root.execution.progress_notify_seconds

            @property
            def max_reply_length(self) -> int:
                return self._root.execution.max_reply_length

            @property
            def watch_external_sessions(self) -> bool:
                return self._root.notification.watch_external_sessions

            @property
            def notify_user_id(self) -> str:
                return self._root.notification.notify_user_id

            @property
            def export_html(self) -> bool:
                return self._root.execution.export_html

            @property
            def export_external_html(self) -> bool:
                return self._root.notification.export_external_html

        return _LegacyOmpView(self)


class OmpPlugin(MaiBotPlugin):
    """OMP Agent 插件实现。"""

    config_model = OmpPluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._default_config_instance: Optional[OmpPluginConfig] = None
        self._active_task: Optional[Dict[str, Any]] = None
        self._watcher_task: Optional[asyncio.Task[None]] = None
        self._watcher_start_time: float = time.time()
        self._bot_session_ids: Set[str] = set()
    @property
    def effective_config(self) -> OmpPluginConfig:
        """获取当前插件配置，未完成注入时返回默认配置实例。"""
        try:
            return self.config  # type: ignore[return-value]
        except RuntimeError:
            if self._default_config_instance is None:
                self._default_config_instance = OmpPluginConfig()
            return self._default_config_instance

    def _resolve_omp_binary(self) -> Optional[str]:
        """探测并返回可用的 OMP 二进制路径。

        Returns:
            Optional[str]: 可执行文件绝对路径；若均不可用返回 None。
        """
        configured = self.effective_config.omp.omp_path.strip()
        if configured and os.path.isfile(configured) and os.access(configured, os.X_OK):
            return configured

        expanded = os.path.expanduser(configured)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded

        which_path = shutil.which("omp")
        if which_path and os.path.isfile(which_path) and os.access(which_path, os.X_OK):
            return which_path

        common_candidates = [
            os.path.expanduser("~/.bun/bin/omp"),
            "/usr/local/bin/omp",
            "/usr/bin/omp",
        ]
        for candidate in common_candidates:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate

        return None

    def _resolve_bwrap_binary(self) -> Optional[str]:
        """探测并返回可用的 bwrap 二进制路径。

        Returns:
            Optional[str]: 可执行文件绝对路径；若均不可用返回 None。
        """
        configured = self.effective_config.omp.bwrap_path.strip()
        if configured and os.path.isfile(configured) and os.access(configured, os.X_OK):
            return configured

        which_path = shutil.which("bwrap")
        if which_path and os.path.isfile(which_path) and os.access(which_path, os.X_OK):
            return which_path

        common_candidates = [
            "/usr/bin/bwrap",
            "/bin/bwrap",
            "/usr/local/bin/bwrap",
        ]
        for candidate in common_candidates:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate

        return None
    def _resolve_workspace_session_dir(self) -> Optional[Path]:
        """定位属于当前默认工作空间专属的 OMP 会话目录，严格实现目录级 Session 隔离。

        Returns:
            Optional[Path]: 对应工作区的专属会话目录路径。
        """
        workspace = Path(self.effective_config.omp.default_cwd).resolve()
        sessions_root = Path.home() / ".omp" / "agent" / "sessions"
        if not sessions_root.is_dir():
            return None

        candidates: List[str] = []
        try:
            rel = workspace.relative_to(Path.home().resolve())
            candidates.append(f"-{str(rel).replace(os.sep, '-')}")
        except ValueError:
            pass

        candidates.append(str(workspace).replace(os.sep, "-"))

        for cand in candidates:
            d = sessions_root / cand
            if d.is_dir():
                return d

        target_dir = sessions_root / (candidates[0] if candidates else str(workspace).replace(os.sep, "-"))
        return target_dir

    def _find_latest_session(self, start_timestamp: float) -> Optional[str]:
        """查找在任务执行期间新创建的 OMP 会话 ID（严格限定在当前工作空间专属目录）。

        Args:
            start_timestamp: 任务开始执行时的时间戳。

        Returns:
            Optional[str]: 会话标识 UUID；未找到时返回 None。
        """
        try:
            ws_dir = self._resolve_workspace_session_dir()
            if not ws_dir or not ws_dir.is_dir():
                return None
            candidates = [
                p
                for p in ws_dir.glob("*.jsonl")
                if p.stat().st_mtime >= start_timestamp - 1.0
            ]
            if not candidates:
                return None
            latest = max(candidates, key=lambda p: p.stat().st_mtime)
            stem = latest.stem
            return stem.split("_")[-1] if "_" in stem else stem
        except Exception as exc:
            logger.debug(f"探测 OMP 会话 ID 失败: {exc}")
            return None

    def _list_recent_sessions(self, limit: int = 5) -> List[Dict[str, str]]:
        """列出属于当前工作空间专属的最近历史 OMP 会话记录（严格隔离）。

        Args:
            limit: 最大返回数量。

        Returns:
            List[Dict[str, str]]: 会话信息字典列表。
        """
        ws_dir = self._resolve_workspace_session_dir()
        if not ws_dir or not ws_dir.is_dir():
            return []

        try:
            jsonls = sorted(
                ws_dir.glob("*.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:limit]
        except Exception as exc:
            logger.debug(f"列出会话目录失败: {exc}")
            return []

        results: List[Dict[str, str]] = []
        for jsonl in jsonls:
            stem = jsonl.stem
            session_id = stem.split("_")[-1] if "_" in stem else stem
            try:
                mtime = jsonl.stat().st_mtime
                updated_at = datetime.datetime.fromtimestamp(mtime).strftime("%m-%d %H:%M")
            except OSError:
                updated_at = "-"
            results.append(
                {
                    "session_id": session_id,
                    "updated_at": updated_at,
                    "summary": self._extract_session_prompt(jsonl),
                }
            )
        return results

    @staticmethod
    def _extract_session_prompt(session_jsonl: Path) -> str:
        """从会话记录文件提取首条用户提示词。

        Args:
            session_jsonl: 会话文件路径。

        Returns:
            str: 首条用户提示词；解析失败返回空字符串。
        """
        try:
            with session_jsonl.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if data.get("type") != "message":
                        continue
                    message = data.get("message")
                    if not isinstance(message, dict) or message.get("role") != "user":
                        continue
                    content = message.get("content")
                    if isinstance(content, str):
                        return content.strip()
                    if isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                return str(item.get("text") or "").strip()
                    return ""
        except Exception as exc:
            logger.debug(f"解析会话概要失败: {exc}")
        return ""

    def _match_session_id(self, key: str, recent_sessions: List[Dict[str, str]]) -> Optional[str]:
        """根据序号或 ID 前缀匹配目标会话 ID。

        Args:
            key: 用户输入的序号或会话 ID 片段。
            recent_sessions: 最近的会话列表。

        Returns:
            Optional[str]: 匹配到的完整会话 ID；未匹配时返回 None。
        """
        clean_key = str(key or "").strip()
        if not clean_key:
            return None

        if clean_key.isdigit():
            index = int(clean_key)
            if 1 <= index <= len(recent_sessions):
                return recent_sessions[index - 1]["session_id"]
            return None

        lowered = clean_key.lower()
        for session in recent_sessions:
            if session["session_id"].lower().startswith(lowered):
                return session["session_id"]
        return None

    async def _safe_send_text(self, text: str, stream_id: str) -> bool:
        """安全向指定聊天流发送文本消息。

        Args:
            text: 发送内容。
            stream_id: 聊天流标识。

        Returns:
            bool: 发送是否成功。
        """
        if not stream_id:
            logger.warning("未提供 stream_id，跳过发送消息")
            return False
        try:
            if hasattr(self, "_ctx") and self._ctx is not None:
                return await self.ctx.send.text(text, stream_id)
        except Exception as exc:
            logger.error(f"向聊天流 {stream_id} 发送文本消息失败: {exc}", exc_info=True)
        return False

    async def _safe_send_forward(
        self,
        nodes: List[Dict[str, Any] | str],
        stream_id: str,
        title: str = "OMP 系统消息",
    ) -> bool:
        """安全向指定聊天流发送 QQ 合并转发卡片。

        Args:
            nodes: 转发节点列表，每个元素可为文本字符串或包含 segments 的字典。
            stream_id: 聊天流标识。
            title: 降级纯文本时的标题。

        Returns:
            bool: 发送是否成功。
        """
        if not stream_id or not nodes:
            return False

        formatted_nodes: List[Dict[str, Any]] = []
        fallback_texts: List[str] = []

        for item in nodes:
            if isinstance(item, str):
                text_content = item.strip()
                if not text_content:
                    continue
                fallback_texts.append(text_content)
                formatted_nodes.append(
                    {
                        "user_id": "2904312047",
                        "nickname": "OMP Agent",
                        "segments": [{"type": "text", "content": text_content}],
                    }
                )
            elif isinstance(item, dict):
                formatted_nodes.append(item)
                raw_segments = item.get("segments") or item.get("content") or []
                if isinstance(raw_segments, list):
                    for seg in raw_segments:
                        if isinstance(seg, dict) and seg.get("type") == "text":
                            fallback_texts.append(str(seg.get("content") or seg.get("data") or ""))

        if not formatted_nodes:
            return False

        # 途径 1: 尝试调用 SDK 发送合并转发卡片
        try:
            if hasattr(self, "_ctx") and self._ctx is not None:
                sent = await self.ctx.send.forward(formatted_nodes, stream_id)
                if sent:
                    return True
        except Exception as exc:
            logger.debug(f"发送合并转发消息失败，准备降级为纯文本: {exc}")

        # 途径 2: 若平台或网关不支持转发，合并为单条文本优雅降级发送
        fallback_body = f"📋 【{title}】\n\n" + "\n\n───────────────\n\n".join(fallback_texts)
        return await self._safe_send_text(fallback_body, stream_id)

    async def _safe_send_file(
        self,
        file_path: Path,
        stream_id: str,
        group_id: str = "",
        user_id: str = "",
    ) -> bool:
        """安全向指定聊天流发送 HTML 交互网页文件。

        Args:
            file_path: 目标文件路径。
            stream_id: 聊天流标识。
            group_id: 群号（如果是群聊）。
            user_id: 用户 QQ 号。

        Returns:
            bool: 发送是否成功。
        """
        if not file_path.is_file():
            return False

        abs_file = str(file_path.resolve())
        file_name = file_path.name
        clean_grp = str(group_id or "").strip()
        clean_usr = str(user_id or "").strip()

        # 途径 1: QQ / NapCat 原生文件上传 API
        # OneBot 11 协议中文件不能作为普通消息段发送（否则会被 QQ 降级为文本 "[file]"），
        # 必须调用 upload_group_file 或 upload_private_file。
        # 此外，NapCat 通常运行在 Docker 容器中，宿主机 /home/xiang/MaiBot/data/MaiMBot 映射为容器内 /MaiMBot/data。
        # 因此我们将文件同步复制一份到共享目录，并传递容器可见路径；若不支持则降级为 base64:// 协议传递。
        if hasattr(self, "_ctx") and self._ctx is not None:
            file_candidates: List[str] = []

            # 1.1 容器共享挂载目录
            shared_dir = Path("/home/xiang/MaiBot/data/MaiMBot/exports")
            try:
                shared_dir.mkdir(parents=True, exist_ok=True)
                container_file = shared_dir / file_name
                shutil.copy2(file_path, container_file)
                file_candidates.append(f"/MaiMBot/data/exports/{file_name}")
            except Exception as copy_err:
                logger.debug(f"复制文件到容器共享目录失败: {copy_err}")

            # 1.2 宿主机直连路径（若 NapCat 本地原生运行）
            file_candidates.append(abs_file)

            # 1.3 Base64 协议（NapCat 内置支持 base64:// 解析，文件 <= 15MB 时可用）
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
                            params={
                                "group_id": int(clean_grp),
                                "file": ref,
                                "name": file_name,
                            },
                        )
                        if isinstance(resp, dict) and resp.get("success") is not False:
                            logger.info(f"已通过 NapCat API 上传群文件: {file_name} -> group {clean_grp}")
                            return True
                    elif clean_usr and clean_usr.isdigit():
                        resp = await self.ctx.api.call(
                            "adapter.napcat.file.upload_private_file",
                            params={
                                "user_id": int(clean_usr),
                                "file": ref,
                                "name": file_name,
                            },
                        )
                        if isinstance(resp, dict) and resp.get("success") is not False:
                            logger.info(f"已通过 NapCat API 上传私聊文件: {file_name} -> user {clean_usr}")
                            return True
                except Exception as exc:
                    logger.debug(f"尝试使用引用 {ref[:40]}... 上传 NapCat 文件失败: {exc}")

        # 途径 2: 降级为通用 custom file 段（适配支持文件段的其它 IM 网关，如 WebUI）
        if hasattr(self, "_ctx") and self._ctx is not None and stream_id:
            try:
                payload = {
                    "file": abs_file,
                    "name": file_name,
                }
                res = await self.ctx.send.custom("file", payload, stream_id)
                if res:
                    return True
            except Exception as exc:
                logger.debug(f"通用 send.custom 文件发送失败: {exc}")

        return False

    async def _export_session_to_html(self, session_id: str) -> Optional[Path]:
        """使用 OMP 内置的 --export 功能将指定会话导出为独立 HTML 文件。

        Args:
            session_id: 会话标识。

        Returns:
            Optional[Path]: 导出的 HTML 文件绝对路径；失败时返回 None。
        """
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
        workspace_path = Path(self.effective_config.omp.default_cwd).resolve()
        workspace_path.mkdir(parents=True, exist_ok=True)

        try:
            proc = await asyncio.create_subprocess_exec(
                omp_binary,
                f"--export={session_jsonl}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workspace_path),
            )
            stdout_b, stderr_b = await proc.communicate()
            if proc.returncode != 0:
                logger.warning(
                    f"OMP 导出 HTML 失败 (code {proc.returncode}): {stderr_b.decode('utf-8', errors='replace')}"
                )
                return None

            stdout_text = stdout_b.decode("utf-8", errors="replace").strip()
            for line in stdout_text.splitlines():
                if "Exported to:" in line:
                    file_name = line.split("Exported to:", 1)[1].strip()
                    target_file = workspace_path / file_name
                    if target_file.is_file():
                        return target_file

            html_candidates = [
                p for p in workspace_path.glob("omp-session-*.html") if session_id in p.name
            ]
            if html_candidates:
                return max(html_candidates, key=lambda p: p.stat().st_mtime)
        except Exception as exc:
            logger.error(f"导出 OMP HTML 发生异常: {exc}", exc_info=True)

        return None

    async def _safe_send_private_text(self, text: str, user_id: str) -> bool:
        """安全向指定 QQ 号发送私聊文本消息。

        Args:
            text: 消息内容。
            user_id: 目标 QQ 号。

        Returns:
            bool: 发送是否成功。
        """
        clean_usr = str(user_id or "").strip()
        if not clean_usr or not clean_usr.isdigit():
            return False
        try:
            if hasattr(self, "_ctx") and self._ctx is not None:
                resp = await self.ctx.api.call(
                    "adapter.napcat.message.send_private_msg",
                    params={
                        "user_id": int(clean_usr),
                        "message": [{"type": "text", "data": {"text": text}}],
                    },
                )
                return isinstance(resp, dict) and resp.get("success") is not False
        except Exception as exc:
            logger.debug(f"向 QQ 用户 {clean_usr} 发送私聊消息失败: {exc}")
        return False

    async def _external_watcher_loop(self) -> None:
        """后台异步监听宿主机外部 OMP 会话（非 Bot 本身发起的任务）。

        严格按照约定：平时的常规工具调用保持静默，仅在出现以下 3 种需要注意的情况时才向 notify_user_id 私聊推送：
        1. 需要人工介入/确认（如调用了 ask 工具或提问决策）；
        2. 致命报错与工具失败；
        3. 外部任务最终执行完成（推送结论摘要与恢复指令）。
        """
        logger.info("外部 OMP 关注事件监听器已启动")
        sessions_root = Path.home() / ".omp" / "agent" / "sessions"

        tracked_offsets: Dict[str, int] = {}
        notified_keys: Set[str] = set()

        while True:
            try:
                await asyncio.sleep(2.5)

                notify_uid = str(self.effective_config.omp.notify_user_id or "").strip()
                if not notify_uid or not self.effective_config.omp.watch_external_sessions:
                    continue

                if not sessions_root.is_dir():
                    continue

                now_ts = time.time()
                recent_candidates = [
                    p
                    for p in sessions_root.glob("*/*.jsonl")
                    if p.stat().st_mtime >= now_ts - 3600
                ]

                for jsonl_path in recent_candidates:
                    jsonl_str = str(jsonl_path)
                    try:
                        file_mtime = jsonl_path.stat().st_mtime
                        file_size = jsonl_path.stat().st_size
                    except OSError:
                        continue

                    # 如果是新发现的文件且修改时间早于监听器启动前，跳至末尾
                    if jsonl_str not in tracked_offsets:
                        if file_mtime < getattr(self, "_watcher_start_time", now_ts) - 3.0:
                            tracked_offsets[jsonl_str] = file_size
                            continue
                        else:
                            tracked_offsets[jsonl_str] = 0

                    last_offset = tracked_offsets.get(jsonl_str, 0)
                    if file_size <= last_offset:
                        continue

                    project_name = jsonl_path.parent.name.lstrip("-") or "root"
                    stem = jsonl_path.stem
                    session_id = stem.split("_")[-1] if "_" in stem else stem

                    # 排除 1: 属于 Bot 自身沙盒工作空间专属目录的会话（Bot 自己的任务）
                    bot_ws_dir = self._resolve_workspace_session_dir()
                    if bot_ws_dir and jsonl_path.parent.resolve() == bot_ws_dir.resolve():
                        continue

                    # 排除 2: 已登记为 Bot 自身派发或恢复的会话 ID
                    if session_id in getattr(self, "_bot_session_ids", set()):
                        continue

                    # 排除 3: 当前主开发环境交互的会话
                    if session_id.startswith("01a17196"):
                        continue

                    # 排除 4: Bot 当前正在执行的活动任务期间生成的文件
                    if self._active_task is not None:
                        task_start = float(self._active_task.get("start_time", 0))
                        if file_mtime >= task_start - 2.0:
                            if hasattr(self, "_bot_session_ids"):
                                self._bot_session_ids.add(session_id)
                            continue
                    try:
                        with jsonl_path.open("r", encoding="utf-8") as f:
                            f.seek(last_offset)
                            new_content = f.read()
                            tracked_offsets[jsonl_str] = f.tell()

                        lines = new_content.splitlines()
                        for line in lines:
                            line_str = line.strip()
                            if not line_str:
                                continue
                            try:
                                event = json.loads(line_str)
                            except Exception:
                                continue

                            # 判定 1: 需要人工介入 / 确认（ask 工具调用）
                            if event.get("customType") == "tool_execution_start":
                                tool_data = event.get("data") or {}
                                tool_name = tool_data.get("toolName", "")
                                if tool_name == "ask":
                                    call_id = tool_data.get("toolCallId") or event.get("id") or ""
                                    event_key = f"{session_id}:ask:{call_id}"
                                    if event_key not in notified_keys:
                                        notified_keys.add(event_key)
                                        args = tool_data.get("args") or {}
                                        q_text = args.get("question") or args.get("prompt") or "OMP 终端请求决策输入"
                                        notice = (
                                            "🔔 【外部 OMP 需人工关注】\n\n"
                                            f"• 项目空间: {project_name}\n"
                                            f"• 会话 ID: {session_id[:8]}\n"
                                            f"• 询问内容: {q_text}\n\n"
                                            "💡 提示：外部任务采用只读监听，请在宿主机终端中查看并回复。"
                                        )
                                        await self._safe_send_private_text(notice, notify_uid)

                            # 判定 2: 致命异常终止（非普通工具失败）
                            if event.get("type") == "message":
                                msg = event.get("message") or {}
                                stop_reason = str(msg.get("stopReason") or "").strip().lower()
                                err_text = str(msg.get("errorMessage") or "").strip()
                                is_retrying = bool(msg.get("retryRecovery"))
                                if stop_reason in {"error", "aborted"} and err_text and not is_retrying:
                                        notified_keys.add(event_key)
                                        notice = (
                                            "⚠️ 【外部 OMP 任务异常中断】\n\n"
                                            f"• 项目空间: {project_name}\n"
                                            f"• 会话 ID: {session_id[:8]}\n"
                                            f"• 中断原因: {err_text[:200]}\n\n"
                                            f"📌 终端查看: omp -r {session_id[:8]}"
                                        )
                                        await self._safe_send_private_text(notice, notify_uid)

                            # 判定 3: 任务完成交付（assistant 回复生成完毕且不包含正在发起的工具调用，或会话退出）
                            is_turn_done = False
                            ans_preview = ""
                            if event.get("type") == "message":
                                msg = event.get("message") or {}
                                if msg.get("role") == "assistant":
                                    content = msg.get("content") or []
                                    has_tool = any(isinstance(c, dict) and c.get("type") == "toolCall" for c in content)
                                    has_text = any(
                                        isinstance(c, dict) and c.get("type") == "text" and str(c.get("text") or "").strip()
                                        for c in content
                                    )
                                    if has_text and not has_tool:
                                        is_turn_done = True
                                        for c in content:
                                            if isinstance(c, dict) and c.get("type") == "text":
                                                ans_preview = str(c.get("text") or "").strip()
                                                break

                            is_session_exit = event.get("customType") == "session_exit"

                            if is_turn_done or is_session_exit:
                                event_id = event.get("id") or str(event.get("timestamp") or "exit")
                                event_key = f"{session_id}:done:{event_id}"
                                if event_key not in notified_keys:
                                    notified_keys.add(event_key)
                                    now_str = datetime.datetime.now().strftime("%H:%M:%S")
                                    preview_text = (
                                        f"\n• 结论概要: {ans_preview[:80]}..."
                                        if ans_preview
                                        else ""
                                    )
                                    notice = (
                                        "✅ 【外部 OMP 任务已完成】\n\n"
                                        f"• 项目空间: {project_name}\n"
                                        f"• 会话 ID: {session_id[:8]}"
                                        f"{preview_text}\n"
                                        f"• 完成时间: {now_str}\n"
                                        f"📌 终端恢复: omp -r {session_id[:8]}"
                                    )
                                    await self._safe_send_private_text(notice, notify_uid)
                                    if self.effective_config.notification.export_external_html:
                                        html_path = await self._export_session_to_html(session_id)
                                        if html_path:
                                            await self._safe_send_file(html_path, stream_id="", user_id=notify_uid)

                    except Exception as err:
                        logger.debug(f"读取会话文件失败: {jsonl_path}: {err}")
            except asyncio.CancelledError:
                break
            except Exception as loop_exc:
                logger.debug(f"外部监听器循环异常: {loop_exc}")
                await asyncio.sleep(5.0)
    @staticmethod
    def _format_file_size(file_path: Path) -> str:
        """格式化文件大小为易读字符串。"""
        try:
            size_bytes = file_path.stat().st_size
            if size_bytes < 1024:
                return f"{size_bytes} B"
            elif size_bytes < 1024 * 1024:
                return f"{round(size_bytes / 1024, 1)} KB"
            else:
                return f"{round(size_bytes / (1024 * 1024), 1)} MB"
        except OSError:
            return "未知大小"

    @staticmethod
    def _detect_new_files(workspace_path: Path, pre_snapshot: Dict[Path, float]) -> List[Path]:
        """检测工作空间在任务执行后新增或被修改的文件，排除隐藏文件。"""
        try:
            post_files = {p: p.stat().st_mtime for p in workspace_path.glob("**/*") if p.is_file()}
            new_or_modified = [
                p
                for p, mtime in post_files.items()
                if (p not in pre_snapshot or mtime > pre_snapshot.get(p, 0))
                and not any(part.startswith(".") for part in p.relative_to(workspace_path).parts)
            ]
            return sorted(new_or_modified, key=lambda p: p.stat().st_mtime)
        except Exception as exc:
            logger.debug(f"检测工作空间新生成文件失败: {exc}")
            return []

    async def _run_omp_task(
        self,
        prompt: str,
        working_dir: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        resume_session_id: Optional[str] = None,
    ) -> Tuple[bool, str, float, Optional[str], List[Path]]:
        """执行 OMP 子进程任务。内置 Bubblewrap 沙盒隔离与工作区产出物检测。

        Args:
            prompt: 提示词或任务指令。
            working_dir: 任务执行工作目录，缺省使用配置中的默认目录。
            model: 模型名称覆盖，缺省使用配置或 OMP 默认模型。
            timeout: 超时时间（秒），缺省使用配置值。
            stream_id: 聊天流标识，用于发送进度通知。
            user_id: 发起用户 QQ 号。
            group_id: 发起群号。
            resume_session_id: 需要延续的历史会话 ID，缺省表示新建会话。

        Returns:
            Tuple[bool, str, float, Optional[str], List[Path]]: (是否成功, 输出或错误内容, 执行耗时秒数, 会话ID, 本次新生成的文件列表)
        """
        omp_binary = self._resolve_omp_binary()
        if not omp_binary:
            return False, "未找到有效的 OMP 可执行文件，请检查插件配置中的 omp_path", 0.0, None, []
        if resume_session_id:
            self._bot_session_ids.add(resume_session_id)


        target_cwd = (working_dir or "").strip() or (self.effective_config.omp.default_cwd or "").strip()
        workspace_path = Path(target_cwd).resolve()
        try:
            workspace_path.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning(f"确保工作空间目录存在失败: {workspace_path}: {exc}")

        # 执行前拍摄工作空间文件快照
        pre_snapshot: Dict[Path, float] = {}
        try:
            pre_snapshot = {p: p.stat().st_mtime for p in workspace_path.glob("**/*") if p.is_file()}
        except Exception:
            pass

        selected_model = (model or "").strip() or (self.effective_config.omp.default_model or "").strip()
        timeout_seconds = timeout or self.effective_config.omp.timeout_seconds

        use_sandbox = self.effective_config.omp.use_sandbox
        bwrap_binary = self._resolve_bwrap_binary() if use_sandbox else None
        proxy_url = (self.effective_config.omp.proxy_url or "").strip()
        env: Dict[str, str] = os.environ.copy()
        if proxy_url:
            env["HTTP_PROXY"] = proxy_url
            env["HTTPS_PROXY"] = proxy_url
            env["http_proxy"] = proxy_url
            env["https_proxy"] = proxy_url
            env["ALL_PROXY"] = proxy_url
            env["all_proxy"] = proxy_url
            env["NO_PROXY"] = "127.0.0.1,localhost,::1"
            env["no_proxy"] = "127.0.0.1,localhost,::1"

        cmd: List[str] = []
        if use_sandbox and bwrap_binary:
            # 组装 Bubblewrap 容器沙盒隔离参数
            cmd.extend([
                bwrap_binary,
                "--ro-bind", "/", "/",               # 1. 宿主机全盘强制只读
                "--dev", "/dev",                     # 2. 设备节点
                "--proc", "/proc",                   # 3. 进程系统信息
                "--tmpfs", "/tmp",                   # 4. 临时目录内存隔离
            ])
            # 5. 遮蔽高危凭据目录（~/.ssh, ~/.gnupg）
            for sensitive_dir in [Path.home() / ".ssh", Path.home() / ".gnupg"]:
                if sensitive_dir.is_dir():
                    cmd.extend(["--tmpfs", str(sensitive_dir)])

            # 6. 挂载 OMP 自身凭据与会话记录目录（支持认证与 resume）
            omp_home = Path.home() / ".omp"
            if omp_home.is_dir():
                cmd.extend(["--bind", str(omp_home), str(omp_home)])

            # 7. 唯一可写工作空间目录
            cmd.extend(["--bind", str(workspace_path), str(workspace_path)])
            cmd.extend(["--chdir", str(workspace_path)])

            # 8. 注入代理环境变量（免 TUN 模式直通）
            if proxy_url:
                cmd.extend([
                    "--setenv", "HTTP_PROXY", proxy_url,
                    "--setenv", "HTTPS_PROXY", proxy_url,
                    "--setenv", "http_proxy", proxy_url,
                    "--setenv", "https_proxy", proxy_url,
                    "--setenv", "ALL_PROXY", proxy_url,
                    "--setenv", "all_proxy", proxy_url,
                    "--setenv", "NO_PROXY", "127.0.0.1,localhost,::1",
                    "--setenv", "no_proxy", "127.0.0.1,localhost,::1",
                ])

            # 9. 启动 OMP 进程
            cmd.extend([omp_binary, "-p", "--mode", "text"])
        else:
            if use_sandbox and not bwrap_binary:
                logger.warning("已开启沙盒配置但未找到 bwrap 可执行文件，降级为宿主机原生执行")
            cmd.extend([omp_binary, "-p", "--mode", "text"])

        if not self.effective_config.omp.save_session:
            cmd.append("--no-session")
        if self.effective_config.omp.auto_approve:
            cmd.append("--auto-approve")
        if selected_model:
            cmd.append(f"--model={selected_model}")
        if resume_session_id:
            cmd.append(f"--resume={resume_session_id}")
        cmd.append(f"--cwd={workspace_path}")
        cmd.append(prompt)

        start_monotonic = time.monotonic()
        start_wall_time = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workspace_path),
                env=env,
            )
        except Exception as exc:
            elapsed = time.monotonic() - start_monotonic
            return False, f"启动 OMP 子进程失败: {exc}", elapsed, None, []

        # 登记当前活动任务，支持 /omp status 查询与 /omp stop 中断
        self._active_task = {
            "proc": proc,
            "prompt": prompt,
            "start_time": time.time(),
            "pid": proc.pid,
            "stream_id": stream_id,
            "user_id": user_id,
            "group_id": group_id,
        }

        try:
            # 确认子进程已成功生成并在后台运行，再发送单行极简普通文本启动回执
            await self._safe_send_text("⏳ OMP 任务已在沙盒中启动... 正在处理中，请稍候。", stream_id)

            communicate_task = asyncio.create_task(proc.communicate())
            notified_progress = False
            progress_notify_sec = float(self.effective_config.omp.progress_notify_seconds)
            hard_timeout_sec = float(timeout_seconds)

            stdout_b = b""
            stderr_b = b""
            timed_out = False

            while True:
                elapsed_current = time.monotonic() - start_monotonic
                remaining_total = hard_timeout_sec - elapsed_current
                if remaining_total <= 0:
                    timed_out = True
                    break

                wait_slice = (
                    min(progress_notify_sec, remaining_total)
                    if not notified_progress
                    else remaining_total
                )

                done, _ = await asyncio.wait([communicate_task], timeout=wait_slice)
                if communicate_task in done:
                    stdout_b, stderr_b = communicate_task.result()
                    break

                # 超过设定时间（默认 5 分钟）尚未结束时，通过合并转发卡片发送进度通知，OMP 继续运行
                if not notified_progress and (time.monotonic() - start_monotonic) >= progress_notify_sec:
                    notified_progress = True
                    elapsed_minutes = max(1, int((time.monotonic() - start_monotonic) // 60))
                    progress_nodes = [
                        f"⏱️ 任务已在沙盒中连续执行 {elapsed_minutes} 分钟，当前未中断，正在继续分析与处理。",
                        "🛡️ Bubblewrap 沙盒容器状态正常，网络连接与模型推理活跃。",
                        "⏳ 请耐心等待最终结果交付，若需中途取消可随时发送 `/omp stop`。",
                    ]
                    await self._safe_send_forward(
                        progress_nodes,
                        stream_id,
                        title=f"OMP 任务执行进度 (已运行 {elapsed_minutes} 分钟)",
                    )

            if timed_out:
                try:
                    proc.terminate()
                    await asyncio.sleep(0.5)
                    if proc.returncode is None:
                        proc.kill()
                except ProcessLookupError:
                    pass
                elapsed = time.monotonic() - start_monotonic
                return False, f"OMP 执行超时（超过 {timeout_seconds} 秒），任务已中止", elapsed, None, []
        finally:
            self._active_task = None

        elapsed = time.monotonic() - start_monotonic
        if resume_session_id:
            session_id = resume_session_id
        else:
            session_id = (
                self._find_latest_session(start_wall_time)
                if self.effective_config.omp.save_session
                else None
            )
        if session_id:
            self._bot_session_ids.add(session_id)

        stdout_str = _clean_omp_output(stdout_b.decode("utf-8", errors="replace"))
        stderr_str = _clean_omp_output(stderr_b.decode("utf-8", errors="replace"))

        if proc.returncode != 0:
            err_msg = stderr_str or stdout_str or f"OMP 进程异常退出，退出码: {proc.returncode}"
            return False, err_msg, elapsed, session_id, []

        generated_files = self._detect_new_files(workspace_path, pre_snapshot)
        output = stdout_str or stderr_str or "(执行成功，但无输出内容)"
        return True, output, elapsed, session_id, generated_files

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
    ) -> Tuple[bool, Optional[str], bool]:
        """回传任务执行结果：根据产出文件数量智能分流交付。

        - 单文件生成：直接作为原生 QQ 文件卡片发送；
        - 多文件生成：采用 QQ 合并转发卡片发送，防刷屏；
        - 无文件生成：直接发送结论文字。
        """
        max_len = self.effective_config.omp.max_reply_length
        display_output = output
        if len(display_output) > max_len:
            display_output = (
                display_output[:max_len]
                + f"\n\n... (输出已截断，共 {len(output)} 字，已保留前 {max_len} 字)"
            )

        if not success:
            resume_hint = f"📌 会话调试: omp -r {session_id[:8]}" if session_id else "无可用调试会话"
            error_nodes = [
                f"❌ OMP 任务执行失败 (耗时: {round(duration, 2)}s)",
                f"【失败原因与报错】\n{output.strip()}",
                resume_hint,
            ]
            await self._safe_send_forward(
                error_nodes,
                stream_id,
                title=f"OMP 执行失败异常报告 ({round(duration, 2)}s)",
            )
            return False, f"OMP 执行失败: {output}", True

        # 任务执行成功
        resume_hint = f"\n📌 终端恢复: omp -r {session_id[:8]}" if session_id else ""

        # 情况 1: 只生成了 1 个新成果文件 -> 直接单独发送该文件！
        if len(generated_files) == 1:
            target_file = generated_files[0]
            file_size_text = self._format_file_size(target_file)
            reply_text = (
                f"✨ OMP 任务执行完成 (耗时: {round(duration, 2)}s)\n"
                f"📄 成果文件已生成: {target_file.name} ({file_size_text})"
                f"{resume_hint}"
            )
            await self._safe_send_text(reply_text, stream_id)
            sent = await self._safe_send_file(
                target_file, stream_id, group_id=group_id, user_id=user_id
            )
            if not sent:
                await self._safe_send_text(
                    f"📄 成果文件已保存在宿主机工作区：{target_file}", stream_id
                )

        # 情况 2: 生成了大于 1 个成果文件 -> 使用 QQ 合并转发卡片发送！
        elif len(generated_files) > 1:
            reply_text = (
                f"✨ OMP 任务执行完成 (耗时: {round(duration, 2)}s)\n"
                f"📦 共生成了 {len(generated_files)} 个新成果文件，已通过合并转发卡片发送。"
                f"{resume_hint}"
            )
            await self._safe_send_text(reply_text, stream_id)

            summary_lines = [
                f"📦 本次任务在工作区 data/omp_workspace/ 下共生成了 {len(generated_files)} 个成果文件：\n"
            ]
            for idx, f in enumerate(generated_files, 1):
                summary_lines.append(f"{idx}. {f.name} ({self._format_file_size(f)})")

            forward_nodes = ["\n".join(summary_lines)]

            workspace_path = Path(self.effective_config.omp.default_cwd).resolve()
            for idx, f in enumerate(generated_files, 1):
                try:
                    rel = f.relative_to(workspace_path)
                except ValueError:
                    rel = f.name
                file_size_str = self._format_file_size(f)
                header = (
                    f"📄 [{idx}/{len(generated_files)}] {f.name} ({file_size_str})\n"
                    f"路径: {rel}\n"
                    "───────────────────\n"
                )
                content_preview = ""
                try:
                    if f.stat().st_size <= 100 * 1024:
                        text_data = f.read_text(encoding="utf-8", errors="replace")
                        if len(text_data) > 3500:
                            content_preview = (
                                text_data[:3500] + f"\n\n... (内容已截断，共 {len(text_data)} 字)"
                            )
                        else:
                            content_preview = text_data
                    else:
                        content_preview = f"(大文件，大小 {file_size_str}，保存在宿主机 {f})"
                except Exception:
                    content_preview = f"(二进制文件，大小 {file_size_str}，保存在宿主机 {f})"

                forward_nodes.append(header + content_preview)

            await self._safe_send_forward(
                forward_nodes, stream_id, title=f"OMP 成果文件清单 (共 {len(generated_files)} 个)"
            )

            # 后台静默将文件逐个上传到 QQ 文件
            for f in generated_files:
                asyncio.create_task(
                    self._safe_send_file(f, stream_id, group_id=group_id, user_id=user_id)
                )

        # 情况 3: 没有产生新文件（纯问答/查询/分析任务）
        else:
            reply_text = (
                f"✨ OMP 任务执行完成 (耗时: {round(duration, 2)}s)\n\n"
                f"【执行结果】\n{display_output}"
                f"{resume_hint}"
            )
            await self._safe_send_text(reply_text, stream_id)

        # 兼容配置：如果额外开启了 export_html，依然导出并发送会话网页卡片
        if self.effective_config.omp.export_html and session_id:
            html_path = await self._export_session_to_html(session_id)
            if html_path:
                await self._safe_send_file(html_path, stream_id, group_id=group_id, user_id=user_id)

        return True, f"OMP 任务执行成功 ({round(duration, 2)}s)", True

    # ===== Command 组件：供用户通过 /omp 前缀显式触发 =====

    @Command(
        "omp",
        description="直接下发任务给 OMP 执行或查询 OMP 运行状态",
        pattern=r"^/omp(?:\s+(?P<args>.*))?$",
        timeout_ms=1200000,
    )
    async def handle_omp(
        self,
        stream_id: str = "",
        **kwargs: Any,
    ) -> Tuple[bool, Optional[str], bool]:
        """处理 /omp 用户显式命令。除启动与成功交付外，全部采用合并转发发送。"""
        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        raw_group_id = str(kwargs.get("group_id") or "").strip()
        raw_user_id = str(kwargs.get("user_id") or "").strip()

        args = str(matched_groups.get("args") or "").strip()
        if not args:
            raw_text = str(kwargs.get("text") or "").strip()
            match = re.match(r"^/omp(?:\s+(?P<args>.*))?$", raw_text, re.DOTALL)
            if match:
                args = str(match.group("args") or "").strip()

        # 并发冲突防御：如果当前已有任务在沙盒中执行，拦截新的任务下发，避免沙盒文件踩踏与算力冲突
        is_query_cmd = (
            not args
            or args in {"help", "-h", "--help", "status", "-s", "stop"}
            or args == "resume"
            or args == "export"
            or (args.startswith("export") and len(args.split()) <= 2)
        )
        if self._active_task is not None and not is_query_cmd:
            elapsed_running = int(time.time() - float(self._active_task.get("start_time", time.time())))
            busy_prompt = str(self._active_task.get("prompt") or "").strip().replace("\n", " ")
            if len(busy_prompt) > 40:
                busy_prompt = busy_prompt[:40] + "..."
            busy_user = str(self._active_task.get("user_id") or "其他用户")
            busy_nodes = [
                "⚠️ 当前已有 OMP 任务正在沙盒中执行，请稍候！",
                (
                    f"• 正在运行: {busy_prompt}\n"
                    f"• 发起用户: {busy_user}\n"
                    f"• 已运行时长: {elapsed_running} 秒\n\n"
                    "💡 为防沙盒文件冲突，OMP 采用独占沙盒模式。请等待当前任务完成，或发送 `/omp stop` 中断后重试。"
                ),
            ]
            await self._safe_send_forward(busy_nodes, stream_id, title="OMP 任务忙碌提示")
            return False, "已有运行中的 OMP 任务", True

        # 分支 1: 帮助信息（合并转发卡片）
        if not args or args in {"help", "-h", "--help"}:
            help_nodes = [
                (
                    "【基础指令列表】\n"
                    "• /omp <任务描述>   在沙盒中执行任务（完成后发送结论提示，按需可用 /omp export 导出网页）\n"
                    "• /omp resume       列出最近 5 次历史会话，支持沿指定会话继续交互\n"
                    "• /omp stop         立即强行终止当前正在运行的 OMP 任务\n"
                    "• /omp export       导出最近一次会话为 HTML 网页文件并发送（也可指定会话 ID）\n"
                    "• /omp status       查看当前运行状态、沙盒隔离及环境参数\n"
                    "• /omp help         显示本帮助信息"
                ),
                (
                    "【常用指令示例】\n"
                    "• /omp 写一个带样式的待办事项网页 demo\n"
                    "• /omp resume 01a17141 继续添加删除按钮\n"
                    "• /omp stop\n"
                    "• /omp export"
                ),
                (
                    "【🛡️ 安全与沙盒策略】\n"
                    "• 内核级 Bubblewrap 沙盒隔离，全盘强制只读保护。\n"
                    "• 唯一可写空间：data/omp_workspace/\n"
                    "• 敏感凭证目录（~/.ssh, ~/.gnupg）自动盲区遮蔽。\n"
                    "• 任务执行完毕后会自动生成自包含的 HTML 网页文件发送。"
                ),
            ]
            await self._safe_send_forward(help_nodes, stream_id, title="OMP Agent 指令指南")
            return True, "已显示帮助信息", True

        # 分支 2: 查看状态（合并转发卡片）
        if args == "status":
            omp_path = self._resolve_omp_binary()
            version_text = "未知版本"
            if omp_path:
                try:
                    probe_proc = await asyncio.create_subprocess_exec(
                        omp_path,
                        "--version",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    v_out, _ = await asyncio.wait_for(probe_proc.communicate(), timeout=5.0)
                    raw_version = _clean_omp_output(v_out.decode("utf-8", errors="replace"))
                    if raw_version:
                        version_text = raw_version.replace("omp/", "v").strip()
                except Exception as exc:
                    logger.debug(f"探测 OMP 版本失败: {exc}")

            bwrap_path = self._resolve_bwrap_binary()
            sandbox_active = self.effective_config.omp.use_sandbox and bool(bwrap_path)

            if self._active_task is not None:
                elapsed_task = int(time.time() - float(self._active_task.get("start_time", time.time())))
                active_prompt = str(self._active_task.get("prompt", "")).strip().replace("\n", " ")
                if len(active_prompt) > 40:
                    active_prompt = active_prompt[:40] + "..."
                task_node = (
                    "【当前运行状态】\n"
                    f"• 活动任务: 执行中 ⏳ (已运行 {elapsed_task} 秒)\n"
                    f"• 进程 PID: {self._active_task.get('pid', '未知')}\n"
                    f"• 任务内容: {active_prompt}\n"
                    f"• 发起用户: {self._active_task.get('user_id') or '未知'}\n"
                    "💡 提示：如需中途取消，可发送 `/omp stop` 立即中断。"
                )
            else:
                task_node = (
                    "【当前运行状态】\n"
                    "• 活动任务: 空闲中 🟢 (无运行中任务)\n"
                    "• 沙盒状态: 就绪"
                )

            proxy_display = self.effective_config.omp.proxy_url or "未启用 (直连)"
            env_node = (
                "【核心环境】\n"
                f"• 插件状态: {'运行中 (Enabled)' if self.effective_config.plugin.enabled else '已停用 (Disabled)'}\n"
                f"• OMP 引擎: {omp_path or '未找到有效二进制文件 ❌'} ({version_text})\n"
                f"• 默认模型: {self.effective_config.omp.default_model or 'OMP 自身默认'}\n"
                f"• 网络代理: {proxy_display}\n"
                f"• 隔离级别: {'Bubblewrap 内核沙盒 (仅 workspace 读写)' if sandbox_active else '未开启沙盒'}\n"
                f"• 工作空间: {self.effective_config.omp.default_cwd}"
            )

            timeout_sec = self.effective_config.omp.timeout_seconds
            timeout_min = (
                f"{timeout_sec // 60} 分钟"
                if timeout_sec % 60 == 0
                else f"{round(timeout_sec / 60, 1)} 分钟"
            )
            progress_sec = self.effective_config.omp.progress_notify_seconds
            progress_min = (
                f"{progress_sec // 60} 分钟"
                if progress_sec % 60 == 0
                else f"{round(progress_sec / 60, 1)} 分钟"
            )

            control_node = (
                "【执行控制】\n"
                f"• 单次硬超时: {timeout_sec} 秒 ({timeout_min})\n"
                f"• 进度提醒: 超过 {progress_sec} 秒 ({progress_min}) 自动发送合并转发进度\n"
                f"• 会话保存: {'开启 (支持 omp -r)' if self.effective_config.omp.save_session else '关闭'}\n"
                f"• 网页导出: {'开启 (完成后自动上传 HTML 交互网页)' if self.effective_config.omp.export_html else '关闭'}"
            )

            status_nodes = [task_node, env_node, control_node]
            await self._safe_send_forward(status_nodes, stream_id, title="OMP Agent 运行状态报告")
            return True, "已显示状态信息", True

        # 分支 3: 强行停止任务（合并转发卡片）
        if args == "stop":
            if self._active_task is not None:
                proc = self._active_task["proc"]
                task_prompt = self._active_task["prompt"].strip().replace("\n", " ")
                if len(task_prompt) > 40:
                    task_prompt = task_prompt[:40] + "..."
                elapsed = round(time.time() - self._active_task["start_time"], 1)
                try:
                    proc.terminate()
                    await asyncio.sleep(0.3)
                    if proc.returncode is None:
                        proc.kill()
                except ProcessLookupError:
                    pass
                self._active_task = None
                stop_nodes = [
                    "🛑 已成功停止正在运行的 OMP 任务！",
                    f"• 中断任务: {task_prompt}\n• 运行时长: {elapsed} 秒\n• 沙盒状态: 进程已终止，工作空间已复位",
                ]
                await self._safe_send_forward(stop_nodes, stream_id, title="OMP 任务中断通知")
                return True, "已停止 OMP 任务", True
            else:
                stop_nodes = ["ℹ️ 当前没有正在运行的 OMP 任务。"]
                await self._safe_send_forward(stop_nodes, stream_id, title="OMP 任务中断通知")
                return True, "无运行中任务", True

        # 分支 4: 会话检索与延续（合并转发卡片）
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
                        "例如：/omp resume 1 帮我在这个基础上再增加一个导出按钮"
                    )
                ]
                for index, session in enumerate(recent_sessions, 1):
                    summary = session["summary"] or "(无提示词记录)"
                    if len(summary) > 45:
                        summary = summary[:45] + "..."
                    resume_nodes.append(
                        f"[{index}] 会话 ID: {session['session_id'][:8]}\n"
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

        # 分支 5: 手动导出会话为 HTML 网页（合并转发卡片 + 文件上传）
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
                    "💡 提示：在 QQ 或浏览器中打开该文件即可离线查看完整代码高亮、思考过程与工具调用。",
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

        # 分支 6: 执行具体任务
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

    # ===== 插件生命周期钩子 =====

    async def on_load(self) -> None:
        """处理插件加载。"""
        default_workspace = Path(self.effective_config.omp.default_cwd).resolve()
        try:
            default_workspace.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning(f"创建 OMP 默认工作空间失败: {default_workspace}: {exc}")

        omp_binary = self._resolve_omp_binary()
        bwrap_binary = self._resolve_bwrap_binary()
        if omp_binary:
            sandbox_desc = f"Bubblewrap 沙盒已开启 ({bwrap_binary})" if bwrap_binary else "无沙盒"
            logger.info(
                f"OMP Agent 插件已加载: omp={omp_binary}, 隔离模式={sandbox_desc}, 工作空间={default_workspace}"
            )
        else:
            logger.warning(
                f"OMP Agent 插件已加载，但未检测到 OMP 二进制文件: {self.effective_config.omp.omp_path}"
            )

        # 启动外部 OMP 关注事件监听器
        self._watcher_start_time = time.time()
        if self.effective_config.omp.watch_external_sessions:
            self._watcher_task = asyncio.create_task(self._external_watcher_loop())

    async def on_unload(self) -> None:
        """处理插件卸载。"""
        if self._watcher_task is not None:
            self._watcher_task.cancel()
            self._watcher_task = None
        if self._active_task is not None:
            proc = self._active_task.get("proc")
            if proc is not None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
            self._active_task = None
        logger.info("OMP Agent 插件已卸载")

    async def on_config_update(
        self, scope: str, config_data: Dict[str, object], version: str
    ) -> None:
        """处理配置热重载事件。"""
        del scope
        del config_data
        del version
        if self._watcher_task is not None:
            self._watcher_task.cancel()
            self._watcher_task = None
        if self.effective_config.omp.watch_external_sessions:
            self._watcher_start_time = time.time()
            self._watcher_task = asyncio.create_task(self._external_watcher_loop())
        logger.info("OMP Agent 插件配置已更新")


def create_plugin() -> OmpPlugin:
    """创建 OMP Agent 插件实例。

    Returns:
        OmpPlugin: 新的 OMP 插件实例。
    """
    return OmpPlugin()
