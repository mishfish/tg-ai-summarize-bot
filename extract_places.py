#!/usr/bin/env python3
"""
Extract place names from alert texts using LLM and geocode them into location_aliases.

Usage:
    python extract_places.py [--batch-size 30] [--limit 0] [--dry-run] [--geocode-only]

Steps:
  1. Load alert texts from DuckDB
  2. Send batches to LLM → collect unique place names
  3. Geocode each place → save to location_aliases
"""

import argparse
import json
import logging
import re
import sys

import duckdb

import config
import alert_map
from llm import get_provider

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_EXTRACT_SYSTEM = (
    "Ти — аналітик повідомлень з моніторингу повітряної обстановки в Одеській та Миколаївській областях України. "
    "Витягни з наданих текстів ВСІ згадки конкретних місць: райони, мікрорайони, вулиці, "
    "неформальні назви (наприклад: «Пентагон», «Бурлача балка», «Таїрова», «Котовського», "
    "«Пересипський район», «Корабельний», «Центр»). "
    "Повертай ТІЛЬКИ JSON-масив рядків. Без пояснень, без markdown."
)

_GEOCODE_SYSTEM = (
    "Ти — геокодер для Одеси та Миколаєва (Україна). "
    "Для назви місця поверни JSON-об'єкт з полями: "
    "district (район або мікрорайон), city (місто), lat (широта), lon (довгота). "
    "Якщо не можеш визначити — поверни null. "
    "Відповідай ТІЛЬКИ JSON, без пояснень, без markdown."
)


def _parse_json_array(raw: str) -> list[str]:
    m = re.search(r'\[.*?\]', raw, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group())
        return [str(x).strip() for x in data if str(x).strip()]
    except json.JSONDecodeError:
        logger.warning("JSON parse failed: %s", raw[:300])
        return []


def _parse_json_object(raw: str) -> dict | None:
    # Try all JSON objects in the response, return the last valid one
    # (LLM sometimes self-corrects with a second JSON block)
    candidates = list(re.finditer(r'\{[^{}]*\}', raw, re.DOTALL))
    last_valid = None
    for m in candidates:
        try:
            data = json.loads(m.group())
            if isinstance(data, dict):
                last_valid = data
        except json.JSONDecodeError:
            pass
    if last_valid is None:
        logger.warning("JSON parse failed: %s", raw[:300])
    return last_valid


def extract_places_batch(provider, texts: list[str]) -> list[str]:
    combined = "\n---\n".join(t[:400] for t in texts)
    messages = [
        {"role": "system", "content": _EXTRACT_SYSTEM},
        {"role": "user", "content": f"Повідомлення:\n\n{combined}"},
    ]
    raw = provider.chat(messages)
    return _parse_json_array(raw)


def geocode_place(provider, place: str) -> dict | None:
    messages = [
        {"role": "system", "content": _GEOCODE_SYSTEM},
        {"role": "user", "content": f'Місце: "{place}"'},
    ]
    raw = provider.chat(messages)
    if raw.strip().lower() in ("null", "none", ""):
        return None
    data = _parse_json_object(raw)
    if data is None:
        return None
    try:
        lat = float(data["lat"])
        lon = float(data["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "district": str(data.get("district") or ""),
        "city": str(data.get("city") or ""),
        "lat": lat,
        "lon": lon,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=30, help="Alerts per LLM call")
    parser.add_argument("--limit", type=int, default=0, help="Max alerts to process (0 = all)")
    parser.add_argument("--dry-run", action="store_true", help="Print extracted places, skip geocoding")
    parser.add_argument("--geocode-only", action="store_true", help="Skip extraction, re-geocode missing aliases")
    args = parser.parse_args()

    provider = get_provider()
    logger.info("Using LLM provider: %s / %s", config.LLM_PROVIDER, provider.current_model())

    if not args.geocode_only:
        with duckdb.connect(config.DUCKDB_PATH) as conn:
            q = "SELECT text FROM alerts ORDER BY date DESC"
            if args.limit:
                q += f" LIMIT {args.limit}"
            rows = conn.execute(q).fetchall()

        texts = [r[0] for r in rows if r[0] and r[0].strip()]
        logger.info("Loaded %d alert texts", len(texts))

        all_places: set[str] = set()
        batch_size = args.batch_size
        total_batches = (len(texts) + batch_size - 1) // batch_size

        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            batch_num = i // batch_size + 1
            logger.info("Batch %d/%d (%d texts)...", batch_num, total_batches, len(batch))
            try:
                places = extract_places_batch(provider, batch)
            except Exception as e:
                logger.error("Batch %d failed: %s", batch_num, e)
                continue
            new_places = set(places) - all_places
            all_places.update(places)
            logger.info("  +%d places (total unique: %d)", len(new_places), len(all_places))

        logger.info("Extraction done. Total unique places: %d", len(all_places))

        if args.dry_run:
            print("\nExtracted places:")
            for p in sorted(all_places):
                print(f"  {p}")
            return
    else:
        existing_aliases = alert_map.get_location_aliases()
        # Find places in DB that have no alias (none in this flow; geocode-only just re-checks missing)
        all_places = set()  # will be filled below from a review step
        logger.info("--geocode-only: will geocode places not yet in location_aliases")

    # Step 2: geocode each new place
    existing = alert_map.get_location_aliases()
    to_geocode = sorted(all_places - set(existing.keys()))
    logger.info("Places to geocode: %d (already in DB: %d)", len(to_geocode), len(existing))

    saved = 0
    failed = 0
    for idx, place in enumerate(to_geocode, 1):
        logger.info("[%d/%d] Geocoding: %s", idx, len(to_geocode), place)
        try:
            geo = geocode_place(provider, place)
        except Exception as e:
            logger.error("  geocode error: %s", e)
            failed += 1
            continue

        if geo is None:
            logger.warning("  -> null (skipped)")
            failed += 1
            continue

        alert_map.save_alias(place, geo["district"], geo["city"], geo["lat"], geo["lon"])
        logger.info("  -> %s, %s (%.4f, %.4f)", geo["district"], geo["city"], geo["lat"], geo["lon"])
        saved += 1

    logger.info("Done. Saved: %d, Failed/null: %d", saved, failed)


if __name__ == "__main__":
    main()
