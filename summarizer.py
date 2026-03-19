import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import config
import storage
from llm import get_provider

logger = logging.getLogger(__name__)
provider = get_provider()

SUMMARIZER_INSTRUCTIONS = (
    "You are a concise news summarizer. "
    "Summarize the key points clearly and briefly. "
    "Group by topic if relevant. Skip filler content."
)

# Default maximum characters per chunk. You can override with environment variable
# SUMMARIZER_MAX_CHARS (useful in .env).
_default_max_chars = max(2000, config.MAX_TOKENS * 4)
MAX_CHARS = int(os.getenv("SUMMARIZER_MAX_CHARS", str(_default_max_chars)))


def _system_prompt() -> str:
    # Append summarizer instructions to the user's SYSTEM_PROMPT so language
    # preferences (e.g. "Always respond in Ukrainian") are respected.
    return f"{config.SYSTEM_PROMPT}\n\n{SUMMARIZER_INSTRUCTIONS}"


def _format(channel: str, messages: List[Dict]) -> str:
    lines = [f"=== {channel} ==="]
    for m in messages:
        # keep concise date format
        date = datetime.fromisoformat(m["date"]).strftime("%b %d %H:%M")
        # trim whitespace
        text = (m.get("text") or "").strip()
        lines.append(f"[{date}] {text}")
    return "\n".join(lines)


def _split_text_into_chunks(text: str, max_chars: int) -> List[str]:
    """
    Split a long text into chunks of at most max_chars, trying to split on line breaks or spaces.
    """
    if len(text) <= max_chars:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        # try to backtrack to a newline or space for nicer splits
        if end < len(text):
            sep_pos = text.rfind("\n", start, end)
            if sep_pos == -1:
                sep_pos = text.rfind(" ", start, end)
            if sep_pos != -1 and sep_pos > start:
                end = sep_pos
        chunk = text[start:end].strip()
        if not chunk:
            # fallback: force split
            end = min(start + max_chars, len(text))
            chunk = text[start:end].strip()
        chunks.append(chunk)
        start = end
    return chunks


def _chunk_messages(messages: List[Dict], max_chars: int) -> List[List[Dict]]:
    """
    Group messages into chunks so the formatted chunk text length does not exceed max_chars.
    Each chunk is a list of message dicts.
    """
    chunks: List[List[Dict]] = []
    current: List[Dict] = []
    current_len = 0

    for m in messages:
        line = f"[{datetime.fromisoformat(m['date']).strftime('%b %d %H:%M')}] {m.get('text', '').strip()}"
        line_len = len(line) + 1  # +1 for newline
        # If single message itself exceeds max_chars, split the message text
        if line_len > max_chars:
            # finish current chunk if non-empty
            if current:
                chunks.append(current)
                current = []
                current_len = 0
            # split the message text into sub-texts and make each a separate pseudo-message
            text = m.get("text", "") or ""
            sub_texts = _split_text_into_chunks(
                text, max_chars - 50
            )  # leave room for header
            for sub in sub_texts:
                chunks.append(
                    [{"text": sub, "date": m["date"], "sender": m.get("sender", "")}]
                )
            continue

        if current_len + line_len > max_chars and current:
            chunks.append(current)
            current = [m]
            current_len = line_len
        else:
            current.append(m)
            current_len += line_len

    if current:
        chunks.append(current)
    return chunks


def _call_provider_summarize(prompt_text: str) -> str:
    """
    Call the provider to summarize the given prompt_text. Wraps messages in the expected format.
    """
    try:
        messages = [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": prompt_text},
        ]
        return provider.chat(messages)
    except Exception as e:
        logger.exception("LLM provider failed during summarization")
        raise


def _summarize_chunks_formatted(channel: str, chunks: List[List[Dict]]) -> str:
    """
    Summarize one or multiple chunks (which are lists of messages already grouped).
    Returns the final summary text.
    """
    chunk_summaries = []
    for idx, chunk_messages in enumerate(chunks):
        content = _format(channel, chunk_messages)
        user_prompt = f"Summarize:\n\n{content}"
        logger.debug(
            "Summarizing chunk %d/%d for %s (approx %d chars)",
            idx + 1,
            len(chunks),
            channel,
            len(content),
        )
        summary = _call_provider_summarize(user_prompt)
        chunk_summaries.append(summary.strip())

    if not chunk_summaries:
        return ""

    if len(chunk_summaries) == 1:
        return chunk_summaries[0]

    # Combine intermediate summaries and create a final concise summary
    combined = "\n\n".join(
        f"Chunk {i + 1} summary:\n{cs}" for i, cs in enumerate(chunk_summaries)
    )
    final_prompt = (
        "Combine and condense the following chunk summaries into a single concise summary, "
        "remove duplicates, and keep only key points:\n\n" + combined
    )
    logger.debug(
        "Combining %d chunk summaries for final pass for %s",
        len(chunk_summaries),
        channel,
    )
    final_summary = _call_provider_summarize(final_prompt)
    return final_summary.strip()


def summarize_channel(channel: str, hours: int = 24) -> str:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    messages = storage.get_messages(channel, since=since)

    if not messages:
        return f"No messages from {channel} in the last {hours}h."

    # Chunk messages by size to avoid overly long prompts
    chunks = _chunk_messages(messages, MAX_CHARS)
    logger.info(
        "Channel %s: %d messages -> %d chunks (max_chars=%d)",
        channel,
        len(messages),
        len(chunks),
        MAX_CHARS,
    )

    try:
        summary = _summarize_chunks_formatted(channel, chunks)
        return f"Summary of {channel} (last {hours}h):\n\n{summary}"
    except Exception as e:
        logger.exception("Failed to summarize channel %s", channel)
        return f"Error generating summary: {e}"


def summarize_all(hours: int = 24) -> str:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    all_messages = storage.get_all_messages(since=since)

    if not all_messages:
        return f"No messages from any monitored channel in the last {hours}h."

    # Build formatted sections per channel and chunk across channels to respect MAX_CHARS
    sections = []
    for channel, msgs in all_messages.items():
        sections.append((channel, _format(channel, msgs)))

    # Now build combined chunks (list of tuples) where each chunk content size <= MAX_CHARS
    combined_chunks: List[List[tuple]] = []
    current: List[tuple] = []
    current_len = 0
    for channel, section_text in sections:
        sec_len = len(section_text) + 2
        if sec_len > MAX_CHARS:
            # Very large single channel section: split it by messages
            # retrieve original messages and chunk them by messages
            msgs = all_messages[channel]
            sub_chunks = _chunk_messages(msgs, MAX_CHARS)
            # each sub_chunk becomes its own combined chunk as a single-channel section
            for sc in sub_chunks:
                combined_chunks.append([(channel, _format(channel, sc))])
            continue

        if current and current_len + sec_len > MAX_CHARS:
            combined_chunks.append(current)
            current = [(channel, section_text)]
            current_len = sec_len
        else:
            current.append((channel, section_text))
            current_len += sec_len

    if current:
        combined_chunks.append(current)

    logger.info(
        "Summarize all: %d channels -> %d combined chunks (max_chars=%d)",
        len(all_messages),
        len(combined_chunks),
        MAX_CHARS,
    )

    intermediate_summaries = []
    # Summarize each combined chunk (each chunk may contain multiple channel sections)
    for idx, chunk in enumerate(combined_chunks):
        content = "\n\n".join(section for _, section in chunk)
        prompt = f"Summarize these Telegram channel messages:\n\n{content}"
        logger.debug(
            "Summarizing combined chunk %d/%d (approx %d chars)",
            idx + 1,
            len(combined_chunks),
            len(content),
        )
        try:
            s = _call_provider_summarize(prompt)
            intermediate_summaries.append(s.strip())
        except Exception as e:
            logger.exception("Failed to summarize combined chunk %d", idx + 1)
            return f"Error generating summary: {e}"

    # If only one intermediate summary, return it. Otherwise combine the intermediate summaries.
    if not intermediate_summaries:
        return f"No messages to summarize."

    if len(intermediate_summaries) == 1:
        return f"Daily digest (last {hours}h):\n\n{intermediate_summaries[0]}"

    combined = "\n\n".join(
        f"Part {i + 1} summary:\n{txt}" for i, txt in enumerate(intermediate_summaries)
    )
    final_prompt = (
        "Combine and condense the following partial digests into a single concise daily digest, "
        "remove duplicates, and keep only key points:\n\n" + combined
    )
    try:
        final_summary = _call_provider_summarize(final_prompt)
        return f"Daily digest (last {hours}h):\n\n{final_summary.strip()}"
    except Exception as e:
        logger.exception("Failed to produce final combined digest")
        return f"Error generating final summary: {e}"
