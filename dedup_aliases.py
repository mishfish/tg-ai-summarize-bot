#!/usr/bin/env python3
"""
Dedup location_aliases: group synonyms and normalize to consistent coordinates.

Steps:
  1. Load all aliases from DuckDB (read-only safe)
  2. Send list to LLM → get duplicate groups with best coordinates
  3. Dry-run (default): print plan; --apply: update DB

Usage:
    python dedup_aliases.py [--apply] [--max-tokens 8192]
"""

import argparse
import json
import logging
import re

import duckdb

import config
import alert_map
from llm import get_provider

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_DEDUP_SYSTEM = (
    "Ти — редактор географічної бази даних для системи моніторингу тривог Одеської та Миколаївської областей України. "
    "Тобі надано список aliases місць з їхніми поточними координатами. "
    "Знайди групи дублікатів — aliases що позначають ОДНЕ й те саме реальне місце "
    "(різне написання, мова, транслітерація, скорочення, помилки). "
    "Для кожної групи визнач найкращі координати (lat/lon), район (district) та місто (city) — "
    "вибирай ті що точніше відповідають реальному місцю розташування. "
    "Включай тільки групи де є 2 або більше aliases. Одиночні aliases не включай. "
    "Відповідай ТІЛЬКИ JSON-масивом без пояснень та markdown:\n"
    "[\n"
    '  {"canonical": "найкраща назва", "district": "район", "city": "місто", '
    '"lat": 46.XXXX, "lon": 30.XXXX, "variants": ["варіант1", "варіант2", ...]},\n'
    "  ...\n"
    "]"
)


def _parse_groups(raw: str) -> list[dict]:
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        logger.warning("No JSON array found in LLM response")
        return []
    try:
        data = json.loads(m.group())
    except json.JSONDecodeError as e:
        logger.warning("JSON parse failed: %s\n%s", e, raw[:500])
        return []
    if not isinstance(data, list):
        return []
    groups = []
    for item in data:
        if not isinstance(item, dict):
            continue
        variants = item.get("variants", [])
        if not isinstance(variants, list) or len(variants) < 2:
            continue
        try:
            lat = float(item["lat"])
            lon = float(item["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        groups.append({
            "canonical": str(item.get("canonical", variants[0])),
            "district": str(item.get("district", "")),
            "city": str(item.get("city", "")),
            "lat": lat,
            "lon": lon,
            "variants": [str(v) for v in variants],
        })
    return groups


def ask_llm_for_groups(provider, aliases: dict) -> list[dict]:
    lines = []
    for alias, geo in sorted(aliases.items()):
        lines.append(
            f'"{alias}" → {geo["district"]}, {geo["city"]} ({geo["lat"]:.4f}, {geo["lon"]:.4f})'
        )
    alias_text = "\n".join(lines)

    messages = [
        {"role": "system", "content": _DEDUP_SYSTEM},
        {"role": "user", "content": f"Список aliases ({len(aliases)} шт.):\n\n{alias_text}"},
    ]
    logger.info("Sending %d aliases to LLM for dedup analysis...", len(aliases))
    raw = provider.chat(messages)
    logger.debug("LLM raw response:\n%s", raw[:1000])
    return _parse_groups(raw)


def apply_groups(groups: list[dict], db_path: str, dry_run: bool) -> None:
    total_updated = 0

    for group in groups:
        canonical = group["canonical"]
        district = group["district"]
        city = group["city"]
        lat = group["lat"]
        lon = group["lon"]
        variants = group["variants"]

        print(f"\n{'[DRY-RUN] ' if dry_run else ''}Group: {canonical!r}")
        print(f"  → {district}, {city} ({lat:.4f}, {lon:.4f})")
        print(f"  Variants ({len(variants)}): {', '.join(repr(v) for v in variants)}")

        if dry_run:
            continue

        with duckdb.connect(db_path) as conn:
            for variant in variants:
                conn.execute(
                    "UPDATE location_aliases SET district=?, city=?, lat=?, lon=? WHERE alias=?",
                    [district, city, lat, lon, variant],
                )
                total_updated += 1

    if not dry_run:
        logger.info("Updated %d alias records across %d groups", total_updated, len(groups))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write changes to DB (default: dry-run)")
    parser.add_argument("--max-tokens", type=int, default=8192, help="LLM max_tokens for this call")
    args = parser.parse_args()

    # Override max_tokens for this large response
    config.MAX_TOKENS = args.max_tokens

    provider = get_provider()
    logger.info("LLM: %s / %s", config.LLM_PROVIDER, provider.current_model())

    aliases = alert_map.get_location_aliases()
    if not aliases:
        logger.error("No aliases in DB. Run extract_places.py first.")
        return

    logger.info("Loaded %d aliases from DB", len(aliases))

    groups = ask_llm_for_groups(provider, aliases)
    logger.info("Found %d duplicate groups", len(groups))

    if not groups:
        print("No duplicate groups found.")
        return

    if not args.apply:
        print(f"\n=== DRY RUN: {len(groups)} groups would be normalized ===")
        print("Run with --apply to write changes.\n")

    apply_groups(groups, config.DUCKDB_PATH, dry_run=not args.apply)

    if not args.apply:
        total_variants = sum(len(g["variants"]) for g in groups)
        print(f"\nSummary: {len(groups)} groups, {total_variants} aliases to normalize.")


if __name__ == "__main__":
    main()
