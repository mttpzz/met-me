<table width="100%">
<tr>
<td width="110"><img src="bot.png" width="110" alt="Mac bot profile"></td>
<td>
<h1>met-me</h1>
<em>A Telegram bot for emotional support.</em>
</td>
</tr>
</table>



**met-me** is an LLM-powered conversational assistant (Claude or Ollama, hot-swappable) designed to offer a safe and welcoming listening space. Configurable persona, knowledge base via RAG, emotional state tracking (*rock-bottom tracker*) with admin alerts, and on-demand web search via tool calling.

The bot is called **Mac** — *"Be you. You'll be fine."*

---

## ✨ Features

- 🤖 **Two LLM providers**: Anthropic Claude (cloud) and Ollama (local), switchable at runtime via `/model`.
- 📚 **Document RAG**: admins drag-and-drop files (`.txt`, `.md`, `.pdf`, `.docx`) into the chat to enrich the knowledge base. Incremental indexing via LlamaIndex + Chroma + Ollama embeddings.
- 💚 **Rock-bottom tracker**: every user message is scored 0–10 by a separate LLM (0 = "rock bottom", 10 = positive). When the rolling average drops below threshold, admins receive an alert with the lowest-scoring messages.
- 🔧 **Tool calling**: the model can invoke `search_web` (DuckDuckGo via `ddgs`) when up-to-date info is needed. Tool-agnostic layer with adapters for both Anthropic and Ollama.
- 🗄️ **SQLite persistence**: users, conversation history, rock-bottom scores, settings. No in-memory state.
- 📝 **Structured logging**: rotating app-wide log + one file per user (`logs/{user_id}.log`) with fixed-width `type | model | text` columns.
- 🎭 **Configurable persona**: name, welcome, prompt, visible commands — all in `persona.yaml`.
- ⚙️ **No magic defaults**: every tunable (chunk size, top-k, rock-bottom threshold, history window…) lives in `.env`. Missing var = fail-fast on startup.

---

## 🏗️ Architecture

```
┌─────────────┐      ┌──────────────────────────────┐
│  Telegram   │◄────►│  bot.py (handlers + routing) │
└─────────────┘      └──────────────┬───────────────┘
                                    │
       ┌────────────┬───────────────┼─────────────────────┬──────────────┐
       ▼            ▼               ▼                     ▼              ▼
   ┌────────┐  ┌─────────┐    ┌───────────┐    ┌────────────────┐  ┌──────────┐
   │ db.py  │  │ llm.py  │    │  rag.py   │    │ rock_bottom.py │  │ tools.py │
   │SQLite  │  │Claude / │    │LlamaIndex │    │ scoring        │  │search_web│
   │        │  │Ollama   │    │+ Chroma   │    │ + alert        │  │  (DDG)   │
   └────────┘  └─────────┘    └───────────┘    └────────────────┘  └──────────┘
```

| File | Role |
|------|------|
| [bot.py](bot.py) | Entry point: Telegram handlers, routing, logging, bootstrap |
| [config.py](config.py) | Loads `.env` + `persona.yaml`, fail-fast on missing vars |
| [db.py](db.py) | Async SQLite (worker thread), schema `users` / `messages` / `rock_bottom_scores` / `settings` |
| [llm.py](llm.py) | Claude and Ollama providers behind a common interface, tool-use loop |
| [rag.py](rag.py) | Incremental ingestion with manifest, queries against Chroma |
| [rock_bottom.py](rock_bottom.py) | 0–10 classifier via LLM, robust JSON parsing |
| [tools.py](tools.py) | Tool-agnostic registry + Anthropic/Ollama adapters |
| [persona.yaml](persona.yaml) | Name, welcome, commands, system prompt |

---

## 🚀 Quick start

### Prerequisites

- Python ≥ 3.11
- [Ollama](https://ollama.com/) running locally (used both for RAG embeddings and as the optional local provider)
- Telegram bot token from [@BotFather](https://t.me/BotFather)
- (Optional) Anthropic API key for Claude

### Installation

```bash
git clone https://github.com/mttpzz/met-me.git
cd met-me

python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

### Ollama models

```bash
# Embedding model for RAG
ollama pull nomic-embed-text

# Local chat model (only if you plan to use the ollama provider)
ollama pull llama3.2
```

### Configuration

Create `.env` in the project root:

```env
# ---- Telegram ----
TELEGRAM_TOKEN=123456:ABC-DEF...
# Comma-separated Telegram user IDs allowed to run admin commands
ADMIN_IDS=111111111,222222222

# ---- Anthropic (Claude) ----
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-6
# Max output tokens per response
CLAUDE_MAX_TOKENS=1024

# ---- Ollama (local) ----
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=llama3.2

# ---- Defaults ----
# Initial provider: "claude" or "ollama"
DEFAULT_MODEL=claude

# ---- Paths ----
PERSONA_FILE=persona.yaml
DB_PATH=db/met-me.db

# ---- History ----
# Number of (user+assistant) turn pairs replayed to the LLM each request
HISTORY_MAX_TURNS=20

# ---- RAG ----
EMBEDDING_MODEL=nomic-embed-text
RAG_PATH=rag
# Top-K document chunks retrieved per turn
RAG_TOP_K=5
# Chunk size / overlap in tokens (chunk must fit within RAG_EMBED_NUM_CTX)
RAG_CHUNK_SIZE=256
RAG_CHUNK_OVERLAP=32
# Embed model context window (nomic-embed-text supports up to 8192)
RAG_EMBED_NUM_CTX=8192

# ---- Rock-bottom tracker ----
# Rolling window size and threshold (0..10). Alert fires when avg drops below
# threshold AND the window is full. 0 = "rock bottom".
ROCK_BOTTOM_WINDOW=10
ROCK_BOTTOM_THRESHOLD=4.0
# Lowest-scoring recent user messages attached to each alert
ROCK_BOTTOM_ALERT_LOW_MSGS=10

# ---- Account deletion ----
# How long (seconds) a /forget confirmation stays valid before it expires.
FORGET_CONFIRM_TIMEOUT_SECONDS=120

# ---- Consent (GDPR art. 9 for special-category data) ----
# Bump this string whenever PRIVACY.md or the consent wording changes
# substantively: users will be re-prompted for explicit consent.
CONSENT_VERSION=1.0
# Public URL of the privacy policy shown inside the consent prompt.
PRIVACY_POLICY_URL=https://github.com/mttpzz/met-me/blob/master/PRIVACY.md

# ---- Rate limit (burst, anti-DoS) ----
# A user can send at most COUNT text messages in any WINDOW-second window.
# Excess messages are dropped (not sent to the LLM, not persisted).
# In-memory only: bot restart resets all counters.
RATE_LIMIT_BURST_COUNT=5
RATE_LIMIT_BURST_WINDOW_SECONDS=10

# ---- Error tracking (Sentry, optional) ----
# Leave SENTRY_DSN empty to disable Sentry entirely (no SDK init, no data
# leaves the bot). When enabling, complete the GDPR steps documented in .env
# before pasting the DSN: declare Sentry as a sub-processor in PRIVACY.md and
# bump CONSENT_VERSION so existing users re-consent.
SENTRY_DSN=
SENTRY_ENVIRONMENT=production
```

### Run

```bash
python bot.py
```

On the first run the SQLite schema is created, the `rag/docs/` folder is indexed (if it contains files), and the bot's Telegram profile is synced.

---

## 💬 Commands

### User

| Command | What it does |
|---------|--------------|
| `/start` | Welcome message |
| `/help` | Command list |
| `/reset` | Clear the user's conversation history |
| `/privacy` | Show what data is stored and how to delete it *(GDPR — right to be informed, art. 13)* |
| `/export` | Download a ZIP of four CSV files (profile, consents, messages, rock-bottom scores) with the user's data *(GDPR — right to data portability, art. 20)* |
| `/feedback <message>` | Forward a one-off feedback message to every admin |
| `/forget` | Permanently delete the user's profile, messages, scores, and log file. Requires confirmation by typing the phrase shown by the bot *(GDPR — right to erasure, art. 17)* |

### Admin

| Command | What it does |
|---------|--------------|
| `/model` | Show the active model |
| `/model claude\|ollama` | Switch provider |
| `/users` | List registered users |
| `/stats` | Statistics: users, messages per role/model, top users |
| `/reindex` | Full re-indexing of `rag/docs/` |
| `/ban <user_id> [reason]` | Block a user from interacting with the bot. Bans survive `/forget` (anti-abuse, legitimate interest art. 6(1)(f) GDPR) |
| `/unban <user_id>` | Lift a ban |
| `/bans` | List currently banned users |
| `/audit` | Show the most recent admin actions (accountability — art. 24/32 GDPR). Every admin command writes a row to the `admin_audit` table; reading the log is itself logged. |

**Document upload**: any file sent by an admin in chat is dropped into `rag/docs/` and automatically indexed.

---

## 🧠 Customizing the bot

The whole personality lives in [persona.yaml](persona.yaml):

```yaml
name: Mac
short_description: Be you. You'll be fine.
welcome: |
  Hi, my name is Mac 👋
help_user:
  - /start - welcome message
  - /help - this list
  - /reset - clear the conversation history
  - /privacy - what data is stored and how to delete it
  - /forget - permanently delete all your data
help_admin:
  - /model - show the active model
  - /model claude|ollama - switch model
prompt: |
  Role: act as an emotional support assistant...
```

Change name, welcome, and prompt to get an assistant with a completely different tone (coach, tutor, customer support, etc.). On restart the bot automatically syncs its Telegram profile (with dedup to avoid the aggressive rate limits of the profile APIs).

---

## 🔒 Safety & use notes

The bot **is not a substitute for a mental health professional**. The system prompt includes explicit instructions to:

- Recognize acute-crisis signals and point the user to emergency contacts
- Avoid diagnoses and clinical language
- Validate before suggesting

Admins receive proactive alerts when a user's rolling emotional-state average drops below threshold, with the lowest-scoring recent messages attached for context. Use responsibly.

---

## 🛡️ Privacy & GDPR

The bot stores in a local database: Telegram ID, username, first/last name (if set), every message exchanged, and a numeric (`rock-bottom`) score for each user message. Messages are sent to the configured LLM provider (Anthropic for the `claude` provider) to generate the reply.

Because conversations about emotional well-being qualify as **special-category personal data** (GDPR art. 9 — data concerning mental health), the bot enforces explicit, versioned consent. The full privacy policy lives at [PRIVACY.md](PRIVACY.md).

GDPR rights wired into the bot:

- **Art. 9 — explicit consent for special-category data**: on first interaction (and after any `CONSENT_VERSION` bump) the bot shows the consent prompt with inline accept/decline buttons. No message is processed, stored, or sent to the LLM until consent is recorded. Consent records (user_id, version, granted_at) live in the dedicated `consents` SQLite table.
- **Art. 13 — right to be informed**: `/privacy` shows a summary; the full policy is at [PRIVACY.md](PRIVACY.md).
- **Art. 17 — right to erasure**: `/forget` permanently deletes the user profile, messages, scores, consent records, and dedicated log file. The user must confirm by typing back a phrase shown by the bot (configurable in `persona.yaml` as `forget_confirm_phrase`); the pending confirmation expires after `FORGET_CONFIRM_TIMEOUT_SECONDS`. The operation is irreversible.
- **Art. 20 — right to data portability**: `/export` returns a ZIP containing one CSV per table (profile, consents, messages, rock-bottom scores). UTF-8 with BOM, opens directly in Excel / Google Sheets.
- **Art. 8 — minors**: the bot is for users 18+. The age confirmation is part of the consent prompt.

The `/reset` command clears only the conversation history and does not constitute full erasure under art. 17.

A **safety net for crisis signals** is also built in: a configurable list of Italian keywords (`crisis_keywords` in [persona.yaml](persona.yaml)) is matched against every user message. On match the bot sends a fixed message with emergency-line phone numbers (`crisis_response`) **before** invoking the LLM, so the user always sees actionable help even if the model errors out. False positives are accepted as a deliberate trade-off — under-triggering on real crisis signals would be the more dangerous failure mode.

A [DPIA (Data Protection Impact Assessment)](DPIA.md) template is included alongside the privacy policy and should be completed by anyone deploying the bot before going public.

Adapt the [persona.yaml](persona.yaml) text fields (`privacy`, `consent_request`, `consent_*`, `forget_*`, `crisis_response`, `crisis_keywords`) and the contents of [PRIVACY.md](PRIVACY.md) / [DPIA.md](DPIA.md) to fit your deployment, the data controller, and any additional regulations applicable in your jurisdiction.

---

## 🗂️ Data layout

```
met-me/
├── db/met-me.db           # SQLite (gitignored, path from DB_PATH)
├── logs/
│   ├── bot.log            # rotating app-wide log
│   └── {user_id}.log      # one file per user
└── rag/
    ├── docs/              # source documents (admin-managed)
    └── index/             # Chroma persistence + manifest.json (gitignored)
```

---

## 📦 Tech stack

- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) `21.6`
- [anthropic](https://github.com/anthropics/anthropic-sdk-python) ≥ 0.39
- [ollama](https://github.com/ollama/ollama-python) ≥ 0.3
- [llama-index](https://github.com/run-llama/llama_index) ≥ 0.14 + [chromadb](https://github.com/chroma-core/chroma) ≥ 1.5
- [ddgs](https://pypi.org/project/ddgs/) for web search
- SQLite (stdlib)

---

## 🎵 Liner notes

The project contains references to three artists whose music has told the story of mental health, identity, and vulnerability better than many textbooks.

- 💬 **Mac Miller** — the bot is called **Mac** and its `short_description` is *"Be you. You'll be fine."* A tribute to an artist who turned fragility and self-care into a language.

- 🎭 **Marracash** — the [persona.yaml](persona.yaml) file takes its name from the album *Persona* (2019). On that record every track is a body part or a fragment of identity. The same idea is borrowed here: `persona.yaml` is the set of "parts" that make up the bot.

- 👋 **Eminem** — the bot's welcome message, *"Hi, my name is Mac 👋"*, is a nod to the single *My Name Is*. The bot's profile picture also echoes the single's cover: a "HI! MY NAME IS" tag with "Mac" in place of "Slim Shady". Direct introduction, no frills. The name of the *rock-bottom tracker* is borrowed from the track *Rock Bottom* (from *The Slim Shady LP*, 1999), a raw portrait of the lowest point one tries to climb back from.

Thanks for the music. 🙏

---

## 📄 License

[MIT](LICENSE) © 2026 Matteo Pozzi
