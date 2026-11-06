# tg-ai-summarize-bot

A personal Telegram assistant that reads channels for you. It listens to public channels through your own Telegram account, sends LLM digests of what happened, reposts selected channels into one feed in real time, and draws a heatmap of the places mentioned in local alert channels.

```
Telegram channels ──Telethon (user client)──▶ listener ──▶ storage (JSON / DuckDB)
                                                 │                │
                                                 │ alert channels │ summary channels
                                                 ▼                ▼
                                       repost to target chat   LLM digest (Groq / Anthropic)
                                                 │
                                                 ▼
                              place extraction (LLM) → geocoded aliases → heatmap PNG
                                                 │
                              Telegram bot (python-telegram-bot) ◀── you
```

## Features

- **Digests.** `/summary [channel] [hours]` on demand, plus a scheduled daily digest. Messages are grouped by topic and formatted for Telegram.
- **Real-time repost.** Messages from "alert" channels (text, photos, video, voice, documents) are forwarded into one target channel as they arrive.
- **Alert heatmap.** An LLM extracts place names from alert-channel messages, they get geocoded into a `location_aliases` table and rendered as a heatmap (folium → PNG via Playwright). Recent mentions weigh more (exponential decay with a configurable half-life). `/startlive` keeps one message updating on a timer.
- **Verkhovna Rada bills monitor.** Scrapes newly registered bills from `itd.rada.gov.ua` on a schedule and stores them in DuckDB.
- **Chat mode.** Any non-command message goes to the LLM with per-user history. `/model` switches models on the fly.
- **Access code.** The bot only answers users who sent `AUTH_CODE` once.

## Stack

Python 3.10+ · Telethon · python-telegram-bot · Groq / Anthropic · DuckDB · folium + Playwright

## Setup

```bash
git clone https://github.com/mishfish/tg-ai-summarize-bot.git
cd tg-ai-summarize-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # needed for map rendering
cp .env.example .env                 # fill it in, see below
python main.py
```

On the first run Telethon asks for your phone number and login code; the session is saved to `session/user.session`.

### What goes into `.env`

| Variable | What it is |
|---|---|
| `TELEGRAM_TOKEN` | Bot token from @BotFather |
| `AUTH_CODE` | Code users send to unlock the bot |
| `API_ID`, `API_HASH`, `PHONE` | Your Telegram account, from my.telegram.org. Used to read channels |
| `LLM_PROVIDER` | `groq` or `anthropic` |
| `GROQ_API_KEY` / `ANTHROPIC_API_KEY` | Key for the chosen provider |
| `SYSTEM_PROMPT` | Prepended to every LLM call, e.g. "Always answer in Ukrainian" |
| `SUMMARY_TIME`, `SUMMARY_CHAT_ID` | When (UTC) and where to send the daily digest |
| `ALERT_TARGET_CHAT_ID` | Channel for reposts; the bot must be an admin there |
| `STORAGE_PROVIDERS` | `json`, `duckdb` or `json,duckdb` (writes go to both) |
| `LEGAL_MONITOR_TIME`, `LEGAL_MONITOR_INTERVAL_HOURS` | Bills scraper schedule |

## Commands

| Command | |
|---|---|
| `/channels`, `/add <ch>`, `/remove <ch>` | Manage channels for digests |
| `/summary [ch] [hours]` | Digest, default: all channels, last 24 h |
| `/alerts`, `/addalert <ch>`, `/removealert <ch>` | Manage real-time repost channels |
| `/settarget <chat_id>` | Where reposts go |
| `/fetchalerts [days]` | Backfill alert history (default 60 days) |
| `/map [days] [odesa\|mykolaiv]` | Heatmap of mentioned places |
| `/startlive [N m] [half-life h] [city]`, `/stoplive` | Self-updating map message |
| `/addalias name \| district \| city \| lat \| lon` | Add a location by hand |
| `/bills [pages]` | Scrape Rada bills now |
| `/model`, `/clear`, `/info` | Switch model, reset chat history, show settings |

## Building the place dictionary

The heatmap only knows places that are in `location_aliases`. Fill it once from history, then clean it up:

```bash
python extract_places.py --dry-run      # see what the LLM finds
python extract_places.py                # extract + geocode + save
python dedup_aliases.py                 # show duplicate groups
python dedup_aliases.py --apply         # merge them
```

## Project layout

```
main.py              entrypoint: bot + Telethon client + scheduled jobs
bot.py               command handlers
listener.py          Telethon event handlers, media detection
summarizer.py        digest prompts
llm.py               provider abstraction (Groq, Anthropic)
storage*.py          JSON / DuckDB persistence
alert_map.py         alerts + aliases in DuckDB, heatmap weights
map_render.py        folium map → PNG
extract_places.py    LLM place extraction + geocoding (CLI)
dedup_aliases.py     LLM-assisted alias dedup (CLI)
legal_monitor.py     Verkhovna Rada bills scraper
test_ollama.py       experiments with a local model via Ollama
tests/               pytest
```

## Tests

```bash
pytest
```
