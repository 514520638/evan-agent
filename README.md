# Evan Agent

基于 Python、LangChain 和 MiniMax 的 AI Agent 起步项目，提供命令行多轮对话入口。

## 环境要求

- Python 3.11 或更高版本
- uv（推荐），或 Python 自带的 venv / pip
- MiniMax 开放平台 API Key，或有模型使用权限的 M Plan 订阅 Key

## 快速开始

```bash
uv sync
cp .env.example .env
```

首次使用时复制模板；已有 `.env` 时直接修改，避免覆盖现有密钥。填写：

```dotenv
MINIMAX_API_KEY=你的MiniMax密钥
MINIMAX_BASE_URL=https://api.minimax.cn/anthropic
MODEL=MiniMax-M3.1-Flash
NO_PROXY=api.minimax.cn,api.minimaxi.com
```

国内站使用上述地址；国际站密钥请将地址改为 `https://api.minimax.io/anthropic`。
模型填写原始 ID，无需 `openai:` 或 `anthropic:` 前缀。然后启动：

```bash
uv run evan-agent
```

请在项目根目录运行上述命令，程序从当前目录读取 `.env`。也可以运行
`uv run python -m evan_agent.main`。

如果不使用 uv：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
# 编辑 .env，填写 API Key 后启动
evan-agent
```

输入 `exit` 或 `quit` 结束对话。模型通过 `.env` 中的 `MODEL` 配置，默认是
`MiniMax-M3.1-Flash`。使用 `langchain-anthropic` 访问 MiniMax 的 Anthropic
兼容接口，认证与地址显式读取 `MINIMAX_API_KEY`、`MINIMAX_BASE_URL`。
原来的 `OPENAI_API_KEY` 不参与调用。

M Plan 用户请从开放平台获取订阅 Key；普通聊天产品的会员是否包含 API
使用权限，需要以所购套餐为准。Key、站点与模型权限应匹配。
请求失败时会显示 HTTP 状态和服务端错误原因（隐藏当前密钥），方便区分
认证失败、额度不足和模型权限问题。

若配置正确但出现 `Connection error` 或 `SSL: UNEXPECTED_EOF_WHILE_READING`，
检查终端的 `HTTP_PROXY` / `HTTPS_PROXY`。本项目使用国内接口时可通过上述
`NO_PROXY` 让 MiniMax 域名直连，其余代理设置继续生效。如果已有 `NO_PROXY`
列表，请在原列表中追加域名；终端已导出的环境变量优先于 `.env`。

参考：[MiniMax 兼容接口](https://platform.minimax.cn/docs/api-reference/text-anthropic-api)、
[订阅 Key 接入配置](https://platform.minimax.cn/docs/m-plan/other-tools)。

对话历史仅保存在当前进程内，退出即清空。Agent 使用 `create_agent`，
可以在 `src/evan_agent/main.py` 中添加工具与业务逻辑。

## 文件说明

- `pyproject.toml`：Python 项目元数据、依赖和命令行入口。
- `src/evan_agent/main.py`：Agent 创建与命令行对话。
- `.env.example`：环境变量模板，真实 `.env` 不提交到 Git。
- `uv.lock`：依赖版本锁文件，应提交到 Git。
- `pelican-bike.html`：独立的 SVG 鹈鹕骑行动画，可直接用浏览器打开。
