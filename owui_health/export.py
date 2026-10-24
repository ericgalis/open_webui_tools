"""Write the Open WebUI import files (../health_data.json, ../food_log.json) from the tool sources here.
Run after every change to a tool: python3 owui_health/export.py"""

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = {"health_data": "health_data.py", "food_log": "food_log.py"}


def frontmatter(src: str) -> dict:
    block = re.match(r'^"""\n(.*?)\n"""', src, re.S).group(1)
    return dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)


def export(tool_id: str, filename: str) -> Path:
    src = (HERE / filename).read_text()
    meta = frontmatter(src)
    doc = [{
        "id": tool_id,
        "name": meta["title"],
        "meta": {"description": meta["description"], "manifest": meta},
        "content": src,
    }]
    out = HERE.parent / f"{tool_id}.json"
    out.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    return out


if __name__ == "__main__":
    for tool_id, filename in TOOLS.items():
        print(export(tool_id, filename))
