# OMP Agent 插件 (maibot-team.omp-agent)

通过显式指令为用户提供调用宿主机 OMP (AI 编程与系统 Harness) 的能力。

## 核心设计与使用规范
- **严格显式指令驱动**：Bot **绝不会自主**调用 OMP。日常闲聊或自然语言问答（如“我想写个网页/写个HTML”）时，Bot 仅作常规对话或普通建议，不会擅自唤起宿主机环境写代码。
- **唯一触发途径**：只有当用户在私聊或群聊中明确发送 `/omp` 开头的命令时，才会触发 OMP 任务执行。

## 指令使用方法
- ` /omp <任务内容>`：向宿主机 OMP 下发执行任务并等待回传输出（例如 `/omp 帮我把当前目录的 README.md 补充一行说明` 或 `/omp 检查当前 git 分支状态`）。
- `/omp status`：查看宿主机 OMP 的可执行文件绝对路径、版本号、默认工作目录及超时参数。
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
- **导出网页记录**：
  ```bash
  omp --export ~/.omp/agent/sessions/-MaiBot/<session_file>.jsonl
  ```

## 配置项
可通过 WebUI 插件管理或 `config.toml` 配置：
- `plugin.enabled` (bool): 插件全局开关，默认 `true`
- `omp.omp_path` (str): OMP 二进制文件绝对路径，默认 `/home/xiang/.bun/bin/omp`
- `omp.default_cwd` (str): 默认执行目录，默认 `/home/xiang/MaiBot`
- `omp.default_model` (str): 指定模型（留空使用 OMP 自身当前默认配置）
- `omp.timeout_seconds` (int): 单次任务最大超时时间（秒），默认 `180`
- `omp.auto_approve` (bool): 是否免交互自动授权工具调用，默认 `true`
- `omp.save_session` (bool): 是否保存会话记录以便后续 resume，默认 `true`
- `omp.max_reply_length` (int): 消息最大截断长度，默认 `1500`
