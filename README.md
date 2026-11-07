# OMP Agent 插件 (github.haoxiangxia.omp-agent) v1.2.1

通过显式指令为用户提供调用宿主机 OMP (AI 编程与系统 Harness) 的能力，内置 Bubblewrap 内核沙盒强隔离、工作空间目录级 Session 隔离、全新 Ephemeral RPC-UI 流式双向交互、产出物智能分流回传、独占并发拦截、基于原生 TS 扩展的外部 OMP 运行状态监听与 WebUI 全中文配置。

> MaiBot 插件 · 遵循 [maibot-plugin-sdk](https://github.com/Mai-with-u/maibot-plugin-sdk) 规范开发

## 特性概览
- **严格显式指令驱动**：Bot **绝不会自主**调用 OMP。日常闲聊或自然语言问答时，Bot 仅作常规对话，不会擅自唤起宿主机环境写代码。
- **Ephemeral RPC-UI 流式双向交互**：
  - 底层使用短驻 `bwrap ... omp --mode rpc-ui` 进程，通过 stdio JSONL 协议驱动，自动协商 Protocol v2 支持超长分片传输（`rpc_chunk` 最大支持 64MB 输出），彻底告别终端 ANSI 码清洗。
- **双层动态心跳看门狗（解除死板超时限制）**：
  - **心跳守护**：只要任务持续有脉搏（流式吐字、工具调用），任务可持续运行数十分钟至一小时，不再被死板的 15 分钟硬超时截断；
  - **单工具卡死检测（默认 300s）**：单个工具或命令执行悬挂超过阈值时，自动判定为卡死并触发 `abort` 优雅取消；
  - **协议静默死锁检测（默认 300s）**：数据流连续静默无响应超过 5 分钟时，判定为网络或模型推理死锁并触发 `abort`；
  - **防死循环熔断兜底**：最大总时长（默认 3600s / 1 小时）与最大工具调用次数（默认 100 次）兜底保护。
- **RPC 周期性高信息量进展卡片**：每隔指定时长（默认 300s / 5分钟）向群聊/私聊发送结构化合并转发卡片，实时汇报当前具体执行工具与动作、工具调用累计统计与最新模型思考意图。
- **双向交互与 `ask` 决策窗口**：
  - 当 OMP 执行遇到 `ask` 工具提问时，任务进入等待决策状态，向任务发起人推送决策卡片；
  - 任务发起人可通过**私聊直接回复**，或在**群聊中引用回复卡片 / 输入 `/omp a <回答>`** 进行精准决策注入；
  - **120 秒超时主动 abort 纪律**：超过 120 秒未收到答复，绝不盲猜，主动下发 RPC `abort` 终止任务并告警，立即释放沙盒与 Session 锁。
- **跨环境 Session 强互斥锁**：严格执行“同一时间一个 Session 绝不允许打断”。无论外部终端还是 QQ 正在运行某个 Session，并发 resume 请求一律直接拒绝，防止历史损坏。
- **轻量 OMP 原生扩展与 Socket 监听**：
  - 弃用磁盘文件轮询，改为在 `~/.omp/agent/extensions/` 自动部署 `maibot-bridge.ts`；
  - 外部终端执行 `omp` 时，通过 `/tmp/maibot-omp-bridge.sock` 毫秒级推送外部会话状态（等待输入、报错、完成，附带前 50 字内容摘要）；
  - 内置 50ms 超时与静默 fail-open 旁路保护，MaiBot 离线时外部终端绝对零感知、零阻塞。
- **新产出物智能识别与自动回传**：
  - **单文件生成**：只生成了一个新文件时，直接作为 QQ 原生文件卡片发送，点开即用；
  - **多文件生成**：生成了多个文件时，自动汇总为 QQ 合并转发卡片发送（含文件清单与代码高亮预览），并后台上传至 QQ 文件，防止刷屏；
  - **无文件生成**：纯分析/查询任务直接回传分析结论文本。
- **任务并发独占与忙碌拦截**：为保护沙盒文件不被不同任务相互踩踏污染，OMP 采用独占模式。当前有任务运行时，新的下发会被自动拦截并发送忙碌卡片（含当前任务内容、发起人与已运行时长）；查询类指令保持畅通。
- **消息发送规则（极致防刷屏）**：
  - **即时启动回执**：单行普通文本直出；
  - **最终结果交付**：智能分流（单文件直发 / 多文件合并转发 / 文本直接呈现）；
  - **其余所有指令与状态反馈**：全部使用 QQ 合并转发卡片发送，彻底避免刷屏。
- **内核级沙盒强隔离**：Bubblewrap (`bwrap`) 全盘只读保护 + 仅 `data/omp_workspace/` 单一可写工作区 + 凭据目录（`~/.ssh`, `~/.gnupg`）盲区遮蔽。
- **任务生命周期控制**：随时可通过 `/omp stop` 优雅软中断（RPC `abort`）正在运行的任务并复位沙盒。
- **免 TUN 模式代理直通**：支持配置 HTTP/SOCKS 代理地址（默认 `http://127.0.0.1:7897`），沙盒自动注入代理环境变量，无需开启 Linux TUN 虚拟网卡即可畅连 Google Gemini 与 OpenAI GPT。

## 兼容性

- **MaiBot 主程序**：`1.3.0` – `1.4.x`。manifest 的 `max_version = 1.4.99` 是插件市场要求的三段式写法，表示 1.4 全系列。
- **插件 SDK**：`2.0.0` 及以上（`2.x`）。
- **宿主机**：需安装 [OMP](https://github.com/oh-my-pi/oh-my-pi) 与 `bwrap`（Bubblewrap）。
- **许可证**：GPL-3.0-or-later。

## 指令使用方法
- `/omp <任务内容>`：在隔离沙盒中向宿主机 OMP 下发执行任务，通过 RPC 结构化驱动。
- `/omp a <回答>` 或 `/omp answer <回答>`：向正在等待人类决策的 OMP 任务注入答复（群聊中也可直接引用回复决策卡片）。
- `/omp stop`：立即向当前任务下发 `abort` 软中断并安全复位沙盒环境。
- `/omp resume`：以合并转发卡片列出当前工作空间最近 5 个历史会话（序号、ID、时间与首条任务摘要），供选择继续。
- `/omp resume <序号/ID> <内容>`：沿用指定历史会话继续提问或写代码（受跨环境互斥锁保护，正在运行的会话不可打断）。
- `/omp export`：手动将当前工作空间最近一次 OMP 会话导出为 HTML 网页文件并发送。
- `/omp status`：以合并转发卡片查看当前运行状态（是否正在运行任务、PID、时长、当前工具、双层看门狗保护状态）、沙盒隔离与核心环境。
- `/omp help`：以合并转发卡片查看帮助指南与常用示例。

## 配置项
可通过 WebUI 插件管理或 `config.toml` 进行 5 个卡片的模块化配置；`config.example.toml` 是带注释的模板，初次部署直接复制即可：
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
  - `tool_hang_timeout_seconds` (int): 单工具执行卡死超时（秒），默认 `300`（5分钟）
  - `idle_timeout_seconds` (int): 协议流静默无响应超时（秒），默认 `300`（5分钟）
  - `max_total_seconds` (int): 任务最大执行总时长熔断（秒），默认 `3600`（1小时）
  - `max_tool_calls` (int): 单任务最大工具调用总次数熔断，默认 `100`
  - `ask_timeout_seconds` (int): 人类决策等待窗口（秒），默认 `120`
  - `progress_notify_seconds` (int): 超过该时长未结束时主动发送进度提醒（秒），默认 `300`（5分钟）
  - `max_reply_length` (int): 消息最大截断长度，默认 `1500`
  - `export_html` (bool): 任务完成后是否自动导出 HTML 网页文件并发送，默认 `false`（仅在执行 `/omp export` 时按需导出）
- **`[notification]` (外部任务监听与通知)**：
  - `watch_external_sessions` (bool): 是否后台监听非 Bot 启动的外部 OMP 任务，默认 `true`
  - `bridge_socket_path` (str): Unix Domain Socket 监听路径，默认 `/tmp/maibot-omp-bridge.sock`
  - `notify_user_id` (str): 外部 OMP 任务关注事件推送的目标 QQ 号（私聊推送，留空则不推送；模板默认留空，请按需填写）
  - `export_external_html` (bool): 外部任务完成后是否自动导出 HTML 网页文件并私聊推送，默认 `false`
