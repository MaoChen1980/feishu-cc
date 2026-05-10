# feishu-cc

飞书 IM bridge for Claude Code CLI — 轻量 Python 替代品，将飞书消息直接转发给 Claude Code 子进程。

## 架构

```
飞书用户 ⇄ Feishu WS ⇄ feishu-cc ⇄ claude 子进程 (JSON 流)
```

## 特性

- **多 Bot 支持** — 一个配置运行多个飞书 Bot，各自独立工作
- **Claude Code JSON 流协议** — 使用 `--output-format stream-json`，不解析终端 ANSI
- **飞书原生交互** — 卡片消息、Quick Replies 按钮、Reaction 表情
- **工具权限** — Claude 请求权限时通过飞书卡片让用户选择允许/拒绝
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
pip install .
```

## 配置

创建 `~/.feishu-cc/config.json`：

```json
{
  "bots": [
    {
      "name": "nanobot",
      "appId": "cli_xxxxxxxxxxxxxxxxxxxx",
      "appSecret": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
      "workspace": "/path/to/workspace",
      "system_prompt": null
    }
  ],
  "domain": "feishu",
  "claude_path": "claude",
  "render_mode": "card",
  "react_emoji": "THUMBSUP"
}
```

### 配置项

| 字段 | 说明 |
|------|------|
| `bots[].name` | Bot 名称，用于日志和 session 文件命名 |
| `bots[].appId` | 飞书应用 App ID |
| `bots[].appSecret` | 飞书应用 App Secret |
| `bots[].workspace` | Claude Code 工作目录（可选） |
| `bots[].system_prompt` | 自定义 system prompt（可选） |
| `domain` | `feishu` 或 `larksuite` |
| `claude_path` | `claude` CLI 路径 |
| `render_mode` | `card` / `post` / `auto` |
| `react_emoji` | 消息处理中时的表情 |
| `done_emoji` | 消息处理完成后的表情（可选） |

## 使用

```bash
# 默认配置 ~/.feishu-cc/config.json
feishu-cc

# 指定配置文件
feishu-cc --config /path/to/config.json

# 指定日志级别
feishu-cc --log-level DEBUG
```

## 多 Bot 示例

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
      "system_prompt": null
    }
  ]
}
```

每个 Bot 拥有独立的 Claude Code 子进程、workspace、session 和 system prompt。

## 许可

MIT
