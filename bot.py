"""Telegram bot entry point.

Connects a Telegram bot to an LLM (Claude or Ollama) and persists users,
messages, rock-bottom scores, GDPR consent records, admin-issued bans and the
active-model setting in SQLite. The active provider is chosen by admins via
/model. Persona texts (welcome, /help, /privacy summary, consent prompt,
crisis response, hotlines, rate-limit notice, system prompt, etc.) all live
in persona.yaml. Document RAG over rag/docs/ injects retrieved chunks into
the system prompt per turn. Optional Sentry error tracking is initialized at
startup when SENTRY_DSN is set, with per-request correlation IDs propagated
via ContextVar so events can be linked back to local logs without sending
user identifiers.
"""

from __future__ import annotations

import asyncio
import collections
import contextvars
import csv
import io
import logging
import logging.handlers
import sqlite3
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone

from telegram import (
    Bot,
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    User,
)
from telegram.constants import ChatAction
from telegram.error import RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from db import Database
from llm import LLMProvider, build_providers
from rock_bottom import RockBottomTracker
from rag import Rag

ACTIVE_MODEL_KEY = "active_model"

# Telegram hard limit on a single text message is 4096 characters. We leave a
# small margin so a trailing newline or entity expansion does not push us over.
TG_MAX_LEN = 4000


def split_for_telegram(text: str, limit: int = TG_MAX_LEN) -> list[str]:
    """Split a long reply into chunks that fit Telegram's per-message limit.

    Tries to cut on a paragraph boundary first, then on a line break, then on a
    sentence end, then on whitespace, falling back to a hard slice. Returns
    chunks in order; never returns an empty chunk.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        # Preference order for the split point, scanned from the end of the window.
        for sep in ("\n\n", "\n", ". ", " "):
            idx = window.rfind(sep)
            if idx > 0:
                cut = idx + len(sep)
                break
        else:
            cut = limit  # no good boundary found; hard cut
        chunk = remaining[:cut].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks

# ---- Logging setup ----------------------------------------------------------

LOG_DIR = config.ROOT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Per-user log line format: "YYYY-MM-DD HH:MM:SS | TYPE | ... | text".
# Fixed-width columns keep the file scannable by eye. Helper functions below
# format each event type with the right column layout.
USER_LOG_FMT = "%(asctime)s | %(message)s"
USER_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Column widths for the type tag and the model tag (kept short and constant).
_TYPE_W = 9
_MODEL_W = 7

_LOG_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

logging.basicConfig(level=logging.INFO, format=_LOG_FMT)
# Silence the very chatty httpx access logs from python-telegram-bot.
logging.getLogger("httpx").setLevel(logging.WARNING)

# App-wide rotating log file: 5MB per file, keep 5 backups (~25MB max on disk).
# Per-user loggers set propagate=False so they don't double-write here.
_app_file_handler = logging.handlers.RotatingFileHandler(
    LOG_DIR / "bot.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
_app_file_handler.setFormatter(logging.Formatter(_LOG_FMT))
logging.getLogger().addHandler(_app_file_handler)

log = logging.getLogger("bot")


_user_loggers: dict[int, logging.Logger] = {}


def _close_user_logger(user_id: int) -> None:
    """Detach and close any cached per-user logger handler.

    Called before rewriting or deleting ``logs/<user_id>.log`` so an open
    FileHandler does not race with the file operation. The logger is dropped
    from the cache; a subsequent log call recreates it lazily.
    """
    logger = _user_loggers.pop(user_id, None)
    if logger is None:
        return
    for h in list(logger.handlers):
        h.close()
        logger.removeHandler(h)


def user_logger(user_id: int) -> logging.Logger:
    """Return a logger that writes to logs/{user_id}.log (one file per user)."""
    if user_id in _user_loggers:
        return _user_loggers[user_id]
    logger = logging.getLogger(f"user.{user_id}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.FileHandler(LOG_DIR / f"{user_id}.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter(USER_LOG_FMT, datefmt=USER_LOG_DATEFMT))
    logger.addHandler(handler)
    _user_loggers[user_id] = logger
    return logger


def _username_tag(u: User) -> str:
    return f"@{u.username}" if u.username else "anon"


def _sanitize(text: str) -> str:
    # Collapse newlines so each event stays on one log line.
    return text.replace("\r", " ").replace("\n", " ")


def log_user(u: User, text: str) -> None:
    # User chat content stays out of the app-wide bot.log on purpose: only the
    # per-user file gets it. Same goes for log_assistant below.
    user_logger(u.id).info(
        "%s | %s | %s",
        "USER".ljust(_TYPE_W),
        _username_tag(u),
        _sanitize(text),
    )


def log_assistant(user_id: int, model: str, text: str) -> None:
    user_logger(user_id).info(
        "%s | %s | %s",
        "ASSISTANT".ljust(_TYPE_W),
        model.ljust(_MODEL_W),
        _sanitize(text),
    )


def log_cmd(u: User, command: str) -> None:
    user_logger(u.id).info(
        "%s | %s | %s",
        "CMD".ljust(_TYPE_W),
        _username_tag(u),
        command,
    )
    log.info("CMD user=%s (%s) %s", u.id, _username_tag(u), command)


def log_error(user_id: int, model: str, exc: BaseException) -> None:
    user_logger(user_id).error(
        "%s | %s | %s: %s",
        "ERROR".ljust(_TYPE_W),
        model.ljust(_MODEL_W),
        type(exc).__name__,
        _sanitize(str(exc)),
    )
    log.error(
        "user=%s model=%s %s: %s",
        user_id, model, type(exc).__name__, _sanitize(str(exc)),
    )


def log_event(user_id: int, message: str) -> None:
    """Generic single-column event (e.g. RESET, info notes)."""
    user_logger(user_id).info(
        "%s | %s",
        "EVENT".ljust(_TYPE_W),
        message,
    )
    log.info("EVENT user=%s %s", user_id, message)


def log_tool(user_id: int, tool: str, args: dict, result: str) -> None:
    # Truncate result preview to keep the log scannable; full result still
    # reaches the LLM and the user via the final assistant reply.
    preview = _sanitize(result)
    if len(preview) > 200:
        preview = preview[:200] + "..."
    user_logger(user_id).info(
        "%s | %s | args=%s | %s",
        "TOOL".ljust(_TYPE_W),
        tool.ljust(_MODEL_W),
        args,
        preview,
    )
    log.info("TOOL user=%s %s args=%s | %s", user_id, tool, args, preview)


# ---- App-wide singletons ----------------------------------------------------

CFG = config.load()
DB = Database(CFG.db_path)
PROVIDERS: dict[str, LLMProvider] = build_providers(CFG)
PERSONA = CFG.persona
SYSTEM_PROMPT = PERSONA.prompt
RAG = Rag(
    CFG.rag_path,
    CFG.ollama_host,
    CFG.embedding_model,
    CFG.rag_chunk_size,
    CFG.rag_chunk_overlap,
    CFG.rag_embed_num_ctx,
)
ROCK_BOTTOM = RockBottomTracker(CFG)

# Seed the active_model setting on first run, then trust the DB value afterwards.
if DB.get_setting_sync(ACTIVE_MODEL_KEY) is None:
    DB.set_setting_sync(ACTIVE_MODEL_KEY, CFG.default_model)


def is_admin(user_id: int) -> bool:
    return user_id in CFG.admin_ids


async def active_model() -> str:
    return await DB.get_setting(ACTIVE_MODEL_KEY, CFG.default_model)


# ---- Handlers ---------------------------------------------------------------

async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    log_cmd(u, "/start")
    if not await _ensure_not_banned(update):
        return
    await DB.upsert_user(u.id, u.username, u.first_name, u.last_name)
    if await DB.has_consent(u.id, CFG.consent_version):
        await update.message.reply_text(PERSONA.welcome)
        return
    # First-time user (or consent version bumped): show the explicit consent
    # prompt with inline accept/decline buttons. No further interaction is
    # accepted until they tap accept.
    log_event(u.id, f"CONSENT | requested (version={CFG.consent_version})")
    await update.message.reply_text(
        _consent_request_text(),
        reply_markup=_consent_keyboard(),
        disable_web_page_preview=True,
    )


async def on_consent_callback(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle taps on the consent inline keyboard.

    On accept: persist the consent under the current policy version, send the
    welcome + consent_accepted text, and strip the buttons from the original
    message so they cannot be re-tapped.
    On decline: edit the prompt to a "no data processed" notice and log the
    refusal. The user can re-enter the flow by sending /start again.
    """
    query = update.callback_query
    u = query.from_user
    await query.answer()
    data = query.data or ""

    if data == _CONSENT_CALLBACK_ACCEPT:
        await DB.upsert_user(u.id, u.username, u.first_name, u.last_name)
        await DB.record_consent(u.id, CFG.consent_version)
        log_event(u.id, f"CONSENT | granted (version={CFG.consent_version})")
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(PERSONA.welcome)
        return

    if data == _CONSENT_CALLBACK_DECLINE:
        log_event(u.id, f"CONSENT | declined (version={CFG.consent_version})")
        await query.edit_message_text(PERSONA.consent_declined)
        return

    log.warning("Unknown consent callback data: %r", data)


async def _ensure_not_banned(update: Update) -> bool:
    """Return True if the user is not currently banned. On False, reply with the
    banned notice and return so the caller can short-circuit. Bans persist
    across /forget on purpose (anti-abuse, legitimate interest art. 6(1)(f))."""
    u = update.effective_user
    if not await DB.is_banned(u.id):
        return True
    log_event(u.id, "BAN | blocked interaction")
    await update.effective_message.reply_text(PERSONA.banned_message)
    return False


async def _ensure_consent(update: Update) -> bool:
    """Return True if the calling user has granted consent under the current
    version. Otherwise reply with the prompt and return False so the caller
    can short-circuit the handler.
    """
    u = update.effective_user
    if await DB.has_consent(u.id, CFG.consent_version):
        return True
    log_event(u.id, "CONSENT | blocked interaction (no current-version consent)")
    await update.effective_message.reply_text(
        _consent_request_text(),
        reply_markup=_consent_keyboard(),
        disable_web_page_preview=True,
    )
    return False


async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    log_cmd(u, "/help")
    lines = list(PERSONA.help_user)
    if is_admin(u.id):
        lines += ["", "Admin:", *PERSONA.help_admin]
    await update.message.reply_text("\n".join(lines))


async def cmd_reset(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    log_cmd(u, "/reset")
    if not await _ensure_consent(update):
        return
    deleted = await DB.clear_history(u.id)
    log_event(u.id, f"RESET | {deleted} messages deleted")
    await update.message.reply_text("Cronologia cancellata.")


async def cmd_privacy(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    log_cmd(u, "/privacy")
    text = PERSONA.privacy.format(policy_url=CFG.privacy_policy_url)
    for chunk in split_for_telegram(text):
        await update.message.reply_text(chunk, disable_web_page_preview=True)


# Column order frozen here so the exported CSVs have a predictable header even
# if the DB schema gains additional columns in the future. The CSV writer is
# configured to silently ignore extra dict keys, so adding columns to the DB
# row dicts won't break the export.
_EXPORT_COLUMNS: dict[str, list[str]] = {
    "profile.csv": ["user_id", "username", "first_name", "last_name", "created_at", "updated_at"],
    "consents.csv": ["version", "granted_at"],
    "messages.csv": ["id", "role", "content", "model", "created_at"],
    "rock_bottom_scores.csv": ["id", "message_id", "score", "created_at"],
}


def _rows_to_csv_bytes(rows: list[dict], columns: list[str]) -> bytes:
    """Serialize ``rows`` as CSV with a UTF-8 BOM so Excel on Windows opens it
    with the right encoding without manual import steps."""
    sio = io.StringIO()
    writer = csv.DictWriter(sio, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return sio.getvalue().encode("utf-8-sig")


async def cmd_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Forward a user feedback message to every configured admin.

    Single-shot usage: ``/feedback <message>``. The original message text
    after the ``/feedback`` token is kept verbatim so spacing and casing
    survive intact.
    """
    u = update.effective_user
    raw = update.message.text or ""
    feedback_text = raw.partition(" ")[2].strip()
    log_cmd(u, "/feedback" + (" ..." if feedback_text else ""))

    if not await _ensure_not_banned(update):
        return
    if not await _ensure_consent(update):
        return

    if not feedback_text:
        await update.message.reply_text(PERSONA.feedback_usage)
        return
    if not CFG.admin_ids:
        await update.message.reply_text(PERSONA.feedback_no_admins)
        return

    handle = f"@{u.username}" if u.username else "anon"
    full_name = " ".join(p for p in (u.first_name, u.last_name) if p) or "-"
    header = f"📩 Feedback da {handle} (id={u.id}, name={full_name}):"
    body = f"{header}\n\n{feedback_text}"

    delivered = 0
    for admin_id in CFG.admin_ids:
        try:
            for chunk in split_for_telegram(body):
                await context.bot.send_message(chat_id=admin_id, text=chunk)
            delivered += 1
        except Exception:
            # An admin may have never started a chat with the bot or blocked it;
            # don't let one failure cancel delivery to the others.
            log.exception("Could not deliver feedback to admin %s", admin_id)

    log_event(
        u.id,
        f"FEEDBACK | bytes={len(feedback_text)} delivered={delivered}/{len(CFG.admin_ids)}",
    )
    if delivered > 0:
        await update.message.reply_text(PERSONA.feedback_thanks)
    else:
        await update.message.reply_text(PERSONA.feedback_no_admins)


async def cmd_export(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """GDPR art. 20 — right to data portability.

    Sends the user a ZIP containing four CSV files (profile, consents,
    messages, rock_bottom_scores). CSV is encoded as UTF-8 with BOM so it
    opens cleanly in Excel.
    """
    u = update.effective_user
    log_cmd(u, "/export")
    if not await _ensure_consent(update):
        return

    data = await DB.export_user(u.id)
    if data is None:
        await update.message.reply_text(PERSONA.export_empty)
        return

    log_event(u.id, f"REQ | id={_new_request_id()}")
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("profile.csv", _rows_to_csv_bytes(
            [data["profile"]], _EXPORT_COLUMNS["profile.csv"]
        ))
        z.writestr("consents.csv", _rows_to_csv_bytes(
            data["consents"], _EXPORT_COLUMNS["consents.csv"]
        ))
        z.writestr("messages.csv", _rows_to_csv_bytes(
            data["messages"], _EXPORT_COLUMNS["messages.csv"]
        ))
        z.writestr("rock_bottom_scores.csv", _rows_to_csv_bytes(
            data["rock_bottom_scores"], _EXPORT_COLUMNS["rock_bottom_scores.csv"]
        ))
    size = zip_buf.tell()
    zip_buf.seek(0)

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    filename = f"met-me-export-{u.id}-{today}.zip"
    await update.message.reply_document(
        document=zip_buf,
        filename=filename,
        caption=PERSONA.export_done,
    )
    log_event(
        u.id,
        f"EXPORT | bytes={size} messages={len(data['messages'])} "
        f"scores={len(data['rock_bottom_scores'])} consents={len(data['consents'])}",
    )


_FORGET_PENDING_KEY = "forget_pending_at"

# Random short ID minted per request and propagated through:
#   1. the ContextVar (visible to any coroutine in the same asyncio task tree)
#   2. local log files (via log_event REQ entry)
#   3. Sentry events (via the before_send hook in _init_sentry)
# The ID reveals nothing about the user; correlation back to a user_id is done
# by searching local logs for the same ID. This keeps Sentry free of PII while
# still allowing incident investigation.
_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=""
)


def _new_request_id() -> str:
    """Mint a short random request ID, store it in the ContextVar for the
    current asyncio task (and any subtask), return it for explicit logging."""
    rid = uuid.uuid4().hex[:12]
    _request_id_var.set(rid)
    return rid


def _sentry_before_send(event, hint):  # type: ignore[no-untyped-def]
    """Attach the current request ID to every Sentry event as a tag."""
    rid = _request_id_var.get()
    if rid:
        event.setdefault("tags", {})["request_id"] = rid
    return event

# Callback-data prefix used by the consent inline keyboard. Keep the prefix
# narrow and explicit so we never confuse it with other future callbacks.
_CONSENT_CALLBACK_ACCEPT = "consent:accept"
_CONSENT_CALLBACK_DECLINE = "consent:decline"

# Per-user sliding window of recent text-message timestamps, used by the
# burst rate limiter. In-memory only: a bot restart wipes counters, which is
# fine because the window is short (seconds) and entries would expire anyway.
_rate_limit_buckets: dict[int, collections.deque[float]] = {}


def _consent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(
                PERSONA.consent_button_accept,
                callback_data=_CONSENT_CALLBACK_ACCEPT,
            )],
            [InlineKeyboardButton(
                PERSONA.consent_button_decline,
                callback_data=_CONSENT_CALLBACK_DECLINE,
            )],
        ]
    )


def _format_hotlines() -> str:
    """Render hotlines as a bullet list. Single source of truth for the
    numbers shown in the consent prompt, the LLM system prompt on crisis, and
    the fallback crisis message."""
    return "\n".join(f"• {h}" for h in PERSONA.hotlines)


def _consent_request_text() -> str:
    return PERSONA.consent_request.format(
        policy_url=CFG.privacy_policy_url,
        hotlines=_format_hotlines(),
    )


def _crisis_response_text() -> str:
    return PERSONA.crisis_response.format(hotlines=_format_hotlines())


def _detect_crisis(text: str) -> str | None:
    """Return the first crisis keyword matched in ``text`` (or None).

    Lowercase substring match. Conservative on purpose: false positives mean
    the user gets emergency numbers shown unnecessarily, which is acceptable;
    false negatives in this context are not.
    """
    low = text.lower()
    for kw in PERSONA.crisis_keywords:
        if kw in low:
            return kw
    return None


def _within_rate_limit(user_id: int) -> bool:
    """Return True if the user is allowed to send another text message now.

    The rejected timestamp is NOT appended to the bucket, so a flooder cannot
    keep extending their own cooldown by retrying — only successful messages
    consume the budget.
    """
    now = time.time()
    window = CFG.rate_limit_burst_window_seconds
    bucket = _rate_limit_buckets.setdefault(user_id, collections.deque())
    while bucket and now - bucket[0] > window:
        bucket.popleft()
    if len(bucket) >= CFG.rate_limit_burst_count:
        return False
    bucket.append(now)
    return True


def _normalize_phrase(text: str) -> str:
    """Forgiving comparison for the /forget confirmation phrase.

    Strips surrounding whitespace, collapses internal whitespace runs to a single
    space, drops trailing punctuation, and lowercases — so trailing dots, double
    spaces, or different casing do not block a clearly-intended confirmation.
    """
    collapsed = " ".join(text.split())
    return collapsed.rstrip(".!?…").lower()


async def cmd_forget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """GDPR "right to erasure" — step 1: mark the user as awaiting confirmation.

    The actual deletion happens in ``on_text`` when the next message matches the
    confirmation phrase. Pending state lives in PTB's per-user ``user_data`` and
    expires after ``FORGET_CONFIRM_TIMEOUT_SECONDS``.
    """
    u = update.effective_user
    log_cmd(u, "/forget")
    context.user_data[_FORGET_PENDING_KEY] = time.time()
    await update.message.reply_text(PERSONA.forget_warning)


async def _perform_forget(update: Update) -> None:
    """Delete the user's DB rows and per-user log file, then confirm."""
    u = update.effective_user
    counts = await DB.delete_user(u.id)
    log_event(
        u.id,
        f"FORGET | users={counts['users']} messages={counts['messages']} "
        f"scores={counts['rock_bottom_scores']}",
    )
    # Drop the per-user log file and detach handlers so a future /start from
    # the same user_id starts a fresh log instead of appending to the old one.
    _close_user_logger(u.id)
    log_path = LOG_DIR / f"{u.id}.log"
    try:
        log_path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Could not delete log file %s: %s", log_path, exc)

    await update.message.reply_text(PERSONA.forget_done)


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    args = context.args or []
    log_cmd(u, "/model" + (" " + " ".join(args) if args else ""))

    if not is_admin(u.id):
        await update.message.reply_text("Admin-only command.")
        return

    if not args:
        current = await active_model()
        await update.message.reply_text(f"Active model: {current}")
        return

    choice = args[0].lower().strip()
    if choice not in CFG.valid_models:
        await update.message.reply_text(
            f"Invalid model. Choose one of: {', '.join(CFG.valid_models)}."
        )
        return

    await DB.set_setting(ACTIVE_MODEL_KEY, choice)
    log.info("admin %s switched model to %s", u.id, choice)
    await update.message.reply_text(f"Model set to: {choice}")


def _format_user_row(row) -> str:
    handle = f"@{row['username']}" if row["username"] else "anon"
    name = " ".join(p for p in (row["first_name"], row["last_name"]) if p) or "-"
    last = row["last_msg_at"] or "-"
    return (
        f"{row['user_id']} | {handle} | {name} | "
        f"msgs={row['msg_count']} | joined={row['created_at']} | last={last}"
    )


async def cmd_users(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    log_cmd(u, "/users")
    if not is_admin(u.id):
        await update.message.reply_text("Admin-only command.")
        return

    rows = await DB.list_users()
    if not rows:
        await update.message.reply_text("No users registered yet.")
        return

    body = "\n".join(_format_user_row(r) for r in rows)
    text = f"Users ({len(rows)}):\n{body}"
    for chunk in split_for_telegram(text):
        await update.message.reply_text(chunk)


async def cmd_reindex(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    log_cmd(u, "/reindex")
    if not is_admin(u.id):
        await update.message.reply_text("Admin-only command.")
        return
    log_event(u.id, f"REQ | id={_new_request_id()}")
    await update.message.reply_text("Reindexing rag/docs/...")
    try:
        files, nodes = await RAG.ingest_folder()
    except Exception as exc:
        log.exception("Reindex failed")
        await update.message.reply_text(f"Reindex failed: {exc}")
        return
    log_event(u.id, f"REINDEX | files={files} nodes={nodes}")
    await update.message.reply_text(f"Reindexed {files} files, {nodes} chunks.")


async def on_document(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: any document sent by an admin is saved to rag/docs/ and
    indexed. Non-admins are silently ignored."""
    msg = update.message
    u = update.effective_user
    if not is_admin(u.id):
        return
    doc = msg.document
    if doc is None or not doc.file_name:
        await msg.reply_text("Missing filename on document.")
        return

    target = RAG.docs_path / doc.file_name
    log_cmd(u, f"DOC {doc.file_name}")
    log_event(u.id, f"REQ | id={_new_request_id()}")
    file = await doc.get_file()
    await file.download_to_drive(custom_path=str(target))
    await msg.reply_text(f"Downloaded {doc.file_name}. Indexing...")
    try:
        added = await RAG.ingest_file(target)
    except Exception as exc:
        log.exception("Ingest failed for %s", target)
        await msg.reply_text(f"Indexing failed: {exc}")
        return
    log_event(u.id, f"DOC | uploaded={doc.file_name} | nodes={added}")
    await msg.reply_text(f"Indexed {added} chunks from {doc.file_name}.")


async def cmd_stats(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    log_cmd(u, "/stats")
    if not is_admin(u.id):
        await update.message.reply_text("Admin-only command.")
        return

    s = await DB.get_stats()
    lines = [
        "Bot statistics:",
        f"- total users: {s['total_users']}",
        f"- total messages: {s['total_messages']}",
        f"  - user: {s['per_role'].get('user', 0)}",
        f"  - assistant: {s['per_role'].get('assistant', 0)}",
    ]
    if s["per_model"]:
        lines.append("- per model:")
        for model, n in s["per_model"].items():
            lines.append(f"  - {model}: {n}")
    if s["top_users"]:
        lines.append("- top users (by messages):")
        for r in s["top_users"]:
            handle = f"@{r['username']}" if r["username"] else (r["first_name"] or str(r["user_id"]))
            lines.append(f"  - {handle}: {r['msg_count']}")

    text = "\n".join(lines)
    for chunk in split_for_telegram(text):
        await update.message.reply_text(chunk)


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: block a user from interacting with the bot.

    Usage: ``/ban <user_id> [reason...]``. The ban is keyed by Telegram user_id
    and persists across /forget (otherwise a banned user could clear their own
    ban by self-deleting).
    """
    u = update.effective_user
    args = context.args or []
    log_cmd(u, "/ban" + (" " + " ".join(args) if args else ""))
    if not is_admin(u.id):
        await update.message.reply_text("Admin-only command.")
        return
    if not args:
        await update.message.reply_text("Usage: /ban <user_id> [reason...]")
        return
    try:
        target = int(args[0])
    except ValueError:
        await update.message.reply_text(f"Invalid user_id: {args[0]!r}")
        return
    reason = " ".join(args[1:]).strip() or None
    await DB.ban_user(target, u.id, reason)
    log_event(u.id, f"BAN | target={target} reason={reason!r}")
    await update.message.reply_text(
        f"User {target} banned." + (f" Reason: {reason}" if reason else "")
    )


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: lift a ban. Usage: ``/unban <user_id>``."""
    u = update.effective_user
    args = context.args or []
    log_cmd(u, "/unban" + (" " + " ".join(args) if args else ""))
    if not is_admin(u.id):
        await update.message.reply_text("Admin-only command.")
        return
    if not args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    try:
        target = int(args[0])
    except ValueError:
        await update.message.reply_text(f"Invalid user_id: {args[0]!r}")
        return
    existed = await DB.unban_user(target)
    if existed:
        log_event(u.id, f"UNBAN | target={target}")
        await update.message.reply_text(f"User {target} unbanned.")
    else:
        await update.message.reply_text(f"User {target} was not banned.")


async def cmd_bans(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: list currently banned users."""
    u = update.effective_user
    log_cmd(u, "/bans")
    if not is_admin(u.id):
        await update.message.reply_text("Admin-only command.")
        return
    rows = await DB.list_bans()
    if not rows:
        await update.message.reply_text("No bans.")
        return
    lines = [f"Banned users ({len(rows)}):"]
    for r in rows:
        reason = r["reason"] or "-"
        lines.append(
            f"- {r['user_id']} | by={r['banned_by']} | at={r['banned_at']} | {reason}"
        )
    text = "\n".join(lines)
    for chunk in split_for_telegram(text):
        await update.message.reply_text(chunk)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    u = update.effective_user
    text = msg.text or ""

    # Intercept the /forget confirmation flow BEFORE we touch the DB or log the
    # message: a user mid-deletion shouldn't have their confirmation phrase
    # persisted as a normal chat turn.
    pending_at = context.user_data.get(_FORGET_PENDING_KEY)
    if pending_at is not None:
        expired = (time.time() - pending_at) > CFG.forget_confirm_timeout_seconds
        context.user_data.pop(_FORGET_PENDING_KEY, None)
        if expired:
            log_event(u.id, "FORGET | confirmation expired, treating as normal message")
        elif _normalize_phrase(text) == _normalize_phrase(PERSONA.forget_confirm_phrase):
            log_user(u, text)
            await _perform_forget(update)
            return
        else:
            log_event(u.id, "FORGET | confirmation cancelled by mismatched reply")
            await msg.reply_text(PERSONA.forget_cancelled)
            return

    # Ban gate: runs AFTER the /forget pending check above (so a banned user
    # mid-deletion can still complete the right-to-erasure flow) but BEFORE
    # consent / rate limit / LLM so no other work happens on a banned message.
    if not await _ensure_not_banned(update):
        return

    # GDPR art. 9: no message is processed, stored, or sent to the LLM until
    # the user has granted explicit consent under the current policy version.
    if not await _ensure_consent(update):
        return

    # Anti-flood burst limiter. Runs after the /forget flow so a user who is
    # mid-deletion can always complete it, and before any DB write so dropped
    # messages leave no trace beyond the rate-limit log event.
    if not _within_rate_limit(u.id):
        log_event(
            u.id,
            f"RATE_LIMIT | dropped ({CFG.rate_limit_burst_count} msgs in "
            f"{CFG.rate_limit_burst_window_seconds}s)",
        )
        await msg.reply_text(PERSONA.rate_limit_message)
        return

    # Refresh user profile on every message: names/usernames can change over time.
    await DB.upsert_user(u.id, u.username, u.first_name, u.last_name)

    log_user(u, text)
    log_event(u.id, f"REQ | id={_new_request_id()}")

    # Crisis-keyword detection. We don't send a separate message here: the
    # hotline numbers and instructions are instead injected into the LLM
    # system prompt below so the model produces ONE coherent empathetic reply
    # that already contains the numbers. The fixed `crisis_response` text is
    # only used as a fallback when the LLM call fails. Detection is a coarse
    # substring match — we accept false positives because under-triggering on
    # real crisis signals would be the more dangerous failure mode.
    crisis_kw = _detect_crisis(text)
    if crisis_kw is not None:
        log_event(u.id, f"CRISIS | keyword={crisis_kw!r}")

    model_name = await active_model()
    provider = PROVIDERS[model_name]
    history = await DB.get_history(u.id, CFG.history_max_turns)

    # Retrieve relevant document chunks from rag/docs/ and inject them into the
    # system prompt as background knowledge.
    retrieved = await RAG.query(text, CFG.rag_top_k)
    system = _build_system_prompt(retrieved, crisis=crisis_kw is not None)

    await msg.chat.send_action(ChatAction.TYPING)

    def _on_tool(name: str, args: dict, result: str) -> None:
        log_tool(u.id, name, args, result)

    try:
        reply = await provider.complete(system, history, text, on_tool=_on_tool)
    except Exception as exc:
        log.exception("LLM error for user %s with provider %s", u.id, provider.name)
        log_error(u.id, provider.name, exc)
        if crisis_kw is not None:
            # Safety net: even if the LLM is unreachable, a crisis-flagged
            # turn must reach the user with hotline numbers.
            for chunk in split_for_telegram(_crisis_response_text()):
                await msg.reply_text(chunk)
        else:
            await msg.reply_text(
                "Mi dispiace, qualcosa è andato storto. Riprova tra un momento."
            )
        return

    # Only persist after a successful response, to keep the conversation log consistent.
    user_msg_id = await DB.add_message(u.id, "user", text, model_name)
    await DB.add_message(u.id, "assistant", reply, model_name)

    log_assistant(u.id, provider.name, reply)
    for chunk in split_for_telegram(reply):
        await msg.reply_text(chunk)

    # Score rock-bottom out of band so the reply is not delayed by a second LLM call.
    asyncio.create_task(
        _track_rock_bottom(context.bot, u.id, text, user_msg_id, model_name)
    )


def _build_system_prompt(retrieved: list[str], crisis: bool = False) -> str:
    parts = [SYSTEM_PROMPT]
    if retrieved:
        bullets = "\n\n".join(f"- {m}" for m in retrieved)
        parts.append(
            "Knowledge base (estratti rilevanti da documenti caricati; "
            "usa solo se pertinenti, cita la fonte se opportuno):\n"
            f"{bullets}"
        )
    if crisis:
        # When the user's message tripped the crisis keyword filter we add the
        # emergency numbers and an explicit instruction to the LLM. The model
        # is responsible for weaving the numbers into a single empathetic reply
        # so the user gets one coherent message — not two consecutive sends.
        parts.append(
            "SICUREZZA — possibile crisi: nel messaggio dell'utente sono comparsi segnali "
            "di potenziale autolesionismo, ideazione suicidaria o disperazione acuta. "
            "Nella tua risposta DEVI:\n"
            "1) Validare con calore e brevità (frasi corte, presenza, niente clinico).\n"
            "2) Invitare gentilmente a non restare solo/a con questi pensieri ora.\n"
            "3) Includere ESATTAMENTE questi numeri di emergenza, integrandoli nel testo in modo "
            "naturale (puoi usarli come elenco a fine messaggio se aiuta la leggibilità):\n"
            f"{_format_hotlines()}\n"
            "Non inventare altri numeri, non parafrasarli, non ometterne."
        )
    return "\n\n".join(parts)


# ---- Rock-bottom tracking ---------------------------------------------------

async def _track_rock_bottom(
    bot: Bot,
    user_id: int,
    user_msg: str,
    user_msg_id: int,
    model_name: str,
) -> None:
    """Score the just-posted user message on the rock-bottom scale, persist it,
    and alert admins when the rolling average crosses the configured threshold.

    Runs as a background task: any failure is logged but never surfaced to the
    user or the chat handler.
    """
    score = await ROCK_BOTTOM.score(model_name, user_msg)
    if score is None:
        log_event(user_id, "ROCK_BOTTOM | scoring failed")
        return

    await DB.add_rock_bottom_score(user_id, score, user_msg_id)
    avg, count = await DB.recent_rock_bottom_avg(user_id, CFG.rock_bottom_window)
    log_event(
        user_id,
        f"ROCK_BOTTOM | score={score:.2f} avg={avg:.2f} "
        f"window={count}/{CFG.rock_bottom_window}",
    )

    # Require a full window of scores before alerting: with fewer samples a single
    # bad message could trip the threshold and produce noisy false positives.
    if (
        avg is None
        or count < CFG.rock_bottom_window
        or avg >= CFG.rock_bottom_threshold
    ):
        return

    user_row = await DB.get_user(user_id)
    low_msgs = await DB.lowest_recent_user_msgs(
        user_id, CFG.rock_bottom_window, CFG.rock_bottom_alert_low_msgs
    )
    alert_text = _format_alert(user_row, avg, count, low_msgs)
    await _alert_admins(bot, alert_text)
    log_event(
        user_id,
        f"ROCK_BOTTOM | ALERT sent (avg={avg:.2f} < {CFG.rock_bottom_threshold:.2f})",
    )


def _format_alert(
    user_row: sqlite3.Row | None,
    avg: float,
    count: int,
    low_msgs: list[sqlite3.Row],
) -> str:
    if user_row is None:
        header = "Unknown user"
    else:
        handle = f"@{user_row['username']}" if user_row["username"] else "anon"
        full_name = " ".join(
            p for p in (user_row["first_name"], user_row["last_name"]) if p
        ) or "-"
        header = (
            f"id={user_row['user_id']} | {handle} | {full_name} | "
            f"joined={user_row['created_at']}"
        )

    lines = [
        "Rock-bottom alert",
        header,
        f"avg={avg:.2f} (threshold={CFG.rock_bottom_threshold:.2f}, "
        f"window={count}/{CFG.rock_bottom_window})",
    ]
    if low_msgs:
        lines.append("")
        lines.append("Lowest-scoring recent messages:")
        for r in low_msgs:
            snippet = (r["content"] or "").replace("\n", " ")
            if len(snippet) > 300:
                snippet = snippet[:300] + "..."
            lines.append(
                f"- [{r['created_at']}] score={r['score']:.2f}: {snippet}"
            )
    return "\n".join(lines)


async def _alert_admins(bot: Bot, text: str) -> None:
    for admin_id in CFG.admin_ids:
        try:
            for chunk in split_for_telegram(text):
                await bot.send_message(chat_id=admin_id, text=chunk)
        except Exception:
            # An admin may have blocked the bot or never started a chat with it;
            # never let one bad recipient stop the others from being notified.
            log.exception("Failed to send rock-bottom alert to admin %s", admin_id)


# ---- Bootstrap --------------------------------------------------------------

def _parse_commands(lines: tuple[str, ...]) -> list[BotCommand]:
    """Parse "/cmd - description" entries from persona help lists into BotCommand objects.

    Telegram's command menu only accepts a single token per command (no args), so
    we strip leading "/" and split off any "claude|ollama" style argument hints
    after the first whitespace in the command part.
    """
    commands: list[BotCommand] = []
    seen: set[str] = set()
    for raw in lines:
        if " - " not in raw:
            continue
        cmd_part, desc = raw.split(" - ", 1)
        cmd = cmd_part.strip().lstrip("/").split()[0]
        if cmd in seen:
            continue
        seen.add(cmd)
        commands.append(BotCommand(cmd, desc.strip()))
    return commands


async def _sync_bot_profile(bot: Bot) -> None:
    """Update bot name/descriptions only when changed. Telegram throttles
    repeated writes aggressively (RetryAfter ~24h), so we read current values
    and only PATCH on diff. RetryAfter is swallowed so startup never blocks."""
    try:
        if (await bot.get_my_name()).name != PERSONA.name:
            await bot.set_my_name(name=PERSONA.name)
        if (await bot.get_my_short_description()).short_description != PERSONA.short_description:
            await bot.set_my_short_description(short_description=PERSONA.short_description)
        if (await bot.get_my_description()).description != PERSONA.welcome:
            await bot.set_my_description(description=PERSONA.welcome)
    except RetryAfter as e:
        log.warning("Telegram flood control on profile update: %s", e)


def _purge_user_log_files_sync(retention_days: int) -> dict[str, int]:
    """Drop log lines older than ``retention_days`` from every per-user log file.

    Operates only on files matching ``logs/<digits>.log`` so the app-wide
    ``bot.log`` (rotated by its handler) is left alone. Each line in a per-user
    log starts with ``YYYY-MM-DD HH:MM:SS``; lines whose timestamp is older
    than the cutoff are removed. If a file ends up empty it is unlinked, and
    any cached logger for that user is closed so a future event will recreate
    the file fresh.
    """
    cutoff = datetime.now() - timedelta(days=retention_days)
    files_touched = 0
    files_removed = 0
    lines_kept_total = 0
    lines_removed_total = 0

    for log_path in LOG_DIR.glob("*.log"):
        # The app-wide rotating log doesn't belong to a user.
        if log_path.name == "bot.log":
            continue
        try:
            user_id = int(log_path.stem)
        except ValueError:
            continue

        # Detach any open FileHandler before we rewrite the file: keeping it
        # open across the rewrite would corrupt buffering on Windows and could
        # race with a concurrent log_user/log_assistant call.
        _close_user_logger(user_id)

        try:
            raw_lines = log_path.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError as exc:
            log.warning("Purge: could not read %s: %s", log_path, exc)
            continue

        kept: list[str] = []
        for line in raw_lines:
            # USER_LOG_FMT puts a "YYYY-MM-DD HH:MM:SS" prefix on every entry,
            # and _sanitize() collapses newlines so each event is one line.
            try:
                ts = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                # Unparseable line (corrupt? old format?) — keep to be safe.
                kept.append(line)
                continue
            if ts >= cutoff:
                kept.append(line)
                lines_kept_total += 1
            else:
                lines_removed_total += 1

        files_touched += 1
        if kept:
            tmp = log_path.with_suffix(".log.tmp")
            tmp.write_text("".join(kept), encoding="utf-8")
            tmp.replace(log_path)
        else:
            try:
                log_path.unlink()
                files_removed += 1
            except OSError as exc:
                log.warning("Purge: could not unlink empty %s: %s", log_path, exc)

    return {
        "files_touched": files_touched,
        "files_removed": files_removed,
        "lines_kept": lines_kept_total,
        "lines_removed": lines_removed_total,
    }


def _next_purge_time(day: int, hour: int) -> datetime:
    """Return the next ``datetime`` matching (day, hour:00) from now.

    ``day`` is expected to be in 1-28 (validated at config load) so we never
    have to handle short-month edge cases like Feb 30/31.
    """
    now = datetime.now()
    candidate = now.replace(day=day, hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= now:
        if candidate.month == 12:
            candidate = candidate.replace(year=candidate.year + 1, month=1)
        else:
            candidate = candidate.replace(month=candidate.month + 1)
    return candidate


async def _run_retention_purge() -> None:
    """One-shot retention purge over DB rows and per-user log files."""
    rid = _new_request_id()
    days = CFG.message_retention_days
    db_counts = await DB.purge_old_data(days)
    log_counts = await asyncio.to_thread(_purge_user_log_files_sync, days)
    log.info(
        "PURGE req=%s | retention_days=%d db_messages=%d db_scores=%d "
        "log_files_touched=%d log_files_removed=%d log_lines_kept=%d log_lines_removed=%d",
        rid, days,
        db_counts["messages"], db_counts["rock_bottom_scores"],
        log_counts["files_touched"], log_counts["files_removed"],
        log_counts["lines_kept"], log_counts["lines_removed"],
    )


async def _purge_loop() -> None:
    """Background coroutine that runs the monthly retention purge.

    Uses a plain ``asyncio.sleep`` between runs instead of an external job
    scheduler so the bot has no extra runtime dependency (PTB's ``JobQueue``
    would require the optional ``[job-queue]`` extra and APScheduler).

    The next fire time is recomputed every iteration, so DST or clock changes
    are handled lazily — at worst one run is offset by a small drift.
    """
    while True:
        next_run = _next_purge_time(CFG.message_purge_day, CFG.message_purge_hour)
        delay = (next_run - datetime.now()).total_seconds()
        log.info(
            "Next retention purge: %s (in %.1f hours)",
            next_run.isoformat(timespec="seconds"),
            delay / 3600,
        )
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            log.info("Retention purge loop cancelled.")
            return
        try:
            await _run_retention_purge()
        except Exception:
            # Never let a purge error kill the loop — log and wait for next month.
            log.exception("Retention purge raised; will retry on next scheduled run.")


async def post_init(app: Application) -> None:
    await _sync_bot_profile(app.bot)

    user_commands = _parse_commands(PERSONA.help_user)
    await app.bot.set_my_commands(user_commands)

    admin_commands = user_commands + _parse_commands(PERSONA.help_admin)
    for admin_id in CFG.admin_ids:
        await app.bot.set_my_commands(
            admin_commands,
            scope=BotCommandScopeChat(chat_id=admin_id),
        )

    files, nodes = await RAG.ingest_folder()
    log.info("RAG startup ingest: files=%d nodes=%d", files, nodes)

    # Monthly retention purge of conversation data + per-user log files.
    # Setting MESSAGE_RETENTION_DAYS=0 disables the job (useful for dev).
    if CFG.message_retention_days > 0:
        asyncio.create_task(_purge_loop(), name="retention_purge")
        log.info(
            "Started monthly purge loop on day %d at %02d:00 (retention=%d days)",
            CFG.message_purge_day, CFG.message_purge_hour, CFG.message_retention_days,
        )
    else:
        log.info("Retention purge disabled (MESSAGE_RETENTION_DAYS=0)")


def _init_sentry() -> None:
    """Initialize Sentry error tracking if a DSN is configured.

    Conservative defaults: PII off, no performance tracing, only error events
    are sent. Stack traces and exception types reach Sentry; user message
    content and Telegram identifiers do not (no ``set_user()`` call anywhere).
    """
    if not CFG.sentry_dsn:
        log.info("Sentry disabled (empty SENTRY_DSN).")
        return
    import sentry_sdk
    sentry_sdk.init(
        dsn=CFG.sentry_dsn,
        environment=CFG.sentry_environment,
        send_default_pii=False,
        traces_sample_rate=0.0,
        sample_rate=1.0,
        before_send=_sentry_before_send,
    )
    log.info("Sentry initialized (environment=%s).", CFG.sentry_environment)


def main() -> None:
    _init_sentry()
    app = (
        Application.builder()
        .token(CFG.telegram_token)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("feedback", cmd_feedback))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("reindex", cmd_reindex))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("bans", cmd_bans))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_consent_callback, pattern=r"^consent:"))

    log.info(
        "Bot starting. default_model=%s active_model=%s admins=%s db=%s",
        CFG.default_model,
        DB.get_setting_sync(ACTIVE_MODEL_KEY, CFG.default_model),
        sorted(CFG.admin_ids),
        CFG.db_path,
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
