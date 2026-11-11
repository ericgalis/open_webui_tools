#!/usr/bin/env python3
"""Writes ../issues.json (the Open WebUI tool import file) from tool.py and ../prompts/feature_template.md.
Run it after editing either; tests/test_tool.py fails if issues.json is out of date."""
import json
from pathlib import Path

here = Path(__file__).resolve().parent
root = here.parent


def build() -> list:
    src = (here / "tool.py").read_text()
    template = (root / "prompts" / "feature_template.md").read_text()
    marker = 'FEATURE_TEMPLATE = ""  # filled in from prompts/feature_template.md by owui_issues/build.py'
    assert marker in src
    src = src.replace(marker, "FEATURE_TEMPLATE = " + json.dumps(template))
    head = src.split('"""')[1]
    meta = dict(line.split(": ", 1) for line in head.strip().splitlines())
    return [{"id": "issues", "name": meta["title"],
             "meta": {"description": meta["description"],
                      "manifest": {"title": meta["title"], "description": meta["description"],
                                   "version": meta["version"], "license": meta["license"]}},
             "content": src}]


if __name__ == "__main__":
    (root / "issues.json").write_text(json.dumps(build(), indent=4, ensure_ascii=False) + "\n")
    print("wrote issues.json")
