# OMP Agent 插件 (maibot-team.omp-agent)

通过显式指令为用户提供调用宿主机 OMP (AI 编程与系统 Harness) 的能力，内置 Bubblewrap 内核沙盒强隔离与内置 HTML 交互网页发送。

## 核心设计与使用规范
- **严格显式指令驱动**：Bot **绝不会自主**调用 OMP。日常闲聊或自然语言问答（如“我想写个网页/写个HTML”）时，Bot 仅作常规对话或普通建议，不会擅自唤起宿主机环境写代码。
- **唯一触发途径**：只有当用户在私聊或群聊中明确发送 `/omp` 开头的命令时，才会触发 OMP 任务执行。
- **内核级沙盒强隔离**：
  - 默认启用 Linux 原生 **Bubblewrap (`bwrap`) 沙盒**。
  - **全系统强制只读**：宿主机根目录、系统二进制、依赖库和主目录默认全盘只读（只读保护，禁止任何写、改、删）。
  - **唯一可写工作区**：仅有 `data/omp_workspace/` 目录允许写入和修改，防止篡改 MaiBot 自身文件或宿主机系统。
  - **隐私目录遮蔽**：`~/.ssh`、`~/.gnupg` 挂载为空白内存盘，防止私钥等凭据泄露。
- **长时间任务状态跟踪与 HTML 网页回传**：
  - 下发命令后立即回执 `⏳ OMP 任务已启动，正在沙盒中执行，请稍候...`；
  - 若任务执行超过 5 分钟，Bot 会向聊天流主动发送进度提醒（`⏳ OMP 任务仍在进行中（已执行 5 分钟）...`），OMP 保持继续执行不中断；
  - **任务完成后，自动利用 OMP 内置的 `--export` 功能将完整对话、思考过程与代码高亮打包为自包含的 HTML 交互网页文件，并通过文件消息直接发送回聊天流**。

## 指令使用方法
- `/omp <任务内容>`：在隔离沙盒中向宿主机 OMP 下发执行任务，完成后自动导出并发送 HTML 交互网页文件（例如 `/omp 写一个猜数字的小游戏`）。
- `/omp export`：手动将最近一次 OMP 会话导出为 HTML 网页文件并发送。
- `/omp status`：查看 OMP 可执行文件路径、沙盒隔离状态、当前工作空间及超时参数。
- `/omp help`：查看帮助指南。

## 会话保存与 Resume 恢复
执行任务时会自动保存会话记录至 `~/.omp/agent/sessions/`，执行完成后 Bot 会在回复末尾附带对应的恢复指令（如 `📌 会话恢复: omp -r 01a16f16`）。
- **查看与恢复会话**：在宿主机终端进入对应工作目录，执行：
  ```bash
  # 交互式选择历史会话
  omp -r

  # 直接指定会话 ID 继续或查看
  omp -r <session_id>

  # 继续最近一次会话
  omp --continue
  ```
- **手动导出网页记录**：
  ```bash
  omp --export ~/.omp/agent/sessions/<项目路径>/<session_file>.jsonl
  ```

## 配置项
可通过 WebUI 插件管理或 `config.toml` 配置：
- `plugin.enabled` (bool): 插件全局开关，默认 `true`
- `omp.use_sandbox` (bool): 是否启用 Bubblewrap 沙盒强隔离，默认 `true`
- `omp.bwrap_path` (str): Bubblewrap (bwrap) 可执行文件绝对路径，默认 `/usr/bin/bwrap`
- `omp.default_cwd` (str): 沙盒内唯一可写工作空间目录，默认 `/home/xiang/MaiBot/data/omp_workspace`
- `omp.default_model` (str): 指定模型，默认 `gemini-3.8-flash`
- `omp.export_html` (bool): 是否自动将执行结果导出为 HTML 网页文件并发送，默认 `true`
- `omp.progress_notify_seconds` (int): 超过该时长未结束时主动发送进度提醒（秒），默认 `300`（5分钟）
- `omp.timeout_seconds` (int): 单次任务最大硬超时时间（秒），默认 `900`（15分钟）
- `omp.auto_approve` (bool): 是否免交互自动授权工具调用，默认 `true`
- `omp.save_session` (bool): 是否保存会话记录以便后续 resume，默认 `true`
- `omp.max_reply_length` (int): 消息最大截断长度，默认 `1500`
