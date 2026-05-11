# feishu-cc

飞书 IM bridge for Claude Code CLI — 轻量 Python 替代品，将飞书消息直接转发给 Claude Code 子进程。

## 架构

![feishu-cc 架构](docs/architecture.jpg)

```
飞书用户 ⇄ Feishu WS ⇄ feishu-cc ⇄ Claude Code 子进程 (JSON stream)
```

每个 Bot 配置独立运行：各自的 Feishu WebSocket 连接、事件循环、Claude Code 子进程。

## 特性

- **多 Bot 支持** — 一个配置运行多个飞书 Bot，各自独立工作
- **Claude Code JSON 流协议** — 使用 `--output-format stream-json --input-format stream-json`，不解析终端 ANSI
- **飞书原生交互** — 卡片消息、Quick Replies 按钮、Reaction 表情
- **图片接收** — 自动下载飞书图片消息并转发给 Claude
- **实时工具调用反馈** — Claude 调用工具时实时推送通知到飞书
- **任务摘要通知** — Claude 完成任务后自动推送 ✅ 摘要
- **工具权限** — Claude 请求权限时通过飞书交互卡片让用户允许/拒绝
- **自动重启** — Claude 子进程崩溃后自动拉起，带指数退避
- **会话恢复** — session 持久化，重启后自动 `--resume`
- **工作目录切换** — 对话中发送 `/workspace <path>` 即可切换 Claude 工作目录
- **自定义 System Prompt** — 每个 Bot 可配置不同的 system prompt

## 安装

```bash
pip install feishu-cc
```

或从源码安装：

```bash
git clone https://github.com/MaoChen1980/feishu-cc.git
cd feishu-cc
pip install -e .
```

## 配置

创建 `~/.feishu-cc/config.json`。首次运行会自动生成模板然后退出：

```bash
feishu-cc
# → Template config created at ~/.feishu-cc/config.json. Please edit it.
```

编辑配置文件：

```json
{
  "bots": [
    {
      "name": "my-bot",
      "appId": "cli_xxxxxxxxxxxxxxxxxxxx",
      "appSecret": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
      "workspace": "/path/to/workspace",
      "system_prompt": null
    }
  ],
  "domain": "feishu",
  "claude_path": "claude",
  "render_mode": "card",
  "react_emoji": "THUMBSUP",
  "done_emoji": null
}
```

### 配置项

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `bots[].name` | string | — | Bot 名称，用于日志和 session 文件命名 |
| `bots[].appId` | string | — | 飞书应用 App ID |
| `bots[].appSecret` | string | — | 飞书应用 App Secret |
| `bots[].workspace` | string | null | Claude Code 工作目录（可选，默认启动目录） |
| `bots[].system_prompt` | string | null | 自定义 system prompt（可选） |
| `domain` | string | `"feishu"` | `"feishu"` 或 `"larksuite"` |
| `claude_path` | string | `"claude"` | `claude` CLI 路径或命令 |
| `render_mode` | string | `"card"` | `"card"` / `"post"` / `"auto"` |
| `react_emoji` | string | `"THUMBSUP"` | 消息处理中时的表情 |
| `done_emoji` | string | null | 消息处理完成后的表情（可选） |

配置项同时支持 camelCase（`appId`）和 snake_case（`app_id`）两种写法。

## 使用

```bash
# 默认配置 ~/.feishu-cc/config.json
feishu-cc

# 指定配置文件
feishu-cc --config /path/to/config.json

# 指定日志级别
feishu-cc --log-level DEBUG
```

### 多 Bot 示例

```json
{
  "bots": [
    {
      "name": "nanobot",
      "appId": "cli_aaaa",
      "appSecret": "secret_aaaa",
      "workspace": "/projects/nanobot",
      "system_prompt": "你是 nanobot 项目的助手。"
    },
    {
      "name": "helper",
      "appId": "cli_bbbb",
      "appSecret": "secret_bbbb",
      "workspace": null,
      "system_prompt": null
    }
  ]
}
```

每个 Bot 拥有独立的 Feishu 连接、Claude Code 子进程、workspace、session 和 system prompt。

## Quick Replies

Claude 的回复中可以通过 `---quick-replies` 提供一键按钮：

```
觉得如何？
---quick-replies
很好||analyze:positive
一般||analyze:neutral
很差||analyze:negative
```

按钮格式：
- `Label||发送内容` — 按钮显示 `Label`，点击后发送指定内容
- `Option` — 只写文字，按钮显示和发送内容相同
- 多个选项用 `|` 分隔或换行分开

## 飞书开放平台配置注意事项

使用 feishu-cc 时，飞书应用需使用 **长连接（WebSocket）** 方式接收事件，**不能填写 HTTP 回调 URL**。

### 常见问题

1. **服务器 URL 干扰** — 在飞书开放平台后台同时配置了服务器 URL（HTTP 回调）时，飞书可能优先将事件 POST 到该地址而非走 WebSocket。即使选择的是长连接，已填写的 URL 仍会干扰事件投递。**解决**：在飞书开放平台 → 你的应用 → 事件与回调 → 回调配置 中清空服务器 URL。

2. **事件订阅** — 确保已添加以下 Event：
   - `im.message.receive_v1`（接收消息）
   - `card.action.trigger`（卡片按钮回调）

3. **权限** — 确保应用已获取 `im:message`、`im:chat` 等必要的权限范围。

4. **图片** — 如需接收图片消息，添加 `im:resource` 权限。

## 测试

```bash
pip install pytest
pytest

# 带覆盖率
pytest --cov=feishu_cc
```

## 许可

MIT
