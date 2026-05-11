# CLAUDE.md

feishu-cc 是 Claude Code 与飞书之间的双向实时桥接工具，已完全替代 cc-connect。

## 快速开始

```bash
# 安装（可编辑开发模式）
pip install -e .

# 运行（需要先配置 ~/.feishu-cc/config.json）
feishu-cc
feishu-cc --log-level DEBUG
feishu-cc --config /path/to/config.json

# 测试
pytest
pytest tests/test_config.py::TestConfig::test_create_template -v
pytest --cov=feishu_cc

# 类型检查
mypy src/feishu_cc/
```

## 最新功能特性

### 实时事件推送
Claude 的所有中间状态实时推送到飞书，无需等待最终回复：

| 事件类型 | 飞书显示 | 说明 |
|---------|---------|------|
| `text` | 逐流输出文字 | Claude 实时生成文本 |
| `thinking` | 💭 思考过程 | 模型内部推理过程 |
| `tool_use` | 📖 Read … / 💻 Bash … | 工具调用通知（带 emoji 图标） |
| `tool_result` | 📊/❌ 结果摘要 | 工具执行结果（截断至 100 字符） |
| `system` | ✅ [status] summary | 系统状态事件 |
| `error` | ❌ type: message | 错误事件通知 |
| `result` | ✅ 完成摘要 | 最终结果的内容概要 |

### 智能 Crash 恢复
- 进程崩溃后自动重启，最多重试 3 次
- 指数退避等待（2s → 4s → 8s，最大 30s）
- `_response_gen` 生成计数器过滤过期事件，防止旧消息干扰新会话

### 多 Bot 并行
- 每 Bot 独立线程 + asyncio 事件循环
- 独立 Claude 子进程 + 独立飞书 WS 连接
- 跨线程通信通过 `asyncio.run_coroutine_threadsafe`

### 交互式权限审批
- Claude 需要执行操作时，飞书卡片弹出 Allow / Deny 按钮
- 支持操作级别的精细控制（allow_once / allow_this_time / deny）

### 消息去重与格式
- 飞书消息 30s 窗口去重
- 自动选择消息格式：富文本卡片 > 帖子 > 纯文本
- `---quick-replies` 转为卡片快捷按钮
- 支持 `/workspace <path>` 命令切换工作目录

## 架构

```
飞书用户 ⇄ Feishu WS ⇄ feishu-cc ⇄ claude 子进程 (JSON stream protocol)
```

```
Feishu IM
   ↓ WebSocket (lark_oapi)
FeishuClient — 消息处理、去重、发送
   ↓ on_message 回调
_BotRuntime — 每 Bot 独立线程 + asyncio 事件循环
   ↓ send_message
ClaudeBridge — 子进程管理、JSON stream 解析、事件分发
   ↓ stdin/stdout (stream-json)
Claude Code CLI
```

### 核心文件

| 文件 | 职责 |
|------|------|
| `src/feishu_cc/__main__.py` | CLI 入口、参数解析、loguru 配置 |
| `src/feishu_cc/config.py` | `Config`/`BotConfig` 数据类、JSON 加载、自动模板生成 |
| `src/feishu_cc/feishu_client.py` | lark_oapi WebSocket 客户端、消息/卡片处理、去重、发送 |
| `src/feishu_cc/claude_bridge.py` | Claude 子进程管理、JSON stream 协议解析、事件分发 |
| `src/feishu_cc/app.py` | `FeishuCCApp` 编排器、`_BotRuntime` 每 Bot 线程/循环容器 |

### JSON Stream 协议

与 Claude CLI 通过 `--output-format stream-json --input-format stream-json` 通信：

- **stdin（→ claude）：** `{"type":"user","message":{"role":"user","content":text}}`
- **stdout（← claude）：** 每行一个 JSON，包含事件类型分发到各回调
- **权限响应：** `{"type":"control_response","response":{"subtype":"success","request_id":"...","response":{"behavior":"allow|deny"}}}`
- **会话持久化：** `~/.feishu-cc/sessions/{bot_name}.session` 支持 `--resume`

## 配置

默认路径：`~/.feishu-cc/config.json`。首次运行自动生成模板（然后抛出 `FileNotFoundError`，用户必须编辑模板）。JSON 中同时支持 camelCase（`appId`）和 snake_case（`app_id`）键名。

## 已知问题

`test_create_template` 对已安装的（过时）包运行会失败。先运行 `pip install -e .` 再 `pytest`。
