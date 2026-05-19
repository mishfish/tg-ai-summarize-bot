import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ollama


DEFAULT_HOST = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3:8b"
DEFAULT_PROMPT = "Reply with exactly: OK"
DEFAULT_STATE_FILE = "data/state.json"
DEFAULT_MESSAGES_FILE = "data/messages.json"
DEFAULT_CACHE_FILE = "data/message_summary_cache.json"
DEFAULT_MAX_MESSAGE_CHARS = 500
DEFAULT_MIN_TEXT_CHARS = 20
DEFAULT_TIMEOUT = 60.0
DEFAULT_LANGUAGE = "uk"

MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
URL_RE = re.compile(r"https?://\S+")
MARKDOWN_DECORATION_RE = re.compile(r"[*_`~>#]+")
WHITESPACE_RE = re.compile(r"\s+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal Ollama connectivity test.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Ollama host URL.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Installed model to test.")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="Prompt to send.")
    parser.add_argument(
        "--language",
        choices=("uk", "en"),
        default=DEFAULT_LANGUAGE,
        help="Language for generated summaries.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.2,
        help="Sampling temperature for Ollama.",
    )
    parser.add_argument(
        "--num-predict",
        type=int,
        default=180,
        help="Maximum tokens to generate.",
    )
    parser.add_argument(
        "--repeat-penalty",
        type=float,
        default=1.1,
        help="Penalty for repeated tokens.",
    )
    parser.add_argument(
        "--num-ctx",
        type=int,
        default=8192,
        help="Context window requested from Ollama.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Seed for reproducible outputs.",
    )
    parser.add_argument(
        "--think",
        choices=("true", "false"),
        default="false",
        help="Whether to enable reasoning mode on models that support it.",
    )
    parser.add_argument(
        "--state-file",
        help=f"Read JSON from a file such as {DEFAULT_STATE_FILE} and send it to the model.",
    )
    parser.add_argument(
        "--messages-file",
        help=(
            f"Read messages JSON from a file such as {DEFAULT_MESSAGES_FILE} and "
            "summarize the latest messages from each channel."
        ),
    )
    parser.add_argument(
        "--messages-per-channel",
        type=int,
        default=1,
        help="How many latest filtered messages to include from each channel.",
    )
    parser.add_argument(
        "--channel",
        action="append",
        help="Limit messages mode to one channel. Repeat to include several channels.",
    )
    parser.add_argument(
        "--overall-from-intermediate",
        action="store_true",
        help="After per-channel summaries, generate one overall summary from them.",
    )
    parser.add_argument(
        "--overall-from-file",
        help="Load saved intermediate summaries from JSON and generate one overall summary.",
    )
    parser.add_argument(
        "--save-intermediate",
        help="Write per-channel summaries and metadata to a JSON file.",
    )
    parser.add_argument(
        "--cache-file",
        default=DEFAULT_CACHE_FILE,
        help="JSON cache file for per-channel summaries.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable reading and writing the per-channel summary cache.",
    )
    parser.add_argument(
        "--one-line",
        action="store_true",
        help="Ask for a one-line summary instead of a short paragraph.",
    )
    parser.add_argument(
        "--max-message-chars",
        type=int,
        default=DEFAULT_MAX_MESSAGE_CHARS,
        help="Trim each cleaned message to this many characters before sending to the model.",
    )
    parser.add_argument(
        "--min-text-chars",
        type=int,
        default=DEFAULT_MIN_TEXT_CHARS,
        help="Drop messages whose cleaned text is shorter than this threshold.",
    )
    time_group = parser.add_mutually_exclusive_group()
    time_group.add_argument(
        "--since-hours",
        type=float,
        help="Only include messages newer than this many hours.",
    )
    time_group.add_argument(
        "--since",
        help="Only include messages newer than this ISO datetime.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="Request timeout in seconds.",
    )
    return parser.parse_args()


def parse_datetime(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def get_cutoff(args: argparse.Namespace) -> datetime | None:
    if args.since_hours is not None:
        return datetime.now(timezone.utc) - timedelta(hours=args.since_hours)
    if args.since:
        return parse_datetime(args.since)
    return None


def normalize_text(text: str) -> str:
    text = MARKDOWN_LINK_RE.sub(r"\1", text)
    text = URL_RE.sub("", text)
    text = MARKDOWN_DECORATION_RE.sub(" ", text)
    text = WHITESPACE_RE.sub(" ", text)
    return text.strip()


def shrink_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def classify_channel(channel: str, cleaned_messages: list[str]) -> str:
    channel_lc = channel.lower()
    blob = " ".join(cleaned_messages).lower()

    if any(token in channel_lc or token in blob for token in ("steam", "bundle", "discount", "sale", "зниж", "акція")):
        return "deals"
    if any(token in blob for token in ("breaking", "source", "report", "strike", "attack", "санкц", "удар", "заяв", "вибух", "iran", "trump")):
        return "news"
    if "чат" in blob or "question" in blob or sum(len(text) for text in cleaned_messages) < 200:
        return "chat"
    return "analysis"


def language_name(code: str) -> str:
    return "Ukrainian" if code == "uk" else "English"


def channel_instruction(kind: str, one_line: bool, language: str) -> str:
    lang = language_name(language)
    if kind == "deals":
        return (
            f"Summarize this channel's latest deal post in one sentence. Reply in {lang}."
            if one_line
            else f"Summarize this channel's latest deal post in {lang}. Mention the item, the offer, and any deadline if present."
        )
    if kind == "news":
        return (
            f"Summarize this channel's latest news in one sentence. Reply in {lang}."
            if one_line
            else f"Summarize this channel's latest news briefly and concretely in {lang}. Focus on the main event and why it matters."
        )
    if kind == "chat":
        return (
            f"Summarize the main point of these chat messages in one sentence. Reply in {lang}."
            if one_line
            else f"Summarize the main point or mood of these chat-like messages briefly in {lang}."
        )
    return (
        f"Summarize the main takeaway from these messages in one sentence. Reply in {lang}."
        if one_line
        else f"Summarize the main takeaway from these messages briefly and clearly in {lang}."
    )


def overall_instruction(one_line: bool, language: str) -> str:
    lang = language_name(language)
    if one_line:
        return (
            f"Create one concrete sentence in {lang} that captures the main cross-channel themes. "
            "If multiple channels report the same fact, mention it only once."
        )
    return (
        f"Create one brief but concrete overall summary in {lang} from these per-channel summaries. "
        "Merge overlapping reports from different channels and do not repeat the same information twice. "
        "Highlight only the distinct main themes."
    )


def build_prompt(args: argparse.Namespace) -> str:
    if args.state_file and args.messages_file:
        raise ValueError("Use either --state-file or --messages-file, not both")
    if args.messages_file and args.overall_from_file:
        raise ValueError("Use either --messages-file or --overall-from-file, not both")

    if not args.state_file:
        return args.prompt

    state_path = Path(args.state_file)
    with state_path.open() as f:
        state = json.load(f)

    state_json = json.dumps(state, ensure_ascii=False, indent=2)
    if args.prompt == DEFAULT_PROMPT:
        instruction = (
            "Read this bot state JSON and briefly explain what channels are monitored, "
            "what channels are used for alerts, what the alert target is, and how many "
            f"authorized users there are. Reply in {language_name(args.language)}."
        )
    else:
        instruction = args.prompt

    return f"{instruction}\n\nJSON:\n{state_json}"


def load_message_payloads(args: argparse.Namespace) -> list[dict]:
    messages_path = Path(args.messages_file)
    with messages_path.open() as f:
        messages_by_channel = json.load(f)

    if args.messages_per_channel < 1:
        raise ValueError("--messages-per-channel must be at least 1")
    if args.max_message_chars < 1:
        raise ValueError("--max-message-chars must be at least 1")
    if args.min_text_chars < 0:
        raise ValueError("--min-text-chars must be at least 0")

    cutoff = get_cutoff(args)
    selected_channels = set(args.channel or [])
    payloads = []

    for channel in sorted(messages_by_channel):
        if selected_channels and channel not in selected_channels:
            continue

        prepared_messages = []
        seen_texts = set()
        for message in messages_by_channel[channel]:
            date_raw = message.get("date", "")
            if not date_raw:
                continue

            date = parse_datetime(date_raw)
            if cutoff and date < cutoff:
                continue

            cleaned = normalize_text(message.get("text", ""))
            if len(cleaned) < args.min_text_chars:
                continue
            if cleaned in seen_texts:
                continue
            seen_texts.add(cleaned)

            prepared_messages.append(
                {
                    "date": date_raw,
                    "sender": message.get("sender", ""),
                    "text": shrink_text(cleaned, args.max_message_chars),
                }
            )

        prepared_messages = prepared_messages[-args.messages_per_channel :]
        if not prepared_messages:
            continue

        cleaned_texts = [message["text"] for message in prepared_messages]
        kind = classify_channel(channel, cleaned_texts)
        section_lines = [f"=== {channel} ==="]
        for message in prepared_messages:
            sender = message["sender"]
            if sender:
                section_lines.append(f"[{message['date']}] {sender}: {message['text']}")
            else:
                section_lines.append(f"[{message['date']}] {message['text']}")

        source_hash = hashlib.sha256(
            json.dumps(prepared_messages, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        payloads.append(
            {
                "channel": channel,
                "kind": kind,
                "message_count": len(prepared_messages),
                "messages": prepared_messages,
                "section": "\n".join(section_lines),
                "source_hash": source_hash,
            }
        )

    if not payloads:
        if selected_channels:
            requested = ", ".join(sorted(selected_channels))
            raise ValueError(f"no messages found for selected channel(s): {requested}")
        raise ValueError("messages file does not contain any messages after filtering")

    return payloads


def build_channel_prompt(args: argparse.Namespace, payload: dict) -> str:
    if args.prompt == DEFAULT_PROMPT:
        instruction = channel_instruction(payload["kind"], args.one_line, args.language)
    else:
        instruction = args.prompt

    return (
        f"{instruction}\n\n"
        f"Channel: {payload['channel']}\n"
        f"Detected type: {payload['kind']}\n"
        f"Included: {payload['message_count']} latest filtered message(s).\n\n"
        f"{payload['section']}"
    )


def build_overall_prompt(args: argparse.Namespace, intermediate_entries: list[dict]) -> str:
    if args.prompt == DEFAULT_PROMPT:
        instruction = overall_instruction(args.one_line, args.language)
    else:
        instruction = args.prompt

    sections = [
        f"=== {entry['channel']} ({entry['kind']}) ===\n{entry['summary']}"
        for entry in intermediate_entries
    ]
    return f"{instruction}\n\n" + "\n\n".join(sections)


def run_chat(client: ollama.Client, args: argparse.Namespace, prompt: str) -> str:
    options = {
        "temperature": args.temperature,
        "num_predict": args.num_predict,
        "repeat_penalty": args.repeat_penalty,
        "num_ctx": args.num_ctx,
        "seed": args.seed,
    }
    response = client.chat(
        model=args.model,
        messages=[{"role": "user", "content": prompt}],
        stream=False,
        think=args.think == "true",
        options=options,
    )
    return response.message.content.strip()


def load_cache(path: Path) -> dict:
    if not path.exists():
        return {"entries": {}}
    with path.open() as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return {"entries": {}}
    data.setdefault("entries", {})
    return data


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def cache_key(args: argparse.Namespace, payload: dict) -> str:
    material = {
        "model": args.model,
        "prompt": args.prompt,
        "one_line": args.one_line,
        "channel": payload["channel"],
        "kind": payload["kind"],
        "source_hash": payload["source_hash"],
    }
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def build_intermediate_file(args: argparse.Namespace, entries: list[dict]) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "messages_file": args.messages_file,
        "messages_per_channel": args.messages_per_channel,
        "max_message_chars": args.max_message_chars,
        "min_text_chars": args.min_text_chars,
        "since_hours": args.since_hours,
        "since": args.since,
        "one_line": args.one_line,
        "channels": entries,
    }


def load_intermediate_entries(path: Path) -> list[dict]:
    with path.open() as f:
        data = json.load(f)
    entries = data.get("channels", [])
    if not entries:
        raise ValueError("intermediate file does not contain any channel summaries")
    return entries


def run_overall_from_entries(
    client: ollama.Client, args: argparse.Namespace, entries: list[dict]
) -> int:
    try:
        summary = run_chat(client, args, build_overall_prompt(args, entries))
    except Exception as exc:
        print(
            f"## Overall\nERROR: {exc.__class__.__name__}: {exc}\n",
            file=sys.stderr,
        )
        return 5

    print(f"## Overall\n{summary}\n")
    return 0


def main() -> int:
    args = parse_args()
    client = ollama.Client(host=args.host, timeout=args.timeout)

    try:
        available_models = [model.model for model in client.list().models]
    except Exception as exc:
        print(
            f"Cannot reach Ollama at {args.host}: {exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    if args.model not in available_models:
        installed = ", ".join(available_models) if available_models else "none"
        print(
            f"Model {args.model!r} is not installed. Available models: {installed}",
            file=sys.stderr,
        )
        return 2

    if args.overall_from_file:
        try:
            entries = load_intermediate_entries(Path(args.overall_from_file))
        except Exception as exc:
            print(
                f"Cannot load intermediate file: {exc.__class__.__name__}: {exc}",
                file=sys.stderr,
            )
            return 4
        return run_overall_from_entries(client, args, entries)

    if args.messages_file:
        try:
            payloads = load_message_payloads(args)
        except Exception as exc:
            print(
                f"Cannot prepare input: {exc.__class__.__name__}: {exc}",
                file=sys.stderr,
            )
            return 4

        cache = {"entries": {}}
        cache_dirty = False
        cache_path = Path(args.cache_file)
        if not args.no_cache:
            try:
                cache = load_cache(cache_path)
            except Exception as exc:
                print(
                    f"Cannot read cache file {args.cache_file!r}: {exc.__class__.__name__}: {exc}",
                    file=sys.stderr,
                )
                return 4

        failures = 0
        intermediate_entries = []
        for payload in payloads:
            key = cache_key(args, payload)
            cached_entry = None if args.no_cache else cache["entries"].get(key)

            if cached_entry:
                summary = cached_entry["summary"]
                cached = True
            else:
                try:
                    summary = run_chat(client, args, build_channel_prompt(args, payload))
                except Exception as exc:
                    failures += 1
                    print(
                        f"## {payload['channel']}\nERROR: {exc.__class__.__name__}: {exc}\n",
                        file=sys.stderr,
                    )
                    continue
                cached = False
                if not args.no_cache:
                    cache["entries"][key] = {
                        "summary": summary,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "channel": payload["channel"],
                        "kind": payload["kind"],
                        "source_hash": payload["source_hash"],
                    }
                    cache_dirty = True

            entry = {
                "channel": payload["channel"],
                "kind": payload["kind"],
                "summary": summary,
                "cached": cached,
                "message_count": payload["message_count"],
                "source_hash": payload["source_hash"],
                "messages": payload["messages"],
            }
            intermediate_entries.append(entry)
            source = "cache" if cached else "llm"
            print(f"## {payload['channel']} [{source}]\n{summary}\n")

        if cache_dirty:
            save_json(cache_path, cache)

        if args.save_intermediate:
            save_json(Path(args.save_intermediate), build_intermediate_file(args, intermediate_entries))

        if args.overall_from_intermediate and intermediate_entries:
            overall_status = run_overall_from_entries(client, args, intermediate_entries)
            if overall_status != 0:
                failures += 1

        return 0 if failures == 0 else 5

    try:
        prompt = build_prompt(args)
    except Exception as exc:
        print(
            f"Cannot prepare input: {exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )
        return 4

    try:
        response = run_chat(client, args, prompt)
    except Exception as exc:
        print(
            f"Chat request failed for {args.model!r} after {args.timeout:.1f}s: "
            f"{exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )
        print(
            "Ollama is reachable, but the model did not produce a response.",
            file=sys.stderr,
        )
        return 3

    print(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
