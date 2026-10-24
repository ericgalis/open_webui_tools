import sys
from pathlib import Path

# The tools are single files Open WebUI loads as modules; import them the same way.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
