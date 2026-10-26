# OMP Agent 插件 (github.haoxiangxia.omp-agent) v1.1.0

通过显式指令为用户提供调用宿主机 OMP (AI 编程与系统 Harness) 的能力，内置 Bubblewrap 内核沙盒强隔离、工作空间目录级 Session 隔离、产出物智能分流回传、独占并发拦截、外部 OMP 运行状态监听与 WebUI 全中文配置。

> MaiBot 插件 · 遵循 [maibot-plugin-sdk](https://github.com/Mai-with-u/maibot-plugin-sdk) 规范开发

## 特性概览
- **严格显式指令驱动**：Bot **绝不会自主**调用 OMP。日常闲聊或自然语言问答时，Bot 仅作常规对话，不会擅自唤起宿主机环境写代码。
- **目录级 Session 隔离**：会话列表、续写与导出彻底锁死在 `data/omp_workspace/` 专属目录内，杜绝跨项目会话越权混淆。
- **外部 OMP 智能监听与私聊推送**：
  - 支持后台异步监听宿主机外部（非 Bot 自身触发的终端命令）运行中的 OMP 任务；
  - **只在关键节点推送，常规读写保持 100% 静默**：
    1. **需要人工介入/确认**：外部 OMP 触发 `ask` 询问工具或决策时，私聊提醒用户回终端查看回复；
    2. **异常报错与卡点**：外部 OMP 遇到致命报错或工具调用失败时，私聊推送报错摘要；
    3. **最终任务完成交付**：外部 OMP 任务完成退出时，私聊发送完成提醒与终端恢复指令（纯文本静默推送，不发文件卡片）。
- **新产出物智能识别与自动回传**：
  - **单文件生成**：只生成了一个新文件时，直接作为 QQ 原生文件卡片发送，点开即用；
  - **多文件生成**：生成了多个文件时，自动汇总为 QQ 合并转发卡片发送（含文件清单与代码高亮预览），并后台上传至 QQ 文件，防止刷屏；
  - **无文件生成**：纯分析/查询任务直接回传分析结论文本。
- **任务并发独占与忙碌拦截**：为保护沙盒文件不被不同任务相互踩踏污染，OMP 采用独占模式。当前有任务运行时，新的下发会被自动拦截并发送忙碌卡片（含当前任务内容、发起人与已运行时长）；查询类指令（`/omp status`, `/omp help`, `/omp stop`, `/omp export`）保持畅通。
- **消息发送规则（极致防刷屏）**：
  - **即时启动回执**：单行普通文本直出；
  - **最终结果交付**：智能分流（单文件直发 / 多文件合并转发 / 文本直接呈现）；
  - **其余所有指令与状态反馈**（`/omp help`, `/omp status`, `/omp stop`, `/omp resume`, 5分钟超时进度, 报错）：**全部使用 QQ 合并转发卡片发送**，在群聊中仅占一行卡片，彻底避免刷屏。
- **内核级沙盒强隔离**：Bubblewrap (`bwrap`) 全盘只读保护 + 仅 `data/omp_workspace/` 单一可写工作区 + 凭据目录（`~/.ssh`, `~/.gnupg`）盲区遮蔽。
- **任务生命周期控制**：随时可通过 `/omp stop` 强行终止正在运行的任务并复位沙盒。
- **会话可续可查**：`/omp resume` 列出最近 5 次会话，支持通过 `/omp resume <序号/ID> <内容>` 沿用历史上下文继续对话。
- **免 TUN 模式代理直通**：支持配置 HTTP/SOCKS 代理地址（默认 `http://127.0.0.1:7897`），沙盒自动注入代理环境变量，无需开启 Linux TUN 虚拟网卡即可畅连 Google Gemini 与 OpenAI GPT。

## 安装
1. 将本仓库克隆或下载到 MaiBot 的 `plugins/` 目录下：
   ```bash
   git clone https://github.com/HaoxiangXia/maibot-omp-plugin.git plugins/maibot_omp_plugin
   ```
2. 重启 MaiBot（或在 WebUI 插件管理中重载），插件 `github.haoxiangxia.omp-agent` 会自动加载。
3. 依赖：宿主机需安装 [OMP](https://github.com/oh-my-pi/oh-my-pi) 与 `bwrap`（Bubblewrap）。

## 指令使用方法
- `/omp <任务内容>`：在隔离沙盒中向宿主机 OMP 下发执行任务，完成后发送简明提示（按需可通过 `/omp export` 导出 HTML 交互网页）。
- `/omp stop`：立即强行终止当前正在运行的 OMP 任务并复位沙盒环境。
- `/omp resume`：以合并转发卡片列出当前工作空间最近 5 个历史会话（序号、ID、时间与首条任务摘要），供选择继续。
- `/omp resume <序号/ID> <内容>`：沿用指定历史会话继续提问或写代码，OMP 会继承该会话的完整上下文（例如 `/omp resume 1 在刚才生成的网页上加一个本地持久化存储功能`）。
- `/omp export`：手动将当前工作空间最近一次 OMP 会话导出为 HTML 网页文件并发送。
- `/omp status`：以合并转发卡片查看当前运行状态（是否正在运行任务、PID、时长）、沙盒隔离与核心环境。
- `/omp help`：以合并转发卡片查看帮助指南与常用示例。

## 会话保存与 Resume 恢复
执行任务时会自动保存会话记录至 `~/.omp/agent/sessions/`，执行完成后 Bot 会在回复末尾附带对应的恢复指令（如 `📌 终端恢复: omp -r 01a17141`）。
- **在宿主机终端恢复与交互**：
  ```bash
  # 交互式选择历史会话
  omp -r

  # 直接指定会话 ID 继续或查看
  omp -r <session_id>

  # 继续最近一次会话
  omp --continue
  ```
- **在 QQ 聊天流中恢复与续写**：
  直接在聊天窗口发送：
  ```text
  /omp resume <序号或ID前缀> <续写指令>
  ```

## 配置项
可通过 WebUI 插件管理或 `config.toml` 进行多模块卡片化配置：
- **`[plugin]` (基础设置)**：
  - `enabled` (bool): 插件全局开关，默认 `true`
- **`[runtime]` (运行环境与模型)**：
  - `omp_path` (str): OMP 二进制路径，默认 `/home/xiang/.bun/bin/omp`
  - `default_model` (str): 默认模型，默认 `gemini-3.8-flash`
  - `default_cwd` (str): 沙盒内可写工作空间目录，默认 `/home/xiang/MaiBot/data/omp_workspace`
- **`[security]` (沙盒安全与网络代理)**：
  - `use_sandbox` (bool): 是否启用 Bubblewrap 内核沙盒隔离，默认 `true`
  - `bwrap_path` (str): Bubblewrap (bwrap) 可执行文件路径，默认 `/usr/bin/bwrap`
  - `proxy_url` (str): 网络代理地址，默认 `http://127.0.0.1:7897`（免 TUN 模式直连）
- **`[execution]` (任务执行与超时策略)**：
  - `auto_approve` (bool): 是否免交互自动授权工具调用，默认 `true`
  - `save_session` (bool): 是否保存历史会话记录以支持 resume 恢复，默认 `true`
  - `timeout_seconds` (int): 单次任务最大硬超时时间（秒），默认 `900`（15分钟）
  - `progress_notify_seconds` (int): 超过该时长未结束时主动发送进度提醒（秒），默认 `300`（5分钟）
  - `max_reply_length` (int): 消息最大截断长度，默认 `1500`
  - `export_html` (bool): 任务完成后是否自动导出 HTML 网页文件并发送，默认 `false`（仅在执行 `/omp export` 时按需导出）
- **`[notification]` (外部任务监听与通知)**：
  - `watch_external_sessions` (bool): 是否后台监听非 Bot 启动的外部 OMP 任务，默认 `true`
  - `notify_user_id` (str): 外部 OMP 任务关注事件推送的目标 QQ 号（私聊推送，默认留空不推送）
  - `export_external_html` (bool): 外部任务完成后是否自动导出 HTML 网页文件并私聊推送，默认 `false`
