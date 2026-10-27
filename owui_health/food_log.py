"""
title: Food Log
description: Log what the user ate, following the food-logging constitution: structured items and portions only, validated and checked against USDA FoodData Central before anything is saved. Entries go to the user's food inbox; the nightly health ingest re-validates them and looks up nutrients in USDA FoodData Central. The model never estimates calories. Entries are never edited: corrections are new entries that supersede old ones.
version: 0.2.0
license: MIT
"""

import asyncio
import json
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field

# Identical to food_constitution.json in the compose repo's health/ stack (a test checks this).
# Change both together, and bump the version.
CONSTITUTION = json.loads(r"""
{
  "name": "food-logging-constitution",
  "version": "2",
  "purpose": "Every food log entry is recorded the same way, so nightly ingest can resolve nutrients deterministically from USDA FoodData Central. The model records what was eaten; it never estimates calories or macros.",
  "rules": [
    {"id": "R1", "text": "One entry per eating occasion. meal is one of breakfast, lunch, dinner, snack."},
    {"id": "R2", "text": "eaten_at is the local date and time the food was eaten (YYYY-MM-DDTHH:MM). It is not in the future and not more than 7 days ago."},
    {"id": "R3", "text": "Each item name is one specific food in lowercase, the food first, then its type and preparation when known: 'egg, scrambled', 'bread, whole wheat, toasted', 'milk, 2%', 'lettuce, romaine', 'spinach, raw'. Name the food, not a category: not 'greens', 'salad', 'vegetables', 'fruit' or 'meat'. A dish USDA lists as a whole (fried rice, miso soup, lasagna) is one item; any other dish is split into its foods, each with its own portion ('salad with ginger dressing' becomes 'lettuce, iceberg' and 'salad dressing, ginger'). No brand in the name, no quantities in the name."},
    {"id": "R4", "text": "quantity is a positive number and unit is one of g, ml, oz, cup, tbsp, tsp, piece, slice, serving. Prefer g or ml when the user gives a weight or volume."},
    {"id": "R5", "text": "grams is set only when the user stated a weight or the package says it. Never invent grams."},
    {"id": "R6", "text": "brand and barcode are set only when the user said them or they are visible in a photo. Never invent them."},
    {"id": "R7", "text": "If the portion is unknown, ask the user. If they cannot say, use quantity 1 and unit serving, and set portion_estimated to true."},
    {"id": "R8", "text": "Logged entries are never edited. To correct one, log a new entry with supersedes set to the old entry_id and the full corrected item list. To delete one, log an entry with supersedes set, void true and no items."},
    {"id": "R9", "text": "source is text when the user described the food, photo when it was identified from an image. confidence is high when the user stated foods and portions, medium when some portions were estimated, low when identified mostly from a photo."},
    {"id": "R10", "text": "Restate the entry to the user and get confirmation before calling food_log_add. Do not log drinks of plain water."},
    {"id": "R11", "text": "food_log_add checks each item against USDA FoodData Central before saving. If it reports an item as no_match (no USDA food for the name) or no_portion (the unit can't be converted to grams), nothing is saved: tell the user and fix the item with them, using a more specific name, splitting the dish, the brand or barcode, the weight in grams, or a unit from the listed portions. Save it unchanged with accept_unmatched only when the user asks for that. After saving, tell the user which USDA food each item matched, so they can catch a wrong match."}
  ],
  "schema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": false,
    "required": ["entry_id", "constitution_version", "owui_user_id", "person", "logged_at", "eaten_at", "meal", "items", "source", "confidence", "void"],
    "properties": {
      "entry_id": {"type": "string", "format": "uuid"},
      "constitution_version": {"enum": ["1", "2"]},
      "owui_user_id": {"type": "string", "minLength": 1},
      "person": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,30}$"},
      "logged_at": {"type": "string", "format": "date-time"},
      "eaten_at": {"type": "string", "format": "date-time"},
      "meal": {"enum": ["breakfast", "lunch", "dinner", "snack"]},
      "source": {"enum": ["text", "photo"]},
      "confidence": {"enum": ["high", "medium", "low"]},
      "note": {"type": "string", "maxLength": 500},
      "supersedes": {"type": "string", "format": "uuid"},
      "void": {"type": "boolean"},
      "items": {
        "type": "array",
        "maxItems": 40,
        "items": {
          "type": "object",
          "additionalProperties": false,
          "required": ["name", "quantity", "unit", "portion_estimated"],
          "properties": {
            "name": {"type": "string", "pattern": "^[a-z0-9][a-z0-9 ,()'%&/.-]{1,79}$",
                     "not": {"enum": ["greens", "vegetables", "veggies", "fruit", "fruits", "salad", "meat", "protein", "snack", "dessert", "food", "leftovers"]}},
            "quantity": {"type": "number", "exclusiveMinimum": 0, "maximum": 5000},
            "unit": {"enum": ["g", "ml", "oz", "cup", "tbsp", "tsp", "piece", "slice", "serving"]},
            "grams": {"type": "number", "minimum": 1, "maximum": 3000},
            "brand": {"type": "string", "minLength": 1, "maxLength": 80},
            "barcode": {"type": "string", "pattern": "^[0-9]{8,14}$"},
            "portion_estimated": {"type": "boolean"}
          }
        }
      }
    },
    "if": {"properties": {"void": {"const": true}}},
    "then": {"required": ["supersedes"], "properties": {"items": {"maxItems": 0}}},
    "else": {"properties": {"items": {"minItems": 1}}}
  }
}
""")

# ---- BEGIN foodmatch.py: verbatim copy of health/healthingest/foodmatch.py (a test checks it) ----
"""Matching a logged food name to a USDA FoodData Central food, and its portion to grams.

Pure functions, no network or database: the health ingest (usda.py) and the Open WebUI Food Log tool both use
them, so the tool's check at logging time agrees with what the ingest stores. The Food Log tool embeds this
file verbatim (a test checks it); change both together.
"""

import re

# Generic foods, in order of preference when two candidates match equally well: FNDDS describes foods as eaten
# and has household portions, Foundation and SR Legacy are ingredient-level.
GENERIC_TYPES = ("Survey (FNDDS)", "Foundation", "SR Legacy")
SEARCH_PAGE = 25

OZ_G = 28.349523125
UNIT_WORDS = {
    "cup": ("cup",),
    "tbsp": ("tbsp", "tablespoon"),
    "tsp": ("tsp", "teaspoon"),
    "slice": ("slice",),
    "piece": ("piece", "each", "medium", "item", "whole", "large", "small", "unit"),
    "serving": ("serving", "portion"),
}


STOPWORDS = {"a", "an", "and", "or", "with", "without", "in", "of", "the", "to", "from", "nfs", "ns", "as"}
# Words for a food's default form: they don't count as extra, so "egg" picks "Egg, whole, raw" over "egg white"
DEFAULT_FORM = {"whole", "raw", "fresh", "plain", "regular", "unprepared"}


def _stem(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("ches", "shes", "xes", "sses", "oes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")) and len(word) > 3:
        return word[:-1]
    return word


def _tokens(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS}


def _ranked(name: str, foods: list[dict]) -> list[tuple]:
    head, _, rest = name.partition(",")
    want_head, want_rest = _tokens(head), _tokens(rest) - _tokens(head)
    want = want_head | want_rest
    ranked = []
    for f in foods:
        desc = f.get("description") or ""
        if f.get("dataType") not in GENERIC_TYPES or not desc:
            continue
        have = _tokens(desc)
        if not want_head <= have or not _tokens(desc.partition(",")[0]) <= want:
            continue
        ranked.append((
            len(want_rest - have), len(have - want - DEFAULT_FORM), GENERIC_TYPES.index(f["dataType"]), int(f["fdcId"]),
        ))
    return sorted(ranked)


def best_match(name: str, foods: list[dict]) -> int | None:
    """The FDC food a generic name means, or None. Deterministic for a given search result.

    A candidate qualifies when it contains every word of the name's head (the part before the first comma)
    and its own head (before its first comma) uses only words from the name: "greens" never becomes
    "Olives, green", and "almond" never becomes "Almond butter". Among qualifying candidates the best one
    matches the most of the name's other words, adds the fewest extra words (not counting DEFAULT_FORM),
    and comes from the preferred data type; ties go to the lowest fdcId.
    """
    ranked = _ranked(name, foods)
    return ranked[0][3] if ranked else None


def _portion_amount(p: dict) -> float:
    """How many units a portion's gramWeight covers: its amount, or the leading number of "1/2 cup"."""
    if p.get("amount"):
        return float(p["amount"])
    m = re.match(r"\s*(\d+(?:\.\d+)?)(?:/(\d+))?\s", str(p.get("portionDescription") or ""))
    if not m:
        return 1.0
    return float(m.group(1)) / float(m.group(2)) if m.group(2) else float(m.group(1))


def _portion_text(p: dict) -> str:
    return " ".join(str(x) for x in (
        p.get("portionDescription"), p.get("modifier"), (p.get("measureUnit") or {}).get("name"),
    ) if x).lower()


def grams_for(item: dict, details: dict | None) -> tuple[float | None, str | None]:
    """Grams for a logged item, and how they were obtained (logged, unit, usda_portion)."""
    qty, unit = float(item["quantity"]), item["unit"]
    if item.get("grams"):
        return float(item["grams"]), "logged"
    if unit == "g":
        return qty, "unit"
    if unit == "oz":
        return qty * OZ_G, "unit"
    if unit == "ml":
        return qty, "unit"  # density of water; good enough for drinks, soups and milk
    if not details:
        return None, None
    if unit == "serving" and details.get("servingSize") and str(details.get("servingSizeUnit", "")).lower() in ("g", "grm"):
        return qty * float(details["servingSize"]), "usda_portion"
    portions = sorted(
        (p for p in details.get("foodPortions") or [] if p.get("gramWeight")),
        key=lambda p: (p.get("sequenceNumber") or 0, p.get("id") or 0),
    )

    def per_unit(match) -> float | None:
        p = next((p for p in portions if match(_portion_text(p))), None)
        return float(p["gramWeight"]) / _portion_amount(p) if p else None

    words = UNIT_WORDS.get(unit, ())
    grams = per_unit(lambda text: any(re.search(rf"\b{w}s?\b", text) for w in words))
    if grams is None and unit in ("tbsp", "tsp"):
        # FNDDS has cup weights but rarely spoon weights: 1 cup = 16 tbsp = 48 tsp
        cup = per_unit(lambda text: re.search(r"\bcups?\b", text))
        grams = cup / (16 if unit == "tbsp" else 48) if cup else None
    if grams is None and unit == "piece":
        # FNDDS names a count by the food itself: "1 egg", "1 nut"
        name = _tokens(item.get("name") or "") | {"nut", "berry"}

        def counted(text: str) -> bool:
            m = re.match(r"1 ([a-z]+)\b", text)
            return bool(m) and _stem(m.group(1)) in name

        grams = per_unit(counted)
    if grams is None and unit == "serving":
        # FNDDS's standard amount for a food eaten without a stated portion
        grams = per_unit(lambda text: "quantity not specified" in text)
    return (qty * grams, "usda_portion") if grams else (None, None)


def search_body(query: str) -> dict:
    """POST body for /foods/search over the generic data types."""
    return {"query": query, "dataType": list(GENERIC_TYPES), "pageSize": SEARCH_PAGE}


def barcode_body(barcode: str) -> dict:
    """POST body for /foods/search for a packaged food's barcode."""
    return {"query": barcode, "dataType": ["Branded"], "pageSize": 5}


def barcode_match(barcode: str, foods: list[dict]) -> int | None:
    """The branded food whose GTIN/UPC is this barcode (leading zeros ignored), or None."""
    hits = [f for f in foods if str(f.get("gtinUpc", "")).lstrip("0") == barcode.lstrip("0")]
    return int(hits[0]["fdcId"]) if hits else None


def raw_query(name: str, foods: list[dict]) -> str | None:
    """A second query to run when a bare name ("egg") only matched dishes: its raw form, else None."""
    best = _ranked(name, foods)
    return f"{name}, raw" if "," not in name and (not best or best[0][1] > 0) else None


def portions_listed(details: dict | None) -> list[str]:
    """Portion descriptions a food offers, to suggest a unit when the logged one can't be converted."""
    return [str(p.get("portionDescription") or (p.get("measureUnit") or {}).get("name") or "")
            for p in (details or {}).get("foodPortions") or [] if p.get("gramWeight")][:8]
# ---- END foodmatch.py ----


VALIDATOR = Draft202012Validator(CONSTITUTION["schema"], format_checker=Draft202012Validator.FORMAT_CHECKER)
MAX_AGE = timedelta(days=7)
CLOCK_SKEW = timedelta(minutes=10)

# First path element of a schema error -> the rule it breaks
RULE_FOR_FIELD = {
    "meal": "R1", "eaten_at": "R2", "name": "R3", "quantity": "R4", "unit": "R4", "grams": "R5",
    "brand": "R6", "barcode": "R6", "portion_estimated": "R7", "supersedes": "R8", "void": "R8",
    "items": "R8", "source": "R9", "confidence": "R9",
}
USDA_API = "https://api.nal.usda.gov/fdc/v1"
ENTRY_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class LogError(Exception):
    pass


def _violation(error) -> dict:
    path = [str(p) for p in error.absolute_path]
    field = next((p for p in reversed(path) if not p.isdigit()), "items" if path[:1] == ["items"] else "")
    if not path and error.validator in ("required", "maxItems", "minItems"):
        field = "supersedes" if "supersedes" in error.message else "items"
    message = error.message
    if error.validator == "not" and field == "name":
        message = f"{error.instance!r} is a category, not a specific food: name the food (rule R3)"
    return {"rule": RULE_FOR_FIELD.get(field, "schema"), "path": "/".join(path) or "(entry)", "message": message}


def _clean_items(items) -> list:
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except ValueError:
            raise LogError("items must be a JSON list of objects with name, quantity, unit.")
    if not isinstance(items, list):
        raise LogError("items must be a list.")
    cleaned = []
    for item in items:
        if not isinstance(item, dict):
            raise LogError("each item must be an object with name, quantity, unit.")
        item = {k: v for k, v in item.items() if v not in (None, "")}
        if isinstance(item.get("name"), str):
            item["name"] = re.sub(r"\s+", " ", item["name"]).strip().lower()
        if isinstance(item.get("unit"), str):
            item["unit"] = item["unit"].strip().lower()
        item.setdefault("portion_estimated", False)
        cleaned.append(item)
    return cleaned


def build_entry(person: str, tz: str, user_id: str, meal: str, eaten_at: str, items, source: str,
                confidence: str, note: str, supersedes: str, void: bool, now: datetime) -> dict:
    zone = ZoneInfo(tz)
    text = (eaten_at or "").strip().lower()
    if text == "now":
        eaten = now.astimezone(zone).replace(second=0, microsecond=0)
    else:
        try:
            eaten = datetime.fromisoformat(text.upper().replace(" ", "T"))
        except ValueError:
            raise LogError("R2: eaten_at must be a local date and time, YYYY-MM-DDTHH:MM.")
        eaten = eaten.replace(tzinfo=zone) if eaten.tzinfo is None else eaten.astimezone(zone)
    entry = {
        "entry_id": str(uuid.uuid4()),
        "constitution_version": CONSTITUTION["version"],
        "owui_user_id": user_id,
        "person": person,
        "logged_at": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "eaten_at": eaten.isoformat(timespec="minutes"),
        "meal": (meal or "").strip().lower(),
        "items": _clean_items(items if items is not None else []),
        "source": (source or "").strip().lower(),
        "confidence": (confidence or "").strip().lower(),
        "void": bool(void),
    }
    if note and note.strip():
        entry["note"] = note.strip()
    if supersedes and supersedes.strip():
        entry["supersedes"] = supersedes.strip().lower()
    return entry


def check_entry(entry: dict, now: datetime) -> list[dict]:
    violations = [_violation(e) for e in sorted(VALIDATOR.iter_errors(entry), key=lambda e: list(e.absolute_path))]
    if violations:
        return violations
    eaten = datetime.fromisoformat(entry["eaten_at"])
    if eaten > now + CLOCK_SKEW:
        violations.append({"rule": "R2", "path": "eaten_at", "message": "eaten_at is in the future"})
    elif eaten < now - MAX_AGE:
        violations.append({"rule": "R2", "path": "eaten_at", "message": "eaten_at is more than 7 days ago"})
    return violations


def _read(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _usda(api_key: str, method: str, path: str, body: dict | None = None) -> dict:
    """One FDC call, retried like the ingest's (USDA's search fails intermittently). Errors omit the URL and key."""
    problem = "no response"
    for attempt in range(3):
        try:
            resp = requests.request(method, f"{USDA_API}{path}", params={"api_key": api_key}, json=body, timeout=15)
        except requests.RequestException as e:
            problem = type(e).__name__
        else:
            if resp.status_code == 200:
                return resp.json()
            problem = f"HTTP {resp.status_code}"
            if resp.status_code not in (400, 429) and resp.status_code < 500:
                break
        if attempt < 2:
            time.sleep(1 + attempt)
    raise LookupError(f"USDA {path.strip('/').split('/')[0]}: {problem}")


def check_item(api_key: str, item: dict) -> dict:
    """What the ingest will make of one item: its USDA food and grams, or why it can't (rule R11).
    Same matching rules as the ingest (foodmatch above)."""
    out = {"name": item["name"]}
    try:
        fdc_id = None
        if item.get("barcode"):
            found = _usda(api_key, "POST", "/foods/search", barcode_body(item["barcode"])).get("foods") or []
            fdc_id = barcode_match(item["barcode"], found)
        foods = []
        if fdc_id is None:
            foods = _usda(api_key, "POST", "/foods/search", search_body(item["name"])).get("foods") or []
            if second := raw_query(item["name"], foods):
                foods += _usda(api_key, "POST", "/foods/search", search_body(second)).get("foods") or []
            fdc_id = best_match(item["name"], foods)
        if fdc_id is None:
            # suggestions share a word with the food itself, so "wrap, carb-friendly" isn't offered low-carb beer
            head, seen = _tokens(item["name"].partition(",")[0]), []
            for f in foods:
                d = (f.get("description") or "").lower()
                if f.get("dataType") in GENERIC_TYPES and d and d not in seen and _tokens(d) & head:
                    seen.append(d)
            return out | {"status": "no_match", "usda_suggestions": seen[:5]}
        details = _usda(api_key, "GET", f"/food/{fdc_id}")
    except LookupError as e:
        return out | {"status": "unchecked", "error": str(e)}
    out["usda_food"] = details.get("description")
    grams, how = grams_for(item, details)
    if grams is None:
        return out | {"status": "no_portion", "unit": item["unit"], "usda_portions": portions_listed(details)}
    return out | {"status": "matched", "grams": round(grams, 1), "grams_from": how}


class Tools:
    class Valves(BaseModel):
        PERSON_KEYS: str = Field(
            default="{}",
            description='JSON object mapping each Open WebUI user id to their health person key and time zone, '
                        'e.g. {"<user id>": {"key": "enrique", "timezone": "America/New_York"}}.',
        )
        INBOX_DIR: str = Field(default="/health_inbox", description="The food inbox mounted into Open WebUI.")
        USDA_FDC_API_KEY: str = Field(
            default="",
            description="USDA FoodData Central API key (the health stack's USDA_FDC_API_KEY or another free one). "
                        "With it, each item is checked against USDA before saving (rule R11); empty skips the check.",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _person(self, user: dict) -> tuple[str, str, Path]:
        if not user or not user.get("id"):
            raise LogError("No Open WebUI user context available.")
        try:
            people = json.loads(self.valves.PERSON_KEYS or "{}")
        except ValueError:
            raise LogError("Tool misconfigured: the PERSON_KEYS valve is not valid JSON.")
        p = people.get(user["id"])
        if not p or not p.get("key"):
            raise LogError("Food logging is not set up for this user. An admin adds them to the PERSON_KEYS valve.")
        base = Path(self.valves.INBOX_DIR) / p["key"]
        if not (base / "pending").is_dir():
            raise LogError(f"The food inbox for {p['key']} does not exist yet; it is created by the health ingest (migrate).")
        return p["key"], p.get("timezone") or "UTC", base

    def _entries(self, base: Path, since: datetime) -> list[dict]:
        out = []
        for status in ("pending", "processed"):
            for path in (base / status).glob("*.json"):
                if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < since - timedelta(days=1):
                    continue
                e = _read(path)
                if e and datetime.fromisoformat(e["eaten_at"]) >= since:
                    out.append({"status": "pending" if status == "pending" else "loaded", **e})
        return sorted(out, key=lambda e: e["eaten_at"])

    async def food_log_rules(self, __user__: dict = {}) -> str:
        """
        The food-logging constitution: the rules every entry must follow and the allowed meals and units, plus the user's current local time. Call this before logging food for the first time in a conversation.
        """
        try:
            _, tz, _ = self._person(__user__)
        except LogError as e:
            return json.dumps({"error": str(e)})
        props = CONSTITUTION["schema"]["properties"]
        return json.dumps({
            "version": CONSTITUTION["version"], "purpose": CONSTITUTION["purpose"], "rules": CONSTITUTION["rules"],
            "meals": props["meal"]["enum"], "units": props["items"]["items"]["properties"]["unit"]["enum"],
            "item_fields": "name, quantity, unit, optional grams, optional brand, optional barcode, portion_estimated",
            "local_time_now": datetime.now(ZoneInfo(tz)).isoformat(timespec="minutes"),
        }, ensure_ascii=False)

    async def food_log_add(
        self,
        meal: str,
        eaten_at: str,
        items: list[dict],
        source: str = "text",
        confidence: str = "high",
        note: str = "",
        supersedes: str = "",
        void: bool = False,
        accept_unmatched: bool = False,
        __user__: dict = {},
    ) -> str:
        """
        Save one eating occasion after the user confirmed it. Follow the constitution (food_log_rules). If the entry breaks a rule, nothing is saved and the violations are returned: fix them (ask the user when a portion is unknown) and call again. Each item is then checked against USDA (rule R11): items with no USDA match or no convertible portion block the save until fixed with the user.
        :param meal: breakfast, lunch, dinner or snack.
        :param eaten_at: Local date and time eaten, YYYY-MM-DDTHH:MM, or 'now'.
        :param items: List of foods, each {"name": "egg, scrambled", "quantity": 2, "unit": "piece", "grams": optional number, "brand": optional, "barcode": optional, "portion_estimated": false}. Units: g, ml, oz, cup, tbsp, tsp, piece, slice, serving. Empty only when void is true.
        :param source: text (user described it) or photo (identified from an image).
        :param confidence: high, medium or low (see rule R9).
        :param note: Optional short note, e.g. restaurant name.
        :param supersedes: entry_id of an earlier entry this one corrects or deletes (rule R8).
        :param void: true to delete the superseded entry (with no items).
        :param accept_unmatched: true only when the user asked to save items USDA can't match as they are (rule R11).
        """
        def work():
            key, tz, base = self._person(__user__)
            now = datetime.now(timezone.utc)
            entry = build_entry(key, tz, __user__["id"], meal, eaten_at, items, source, confidence, note,
                                supersedes, void, now)
            violations = check_entry(entry, now)
            if not violations and entry.get("supersedes"):
                sid = entry["supersedes"]
                if not any((base / s / f"{sid}.json").exists() for s in ("pending", "processed")):
                    violations.append({"rule": "R8", "path": "supersedes",
                                       "message": f"no logged entry {sid}; use food_log_recent to find entry ids"})
            if violations:
                return {"saved": False, "violations": violations,
                        "hint": "Nothing was saved. Fix these against the rules (food_log_rules) and try again; "
                                "ask the user if a portion or detail is unknown."}
            checks = None
            if self.valves.USDA_FDC_API_KEY and entry["items"]:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    checks = list(pool.map(lambda i: check_item(self.valves.USDA_FDC_API_KEY, i), entry["items"]))
                unmatched = [c for c in checks if c["status"] in ("no_match", "no_portion")]
                if unmatched and not accept_unmatched:
                    return {"saved": False, "unmatched": unmatched,
                            "matched": [c for c in checks if c not in unmatched],
                            "hint": "Nothing was saved (rule R11). Tell the user which foods USDA can't match or "
                                    "weigh, and fix them together: a more specific name, the dish split into its "
                                    "foods, the brand or barcode, grams, or a unit from usda_portions. Call again "
                                    "with accept_unmatched true only if the user wants them saved as they are."}
            pending = base / "pending"
            tmp = pending / f".tmp-{entry['entry_id']}"
            tmp.write_text(json.dumps(entry, ensure_ascii=False, indent=2))
            os.replace(tmp, pending / f"{entry['entry_id']}.json")
            result = {"saved": True, "entry_id": entry["entry_id"],
                      "entry": {k: entry[k] for k in ("eaten_at", "meal", "items", "void")},
                      "next": "Nutrients are looked up in USDA FoodData Central at the next hourly health ingest."}
            if checks is not None:
                result["usda"] = checks
                result["tell_user"] = "Which USDA food each item matched and its grams (rule R11)."
            else:
                result["usda"] = "not checked (no USDA_FDC_API_KEY valve)" if entry["items"] else None
            return result

        try:
            return json.dumps(await asyncio.to_thread(work), ensure_ascii=False)
        except LogError as e:
            return json.dumps({"saved": False, "error": str(e)})

    async def food_log_recent(self, days: int = 2, __user__: dict = {}) -> str:
        """
        The user's food entries eaten in the last few days, from the inbox: pending (not loaded yet) and loaded ones, with entry ids for corrections and withdrawals.
        :param days: How many days back (1-7).
        """
        try:
            _, tz, base = self._person(__user__)
        except LogError as e:
            return json.dumps({"error": str(e)})
        n = max(1, min(int(days), 7))
        since = datetime.now(ZoneInfo(tz)).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=n - 1)
        entries = await asyncio.to_thread(self._entries, base, since)
        return json.dumps({"since": since.isoformat(timespec="minutes"), "entries": [
            {k: e.get(k) for k in ("status", "entry_id", "eaten_at", "meal", "items", "note", "supersedes", "void")}
            for e in entries]}, ensure_ascii=False)

    async def food_log_withdraw(self, entry_id: str, __user__: dict = {}) -> str:
        """
        Withdraw an entry that is still pending (not yet loaded), e.g. when it was logged by mistake. A loaded entry cannot be withdrawn: log a correction with supersedes (and void true to delete it).
        :param entry_id: The entry id from food_log_add or food_log_recent.
        """
        try:
            _, _, base = self._person(__user__)
        except LogError as e:
            return json.dumps({"error": str(e)})
        eid = (entry_id or "").strip().lower()
        if not ENTRY_ID.match(eid):
            return json.dumps({"withdrawn": False, "error": "entry_id must be an entry id (a UUID)."})
        src = base / "pending" / f"{eid}.json"
        if not src.exists():
            loaded = (base / "processed" / f"{eid}.json").exists()
            return json.dumps({"withdrawn": False, "error": (
                "Already loaded: log a correction with supersedes set to this id (void true to delete it)."
                if loaded else "No pending entry with that id.")})
        os.replace(src, base / "withdrawn" / src.name)
        return json.dumps({"withdrawn": True, "entry_id": eid})
