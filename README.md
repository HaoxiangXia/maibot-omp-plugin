# OMP Agent 插件 (maibot-team.omp-agent)

通过显式指令为用户提供调用宿主机 OMP (AI 编程与系统 Harness) 的能力，内置 Bubblewrap 内核沙盒强隔离与内置 HTML 交互网页发送。

> MaiBot 插件 · 遵循 [maibot-plugin-sdk](https://github.com/Mai-with-u/maibot-plugin-sdk) 规范开发

## 特性概览
- **严格显式指令驱动**：Bot **绝不会自主**调用 OMP。日常闲聊或自然语言问答（如“我想写个网页/写个HTML”）时，Bot 仅作常规对话或普通建议，不会擅自唤起宿主机环境写代码。
- **内核级沙盒强隔离**：Bubblewrap (`bwrap`) 全盘只读保护 + 单一可写工作区 + 凭据目录盲区遮蔽。
- **会话可续可查**：`/omp resume` 列出最近会话并支持沿用历史上下文继续对话。
- **HTML 交互网页回传**：任务完成后利用 OMP 内置 `--export` 生成自包含交互网页，通过 QQ 原生文件卡片直接送达。

## 安装
1. 将本仓库克隆或下载到 MaiBot 的 `plugins/` 目录下：
   ```bash
   git clone https://github.com/HaoxiangXia/maibot-omp-plugin.git plugins/maibot_omp_plugin
   ```
2. 重启 MaiBot（或在 WebUI 插件管理中重载），插件 `maibot-team.omp-agent` 会自动加载。
3. 依赖：宿主机需安装 [OMP](https://github.com/oh-my-pi/oh-my-pi) 与 `bwrap`（Bubblewrap）。

## 核心设计与使用规范
- **严格显式指令驱动**：Bot **绝不会自主**调用 OMP。日常闲聊或自然语言问答（如“我想写个网页/写个HTML”）时，Bot 仅作常规对话或普通建议，不会擅自唤起宿主机环境写代码。
- **唯一触发途径**：只有当用户在私聊或群聊中明确发送 `/omp` 开头的命令时，才会触发 OMP 任务执行。
- **内核级沙盒强隔离**：
  - 默认启用 Linux 原生 **Bubblewrap (`bwrap`) 沙盒**。
  - **全系统强制只读**：宿主机根目录、系统二进制、依赖库和主目录默认全盘只读（只读保护，禁止任何写、改、删）。
  - **唯一可写工作区**：仅有 `data/omp_workspace/` 目录允许写入和修改，防止篡改 MaiBot 自身文件或宿主机系统。
  - **隐私目录遮蔽**：`~/.ssh`、`~/.gnupg` 挂载为空白内存盘，防止私钥等凭据泄露。
- **长时间任务状态跟踪与 HTML 网页回传**：
  - 下发命令后立即回执 `⏳ OMP 任务已在沙盒中启动...`；
  - 若任务执行超过 5 分钟，Bot 会向聊天流主动发送进度提醒，OMP 保持继续执行不中断；
  - **任务完成后，自动利用 OMP 内置的 `--export` 功能将完整对话、思考过程与代码高亮打包为自包含的 HTML 交互网页文件，并通过文件消息直接发送回聊天流**。

## 指令使用方法
- `/omp <任务内容>`：在隔离沙盒中向宿主机 OMP 下发执行任务，完成后自动导出并发送 HTML 交互网页文件（例如 `/omp 写一个猜数字的小游戏`）。
- `/omp resume`：列出最近 5 个历史会话（序号、ID、时间与首条任务摘要），供选择继续。
- `/omp resume <序号/ID> <内容>`：沿用指定历史会话继续提问或写代码，OMP 会继承该会话的完整上下文（例如 `/omp resume 1 在刚才生成的网页上加一个本地持久化存储功能`）。
- `/omp export`：手动将最近一次 OMP 会话导出为 HTML 网页文件并发送。
- `/omp status`：查看核心运行环境（插件状态、OMP 引擎路径与版本、默认模型）。
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
