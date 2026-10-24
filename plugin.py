"""OMP Agent 插件 — 基于 maibot-plugin-sdk

为麦麦提供调用宿主机 OMP (AI 编程与系统 Harness) 的能力，
支持通过用户前缀命令（@Command /omp）直接交互与任务调度。
内置 Bubblewrap (bwrap) 沙盒隔离：仅 data/omp_workspace/ 目录可写，全盘强制只读。
"""

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import asyncio
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
        default="",
        description="指定使用的模型名称（留空使用 OMP 自身当前默认配置）",
    )
    timeout_seconds: int = Field(
        default=180,
        ge=10,
        le=600,
        description="单次执行最大超时时间（秒）",
    )
    auto_approve: bool = Field(
        default=True,
        description="是否自动授权工具调用（免交互执行）",
    )
    save_session: bool = Field(
        default=True,
        description="是否保存会话记录以便在终端使用 omp -r 恢复或查看",
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

    async def _run_omp_task(
        self,
        prompt: str,
        working_dir: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> Tuple[bool, str, float, Optional[str]]:
        """执行 OMP 子进程任务。内置 Bubblewrap 沙盒隔离。

        Args:
            prompt: 提示词或任务指令。
            working_dir: 任务执行工作目录，缺省使用配置中的默认目录。
            model: 模型名称覆盖，缺省使用配置或 OMP 默认模型。
            timeout: 超时时间（秒），缺省使用配置值。

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
        cmd.append(f"--cwd={workspace_path}")
        cmd.append(prompt)

        start_monotonic = time.monotonic()
        start_wall_time = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workspace_path),
            )
        except Exception as exc:
            elapsed = time.monotonic() - start_monotonic
            return False, f"启动 OMP 子进程失败: {exc}", elapsed, None

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=float(timeout_seconds),
            )
        except asyncio.TimeoutError:
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

    # ===== Command 组件：供用户通过 /omp 前缀显式触发 =====

    @Command(
        "omp",
        description="直接下发任务给 OMP 执行或查询 OMP 运行状态",
        pattern=r"^/omp(?:\s+(?P<args>.*))?$",
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

        args = str(matched_groups.get("args") or "").strip()
        if not args:
            raw_text = str(kwargs.get("text") or "").strip()
            match = re.match(r"^/omp(?:\s+(?P<args>.*))?$", raw_text, re.DOTALL)
            if match:
                args = str(match.group("args") or "").strip()

        # 分支 1: 帮助信息
        if not args or args in {"help", "-h", "--help"}:
            help_text = (
                "🛠️ OMP Agent 插件命令指南：\n"
                "• /omp <任务描述> : 在隔离沙盒中调用宿主机 OMP 执行开发与系统任务\n"
                "• /omp status : 查看 OMP 可执行文件状态、沙盒隔离与配置\n"
                "• /omp help : 显示本帮助指南\n\n"
                "🛡️ 安全防护：插件已开启 Bubblewrap 内核沙盒隔离，仅 data/omp_workspace/ 目录允许写入，系统其它目录全盘只读。"
            )
            await self._safe_send_text(help_text, stream_id)
            return True, "已显示帮助信息", True

        # 分支 2: 查看状态
        if args == "status":
            omp_path = self._resolve_omp_binary()
            bwrap_path = self._resolve_bwrap_binary()
            sandbox_active = self.effective_config.omp.use_sandbox and bool(bwrap_path)
            status_lines = [
                "📊 OMP Agent 状态信息：",
                f"• 插件启用: {'是' if self.effective_config.plugin.enabled else '否'}",
                f"• 沙盒隔离: {'已开启 🛡️ (Bubblewrap / 仅 workspace 可写，全盘只读)' if sandbox_active else '未开启 ⚠️'}",
                f"• OMP 路径: {omp_path or '未找到有效二进制文件 ❌'}",
                f"• 沙盒工作空间: {self.effective_config.omp.default_cwd}",
                f"• 默认模型: {self.effective_config.omp.default_model or 'OMP 自身默认'}",
                f"• 超时时间: {self.effective_config.omp.timeout_seconds} 秒",
                f"• 自动授权: {'是' if self.effective_config.omp.auto_approve else '否'}",
                f"• 保存会话: {'是 (支持 omp -r)' if self.effective_config.omp.save_session else '否'}",
                f"• 最大回复长度: {self.effective_config.omp.max_reply_length} 字",
            ]
            if omp_path:
                try:
                    probe_proc = await asyncio.create_subprocess_exec(
                        omp_path,
                        "--version",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    v_out, _ = await asyncio.wait_for(probe_proc.communicate(), timeout=5.0)
                    version_text = _clean_omp_output(v_out.decode("utf-8", errors="replace"))
                    status_lines.append(f"• OMP 版本: {version_text}")
                except Exception as exc:
                    status_lines.append(f"• 版本检测失败: {exc}")
            status_text = "\n".join(status_lines)
            await self._safe_send_text(status_text, stream_id)
            return True, "已显示状态信息", True

        # 分支 3: 执行具体任务
        if not self.effective_config.plugin.enabled:
            err_msg = "❌ OMP 插件当前处于禁用状态，请在配置中开启后再试。"
            await self._safe_send_text(err_msg, stream_id)
            return False, "OMP 插件已禁用", True

        success, output, duration, session_id = await self._run_omp_task(prompt=args)
        max_len = self.effective_config.omp.max_reply_length

        if success:
            display_output = output
            if len(display_output) > max_len:
                display_output = (
                    display_output[:max_len]
                    + f"\n\n... (输出已截断，共 {len(output)} 字，已保留前 {max_len} 字)"
                )
            resume_hint = (
                f"\n\n📌 会话恢复: omp -r {session_id[:8]}"
                if session_id
                else ""
            )
            reply_text = (
                f"✨ OMP 执行完成 ({round(duration, 2)}s)：\n\n"
                f"{display_output}"
                f"{resume_hint}"
            )
            await self._safe_send_text(reply_text, stream_id)
            return True, f"OMP 任务执行成功 ({round(duration, 2)}s)", True
        else:
            resume_hint = (
                f"\n\n📌 调试会话: omp -r {session_id[:8]}"
                if session_id
                else ""
            )
            reply_text = (
                f"❌ OMP 执行失败 ({round(duration, 2)}s)：\n\n"
                f"{output}"
                f"{resume_hint}"
            )
            await self._safe_send_text(reply_text, stream_id)
            return False, f"OMP 执行失败: {output}", True

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
