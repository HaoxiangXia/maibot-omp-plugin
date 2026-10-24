# OMP Agent 插件 (maibot-team.omp-agent)

为麦麦提供调用宿主机 OMP (AI 编程与系统 Harness) 的能力。

## 特性
- **自主工具调用 (`@Tool omp_execute`)**：麦麦（Devil）在对话规划中可根据需要自主调用 OMP 执行代码编写、文件检索、系统诊断或终端任务。
- **用户直通命令 (`@Command /omp`)**：
  - `/omp <任务描述>`：直接将任务下发给 OMP 执行并回传结果。
  - `/omp status`：查看 OMP 可执行文件路径、版本与配置状态。
  - `/omp help`：查看帮助指南。
- **免交互与超时保护**：支持 `--auto-approve` 自动授权，具备可配置的超时强杀与字符数自动截断机制。

## 配置项
可通过 WebUI 插件管理或 `config.toml` 配置：
- `omp.enabled` (bool): 是否启用插件，默认 `true`
- `omp.omp_path` (str): OMP 可执行文件绝对路径，默认 `/home/xiang/.bun/bin/omp`
- `omp.default_cwd` (str): 默认工作目录绝对路径，默认 `/home/xiang/MaiBot`
- `omp.default_model` (str): 指定模型名称（留空使用 OMP 自身当前默认配置）
- `omp.timeout_seconds` (int): 单次任务执行超时时间（秒），默认 `180`
- `omp.auto_approve` (bool): 是否自动授权工具调用，默认 `true`
- `omp.max_reply_length` (int): 消息最大截断长度，默认 `1500`
