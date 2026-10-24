"""
title: Food Log
description: Log what the user ate, following the food-logging constitution: structured items and portions only, validated before anything is saved. Entries go to the user's food inbox; the nightly health ingest re-validates them and looks up nutrients in USDA FoodData Central. The model never estimates calories. Entries are never edited: corrections are new entries that supersede old ones.
version: 0.1.0
license: MIT
"""

import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field

# Identical to food_constitution.json in the compose repo's health/ stack (a test checks this).
# Change both together, and bump the version.
CONSTITUTION = json.loads(r"""
{
  "name": "food-logging-constitution",
  "version": "1",
  "purpose": "Every food log entry is recorded the same way, so nightly ingest can resolve nutrients deterministically from USDA FoodData Central. The model records what was eaten; it never estimates calories or macros.",
  "rules": [
    {
      "id": "R1",
      "text": "One entry per eating occasion. meal is one of breakfast, lunch, dinner, snack."
    },
    {
      "id": "R2",
      "text": "eaten_at is the local date and time the food was eaten (YYYY-MM-DDTHH:MM). It is not in the future and not more than 7 days ago."
    },
    {
      "id": "R3",
      "text": "Each item name is a generic food in lowercase, most specific first, with the preparation when known: 'egg, scrambled', 'bread, whole wheat, toasted', 'milk, 2%'. No brand in the name, no quantities in the name, one food per item."
    },
    {
      "id": "R4",
      "text": "quantity is a positive number and unit is one of g, ml, oz, cup, tbsp, tsp, piece, slice, serving. Prefer g or ml when the user gives a weight or volume."
    },
    {
      "id": "R5",
      "text": "grams is set only when the user stated a weight or the package says it. Never invent grams."
    },
    {
      "id": "R6",
      "text": "brand and barcode are set only when the user said them or they are visible in a photo. Never invent them."
    },
    {
      "id": "R7",
      "text": "If the portion is unknown, ask the user. If they cannot say, use quantity 1 and unit serving, and set portion_estimated to true."
    },
    {
      "id": "R8",
      "text": "Logged entries are never edited. To correct one, log a new entry with supersedes set to the old entry_id and the full corrected item list. To delete one, log an entry with supersedes set, void true and no items."
    },
    {
      "id": "R9",
      "text": "source is text when the user described the food, photo when it was identified from an image. confidence is high when the user stated foods and portions, medium when some portions were estimated, low when identified mostly from a photo."
    },
    {
      "id": "R10",
      "text": "Restate the entry to the user and get confirmation before calling food_log_add. Do not log drinks of plain water."
    }
  ],
  "schema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": false,
    "required": [
      "entry_id",
      "constitution_version",
      "owui_user_id",
      "person",
      "logged_at",
      "eaten_at",
      "meal",
      "items",
      "source",
      "confidence",
      "void"
    ],
    "properties": {
      "entry_id": {
        "type": "string",
        "format": "uuid"
      },
      "constitution_version": {
        "const": "1"
      },
      "owui_user_id": {
        "type": "string",
        "minLength": 1
      },
      "person": {
        "type": "string",
        "pattern": "^[a-z][a-z0-9_]{0,30}$"
      },
      "logged_at": {
        "type": "string",
        "format": "date-time"
      },
      "eaten_at": {
        "type": "string",
        "format": "date-time"
      },
      "meal": {
        "enum": [
          "breakfast",
          "lunch",
          "dinner",
          "snack"
        ]
      },
      "source": {
        "enum": [
          "text",
          "photo"
        ]
      },
      "confidence": {
        "enum": [
          "high",
          "medium",
          "low"
        ]
      },
      "note": {
        "type": "string",
        "maxLength": 500
      },
      "supersedes": {
        "type": "string",
        "format": "uuid"
      },
      "void": {
        "type": "boolean"
      },
      "items": {
        "type": "array",
        "maxItems": 40,
        "items": {
          "type": "object",
          "additionalProperties": false,
          "required": [
            "name",
            "quantity",
            "unit",
            "portion_estimated"
          ],
          "properties": {
            "name": {
              "type": "string",
              "pattern": "^[a-z0-9][a-z0-9 ,()'%&/.-]{1,79}$"
            },
            "quantity": {
              "type": "number",
              "exclusiveMinimum": 0,
              "maximum": 5000
            },
            "unit": {
              "enum": [
                "g",
                "ml",
                "oz",
                "cup",
                "tbsp",
                "tsp",
                "piece",
                "slice",
                "serving"
              ]
            },
            "grams": {
              "type": "number",
              "minimum": 1,
              "maximum": 3000
            },
            "brand": {
              "type": "string",
              "minLength": 1,
              "maxLength": 80
            },
            "barcode": {
              "type": "string",
              "pattern": "^[0-9]{8,14}$"
            },
            "portion_estimated": {
              "type": "boolean"
            }
          }
        }
      }
    },
    "if": {
      "properties": {
        "void": {
          "const": true
        }
      }
    },
    "then": {
      "required": [
        "supersedes"
      ],
      "properties": {
        "items": {
          "maxItems": 0
        }
      }
    },
    "else": {
      "properties": {
        "items": {
          "minItems": 1
        }
      }
    }
  }
}
""")

VALIDATOR = Draft202012Validator(CONSTITUTION["schema"], format_checker=Draft202012Validator.FORMAT_CHECKER)
MAX_AGE = timedelta(days=7)
CLOCK_SKEW = timedelta(minutes=10)

# First path element of a schema error -> the rule it breaks
RULE_FOR_FIELD = {
    "meal": "R1", "eaten_at": "R2", "name": "R3", "quantity": "R4", "unit": "R4", "grams": "R5",
    "brand": "R6", "barcode": "R6", "portion_estimated": "R7", "supersedes": "R8", "void": "R8",
    "items": "R8", "source": "R9", "confidence": "R9",
}
ENTRY_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class LogError(Exception):
    pass


def _violation(error) -> dict:
    path = [str(p) for p in error.absolute_path]
    field = next((p for p in reversed(path) if not p.isdigit()), "items" if path[:1] == ["items"] else "")
    if not path and error.validator in ("required", "maxItems", "minItems"):
        field = "supersedes" if "supersedes" in error.message else "items"
    return {"rule": RULE_FOR_FIELD.get(field, "schema"), "path": "/".join(path) or "(entry)", "message": error.message}


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


class Tools:
    class Valves(BaseModel):
        PERSON_KEYS: str = Field(
            default="{}",
            description='JSON object mapping each Open WebUI user id to their health person key and time zone, '
                        'e.g. {"<user id>": {"key": "enrique", "timezone": "America/New_York"}}.',
        )
        INBOX_DIR: str = Field(default="/health_inbox", description="The food inbox mounted into Open WebUI.")

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
        __user__: dict = {},
    ) -> str:
        """
        Save one eating occasion after the user confirmed it. Follow the constitution (food_log_rules). If the entry breaks a rule, nothing is saved and the violations are returned: fix them (ask the user when a portion is unknown) and call again.
        :param meal: breakfast, lunch, dinner or snack.
        :param eaten_at: Local date and time eaten, YYYY-MM-DDTHH:MM, or 'now'.
        :param items: List of foods, each {"name": "egg, scrambled", "quantity": 2, "unit": "piece", "grams": optional number, "brand": optional, "barcode": optional, "portion_estimated": false}. Units: g, ml, oz, cup, tbsp, tsp, piece, slice, serving. Empty only when void is true.
        :param source: text (user described it) or photo (identified from an image).
        :param confidence: high, medium or low (see rule R9).
        :param note: Optional short note, e.g. restaurant name.
        :param supersedes: entry_id of an earlier entry this one corrects or deletes (rule R8).
        :param void: true to delete the superseded entry (with no items).
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
            pending = base / "pending"
            tmp = pending / f".tmp-{entry['entry_id']}"
            tmp.write_text(json.dumps(entry, ensure_ascii=False, indent=2))
            os.replace(tmp, pending / f"{entry['entry_id']}.json")
            return {"saved": True, "entry_id": entry["entry_id"], "entry": {k: entry[k] for k in ("eaten_at", "meal", "items", "void")},
                    "next": "Nutrients are looked up in USDA FoodData Central at the next hourly health ingest."}

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
