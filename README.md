# Assistant

[![CI](https://github.com/opbenesh/todoist-assistant/actions/workflows/ci.yml/badge.svg)](https://github.com/opbenesh/todoist-assistant/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)

A personal Telegram bot that connects [Todoist](https://todoist.com) and [Obsidian](https://obsidian.md), using Claude to help plan days, break down blocked tasks, and surface insights.

**Single-user, personal tool.** Run on your own machine against your own accounts.

## Commands

| Command | Description |
|---|---|
| `/plan` | Time-block your day: brainstorm → triage → write a plan to your Obsidian daily note |
| `/unblock` | Break a stuck task into actionable subtasks |
| `/brainstorm` | Free-form brainstorm → Todoist capture |
| `/insights` | Weekly digest of completed tasks and usage patterns |
| `/session` | Review the current planning session state |

## Stack

- Python 3.12+, [uv](https://docs.astral.sh/uv/)
- [python-telegram-bot](https://python-telegram-bot.org/) v20+ (async)
- [Anthropic SDK](https://docs.anthropic.com/) — Claude Haiku (fast tasks) + Sonnet (planning)
- [Todoist API](https://developer.todoist.com/)
- Obsidian vault (local filesystem, markdown)
- PM2 for process management

## Requirements

- Telegram bot token ([BotFather](https://t.me/botfather))
- Todoist API key
- Anthropic API key
- Obsidian vault on the same machine

## Setup

```bash
git clone https://github.com/opbenesh/todoist-assistant
cd todoist-assistant
uv sync
cp .env.example .env  # fill in your keys
uv run python main.py
```

Or with PM2:
```bash
pm2 start ecosystem.config.cjs
```

## Environment variables

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_KEY` | Telegram bot token |
| `TODOIST_KEY` | Todoist API token |
| `ANTHROPIC_KEY` | Anthropic API key |
| `TELEGRAM_USER_ID` | Your Telegram user ID (whitelist — bot ignores all other users) |
| `VAULT_PATH` | Absolute path to your Obsidian vault (default: `~/vault`) |

## Development

```bash
uv run python cli.py          # interactive REPL — mirrors the bot without Telegram auth
uv run pytest -m "not e2e"   # unit tests
uv run pytest -m e2e         # end-to-end tests (launches real bot against fake services)
uv run ruff check .           # lint
uv run ruff format .          # format
```

E2E tests spin up fake Telegram, Todoist, and LLM servers and run the real bot as a subprocess — no external accounts needed.

## License

MIT
