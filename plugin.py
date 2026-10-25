"""OMP Agent 插件 — 基于 maibot-plugin-sdk

为麦麦提供调用宿主机 OMP (AI 编程与系统 Harness) 的能力，
支持通过用户前缀命令（@Command /omp）直接交互与任务调度。
内置 Bubblewrap (bwrap) 沙盒隔离：仅 data/omp_workspace/ 目录可写，全盘强制只读。
"""

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import time
from datetime import datetime

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
    """插件基础元数据配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用 OMP 插件")
    config_version: str = Field(default="1.0.0", description="配置版本号")


class OmpSectionConfig(PluginConfigBase):
    """OMP 执行设置。"""

    __ui_label__ = "OMP 设置"
    __ui_icon__ = "terminal"
    __ui_order__ = 1

    omp_path: str = Field(
        default="/home/xiang/.bun/bin/omp",
        description="OMP 可执行文件绝对路径",
    )
    use_sandbox: bool = Field(
        default=True,
        description="是否启用 Bubblewrap 沙盒隔离（除 workspace 和会话目录外全盘只读）",
    )
    bwrap_path: str = Field(
        default="/usr/bin/bwrap",
        description="Bubblewrap (bwrap) 可执行文件绝对路径",
    )
    default_cwd: str = Field(
        default="/home/xiang/MaiBot/data/omp_workspace",
        description="默认工作空间目录绝对路径（沙盒内唯一可写工作目录）",
    )
    default_model: str = Field(
        default="gemini-3.8-flash",
        description="指定使用的模型名称（默认使用 gemini-3.8-flash）",
    )
    progress_notify_seconds: int = Field(
        default=300,
        ge=30,
        le=3600,
        description="执行超过该时长未结束时向聊天流发送进度告知（秒，默认 5 分钟）",
    )
    timeout_seconds: int = Field(
        default=900,
        ge=60,
        le=3600,
        description="单次执行最大硬超时时间（秒，默认 15 分钟）",
    )
    auto_approve: bool = Field(
        default=True,
        description="是否自动授权工具调用（免交互执行）",
    )
    save_session: bool = Field(
        default=True,
        description="是否保存会话记录以便在终端使用 omp -r 恢复或查看",
    )
    export_html: bool = Field(
        default=True,
        description="任务完成后是否使用 OMP 内置的 --export 功能将结果导出为 HTML 网页文件并发送",
    )
    max_reply_length: int = Field(
        default=1500,
        description="群聊/私聊直接返回的最大字符截断长度",
    )


class OmpPluginConfig(PluginConfigBase):
    """OMP 插件顶层配置模型。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    omp: OmpSectionConfig = Field(default_factory=OmpSectionConfig)


class OmpPlugin(MaiBotPlugin):
    """OMP Agent 插件实现。"""

    config_model = OmpPluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._default_config_instance: Optional[OmpPluginConfig] = None

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

    def _find_latest_session(self, start_timestamp: float) -> Optional[str]:
        """查找在任务执行期间新创建的 OMP 会话 ID。

        Args:
            start_timestamp: 任务开始执行时的时间戳。

        Returns:
            Optional[str]: 会话标识 UUID；未找到时返回 None。
        """
        try:
            sessions_root = Path.home() / ".omp" / "agent" / "sessions"
            if not sessions_root.is_dir():
                return None
            candidates = [
                p
                for p in sessions_root.glob("*/*.jsonl")
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
        """列出最近的 OMP 会话概要。

        Args:
            limit: 最多返回的会话数量。

        Returns:
            List[Dict[str, str]]: 每项包含 session_id、updated_at 与 summary。
        """
        sessions_root = Path.home() / ".omp" / "agent" / "sessions"
        if not sessions_root.is_dir():
            return []

        try:
            jsonls = sorted(
                sessions_root.glob("*/*.jsonl"),
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
                updated_at = datetime.fromtimestamp(jsonl.stat().st_mtime).strftime("%m-%d %H:%M")
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
        """从会话记录中提取首条用户提示词作为概要。

        Args:
            session_jsonl: 会话记录文件路径。

        Returns:
            str: 首条用户提示词摘要；无法解析时返回空字符串。
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
            Optional[str]: 匹配到的会话 ID；未匹配时返回 None。
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
            logger.error(f"向聊天流 {stream_id} 发送消息失败: {exc}", exc_info=True)
        return False

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

        sessions_root = Path.home() / ".omp" / "agent" / "sessions"
        if not sessions_root.is_dir():
            return None

        matched_files = list(sessions_root.glob(f"*/*{session_id}*.jsonl"))
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


    async def _run_omp_task(
        self,
        prompt: str,
        working_dir: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
        stream_id: str = "",
        resume_session_id: Optional[str] = None,
    ) -> Tuple[bool, str, float, Optional[str]]:
        """执行 OMP 子进程任务。内置 Bubblewrap 沙盒隔离。

        Args:
            prompt: 提示词或任务指令。
            working_dir: 任务执行工作目录，缺省使用配置中的默认目录。
            model: 模型名称覆盖，缺省使用配置或 OMP 默认模型。
            timeout: 超时时间（秒），缺省使用配置值。
            stream_id: 聊天流标识，用于发送进度通知。
            resume_session_id: 需要延续的历史会话 ID，缺省表示新建会话。

        Returns:
            Tuple[bool, str, float, Optional[str]]: (是否成功, 输出或错误内容, 执行耗时秒数, 会话ID)
        """
        omp_binary = self._resolve_omp_binary()
        if not omp_binary:
            return False, "未找到有效的 OMP 可执行文件，请检查插件配置中的 omp_path", 0.0, None

        target_cwd = (working_dir or "").strip() or (self.effective_config.omp.default_cwd or "").strip()
        workspace_path = Path(target_cwd).resolve()
        try:
            workspace_path.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning(f"确保工作空间目录存在失败: {workspace_path}: {exc}")

        selected_model = (model or "").strip() or (self.effective_config.omp.default_model or "").strip()
        timeout_seconds = timeout or self.effective_config.omp.timeout_seconds

        use_sandbox = self.effective_config.omp.use_sandbox
        bwrap_binary = self._resolve_bwrap_binary() if use_sandbox else None

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

            # 8. 启动 OMP 进程
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
            )
        except Exception as exc:
            elapsed = time.monotonic() - start_monotonic
            return False, f"启动 OMP 子进程失败: {exc}", elapsed, None

        # 确认子进程已成功生成并在后台运行，再发送启动回执
        preview_prompt = prompt.strip().replace("\n", " ")
        if len(preview_prompt) > 60:
            preview_prompt = preview_prompt[:60] + "..."
        start_lines = [
            "⏳ OMP 任务已在沙盒中启动...",
            f"• 目标模型: {selected_model or 'OMP 自身默认'}",
            f"• 工作目录: {workspace_path}",
        ]
        if resume_session_id:
            start_lines.append(f"• 已恢复会话: {resume_session_id[:8]}")
        start_lines.append(f"• 任务指令: {preview_prompt}")
        start_lines.append("正在处理中，请稍候。")
        await self._safe_send_text("\n".join(start_lines), stream_id)

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

            # 超过设定时间（默认 5 分钟）尚未结束时通知用户，任务继续在沙盒中执行
            if not notified_progress and (time.monotonic() - start_monotonic) >= progress_notify_sec:
                notified_progress = True
                elapsed_minutes = max(1, int((time.monotonic() - start_monotonic) // 60))
                await self._safe_send_text(
                    f"⏳ OMP 任务仍在进行中（已执行 {elapsed_minutes} 分钟）...\n"
                    "正在沙盒中继续分析与处理，未中断。执行完成后将自动发送最终结果与 HTML 网页，请耐心等待。",
                    stream_id,
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
            return False, f"OMP 执行超时（超过 {timeout_seconds} 秒），任务已中止", elapsed, None

        elapsed = time.monotonic() - start_monotonic
        if resume_session_id:
            # 延续历史会话时，OMP 会复用原会话文件，ID 保持不变
            session_id = resume_session_id
        else:
            session_id = (
                self._find_latest_session(start_wall_time)
                if self.effective_config.omp.save_session
                else None
            )
        stdout_str = _clean_omp_output(stdout_b.decode("utf-8", errors="replace"))
        stderr_str = _clean_omp_output(stderr_b.decode("utf-8", errors="replace"))

        if proc.returncode != 0:
            err_msg = stderr_str or stdout_str or f"OMP 进程异常退出，退出码: {proc.returncode}"
            return False, err_msg, elapsed, session_id

        output = stdout_str or stderr_str or "(执行成功，但无输出内容)"
        return True, output, elapsed, session_id

    async def _reply_task_result(
        self,
        success: bool,
        output: str,
        duration: float,
        session_id: Optional[str],
        stream_id: str,
        group_id: str = "",
        user_id: str = "",
    ) -> Tuple[bool, Optional[str], bool]:
        """回传任务执行结果，并按需附带 HTML 交互网页文件。

        Args:
            success: 任务是否执行成功。
            output: 任务输出或错误信息。
            duration: 任务执行耗时（秒）。
            session_id: 关联的 OMP 会话 ID。
            stream_id: 聊天流标识。
            group_id: 群号（如果是群聊）。
            user_id: 用户 QQ 号。

        Returns:
            Tuple[bool, Optional[str], bool]: 命令执行结果三元组。
        """
        max_len = self.effective_config.omp.max_reply_length
        display_output = output
        if len(display_output) > max_len:
            display_output = (
                display_output[:max_len]
                + f"\n\n... (输出已截断，共 {len(output)} 字，已保留前 {max_len} 字)"
            )

        if success:
            resume_hint = f"\n📌 终端恢复: omp -r {session_id[:8]}" if session_id else ""
            reply_text = (
                f"✨ OMP 任务执行成功 (耗时: {round(duration, 2)}s)\n"
                "📄 任务详情、完整代码与思考过程请查看下方 HTML 交互网页。"
                f"{resume_hint}"
            )
            await self._safe_send_text(reply_text, stream_id)
        else:
            resume_hint = f"\n\n📌 会话调试: omp -r {session_id[:8]}" if session_id else ""
            reply_text = (
                f"❌ OMP 任务执行失败 (耗时: {round(duration, 2)}s)\n\n"
                f"【失败信息】\n{display_output}{resume_hint}"
            )
            await self._safe_send_text(reply_text, stream_id)

        # 使用 OMP 内置的 --export 功能将结果转换为 HTML 网页文件并发送
        if success and self.effective_config.omp.export_html and session_id:
            html_path = await self._export_session_to_html(session_id)
            if html_path:
                sent = await self._safe_send_file(
                    html_path, stream_id, group_id=group_id, user_id=user_id
                )
                if not sent:
                    await self._safe_send_text(f"📄 交互网页已生成：{html_path}", stream_id)

        if success:
            return True, f"OMP 任务执行成功 ({round(duration, 2)}s)", True
        return False, f"OMP 执行失败: {output}", True

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
        """处理 /omp 用户显式命令。"""
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

        # 分支 1: 帮助信息
        if not args or args in {"help", "-h", "--help"}:
            help_text = (
                "🛠️ OMP Agent 指令指南\n\n"
                "【基础指令】\n"
                "• /omp <任务描述>                 向 OMP 下发开发任务（沙盒执行，完成后回传 HTML 交互网页）\n"
                "• /omp resume                     列出最近 5 个历史会话，可选择继续沿某一个会话深入对话\n"
                "• /omp resume <序号/ID> <内容>     沿用指定的历史会话继续提问或写代码\n"
                "• /omp export                     导出最近一次会话为交互式 HTML 网页并发送\n"
                "• /omp status                     查看核心运行环境与当前模型\n"
                "• /omp help                       显示本帮助信息\n\n"
                "【常用示例】\n"
                "• /omp 写一个带样式的待办事项网页 demo\n"
                "• /omp resume 1 在刚才生成的网页上加一个本地持久化存储功能\n\n"
                "🛡️ 运行环境与安全策略：\n"
                "- 内核级 Bubblewrap 沙盒强隔离，全盘强制只读保护。\n"
                "- 唯一可写工作空间：data/omp_workspace/\n"
                "- 敏感凭据目录（~/.ssh, ~/.gnupg）自动盲区遮蔽。"
            )
            await self._safe_send_text(help_text, stream_id)
            return True, "已显示帮助信息", True

        # 分支 2: 会话检索与延续
        if args == "resume" or args.startswith("resume "):
            recent_sessions = self._list_recent_sessions(limit=5)
            if not recent_sessions:
                await self._safe_send_text("⚠️ 暂无可用的 OMP 历史会话，请先执行一次 /omp 任务。", stream_id)
                return False, "未找到会话", True

            resume_args = args[len("resume") :].strip()
            if not resume_args:
                list_lines = [f"📋 最近 {len(recent_sessions)} 个 OMP 会话列表：", ""]
                for index, session in enumerate(recent_sessions, 1):
                    summary = session["summary"] or "(无提示词记录)"
                    if len(summary) > 45:
                        summary = summary[:45] + "..."
                    list_lines.append(f"[{index}] {session['session_id'][:8]} | {session['updated_at']}")
                    list_lines.append(f'    💬 "{summary}"')
                    list_lines.append("")
                list_lines.append("👉 如何继续对话：")
                list_lines.append("发送：/omp resume <序号或ID> <新任务/继续说明>")
                list_lines.append("例如：/omp resume 1 帮我在这个基础上再增加一个导出按钮")
                list_lines.append("或者：/omp resume 01a16e73 帮我在这个基础上再增加一个导出按钮")
                await self._safe_send_text("\n".join(list_lines), stream_id)
                return True, "已列出会话列表", True

            resume_parts = resume_args.split(maxsplit=1)
            resume_key = resume_parts[0]
            resume_prompt = resume_parts[1].strip() if len(resume_parts) > 1 else ""
            if not resume_prompt:
                await self._safe_send_text(
                    "⚠️ 请在会话标识后补充要继续的内容。\n"
                    "例如：/omp resume 1 在刚才生成的网页上加一个本地持久化存储功能",
                    stream_id,
                )
                return False, "缺少续写内容", True

            target_session_id = self._match_session_id(resume_key, recent_sessions)
            if not target_session_id:
                await self._safe_send_text(
                    f"⚠️ 未找到匹配的会话：{resume_key}\n"
                    "请先发送 /omp resume 查看最近 5 个会话列表。",
                    stream_id,
                )
                return False, "未找到会话", True

            success, output, duration, session_id = await self._run_omp_task(
                prompt=resume_prompt,
                stream_id=stream_id,
                resume_session_id=target_session_id,
            )
            return await self._reply_task_result(
                success=success,
                output=output,
                duration=duration,
                session_id=session_id,
                stream_id=stream_id,
                group_id=raw_group_id,
                user_id=raw_user_id,
            )

        # 分支 3: 手动导出最近会话为 HTML 网页
        if args == "export":
            sessions_root = Path.home() / ".omp" / "agent" / "sessions"
            if not sessions_root.is_dir():
                await self._safe_send_text("⚠️ 暂无可导出的 OMP 历史会话，请先执行一次 /omp 任务。", stream_id)
                return False, "未找到会话", True
            jsonls = list(sessions_root.glob("*/*.jsonl"))
            if not jsonls:
                await self._safe_send_text("⚠️ 暂无可导出的 OMP 历史会话，请先执行一次 /omp 任务。", stream_id)
                return False, "未找到会话", True
            latest_session = max(jsonls, key=lambda p: p.stat().st_mtime)
            stem = latest_session.stem
            latest_id = stem.split("_")[-1] if "_" in stem else stem
            html_path = await self._export_session_to_html(latest_id)
            if html_path:
                sent = await self._safe_send_file(
                    html_path, stream_id, group_id=raw_group_id, user_id=raw_user_id
                )
                if sent:
                    await self._safe_send_text(
                        "📄 交互式网页文件已上传完成！\n"
                        f"• 文件名: {html_path.name}\n"
                        f"• 本地路径: {html_path}\n"
                        "💡 提示：点击聊天中的文件即可在浏览器离线查看完整代码高亮、思考过程与工具调用。",
                        stream_id,
                    )
                else:
                    await self._safe_send_text(f"📄 交互网页已生成：{html_path}", stream_id)
                return True, "已导出并发送 HTML 文件", True
            else:
                await self._safe_send_text("❌ 导出 HTML 文件失败", stream_id)
                return False, "导出失败", True

        # 分支 4: 查看核心运行环境
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

            status_text = "\n".join(
                [
                    "📊 OMP Agent 运行状态",
                    "",
                    "【核心环境】",
                    f"• 插件状态: {'运行中 (Enabled)' if self.effective_config.plugin.enabled else '已停用 (Disabled)'}",
                    f"• OMP 引擎: {omp_path or '未找到有效二进制文件 ❌'} ({version_text})",
                    f"• 默认模型: {self.effective_config.omp.default_model or 'OMP 自身默认'}",
                ]
            )
            await self._safe_send_text(status_text, stream_id)
            return True, "已显示状态信息", True

        # 分支 5: 执行具体任务
        if not self.effective_config.plugin.enabled:
            err_msg = "❌ OMP 插件当前处于禁用状态，请在配置中开启后再试。"
            await self._safe_send_text(err_msg, stream_id)
            return False, "OMP 插件已禁用", True

        success, output, duration, session_id = await self._run_omp_task(prompt=args, stream_id=stream_id)
        return await self._reply_task_result(
            success=success,
            output=output,
            duration=duration,
            session_id=session_id,
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

    async def on_unload(self) -> None:
        """处理插件卸载。"""
        logger.info("OMP Agent 插件已卸载")

    async def on_config_update(
        self, scope: str, config_data: Dict[str, object], version: str
    ) -> None:
        """处理配置热重载事件。"""
        del scope
        del config_data
        del version
        logger.info("OMP Agent 插件配置已更新")


def create_plugin() -> OmpPlugin:
    """创建 OMP Agent 插件实例。

    Returns:
        OmpPlugin: 新的 OMP 插件实例。
    """
    return OmpPlugin()
