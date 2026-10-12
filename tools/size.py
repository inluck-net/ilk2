"""Core size per module (docs/02 §2.12): non-blank, non-comment lines. A diagnostic, not a budget."""
import re
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1] / "src" / "ilk2"
rows = []
for f in sorted(root.rglob("*")):
    if f.suffix in (".py", ".mjs") and f.is_file():
        n = sum(1 for l in f.read_text().splitlines() if l.strip() and not re.match(r"\s*(#|//)", l))
        rows.append((n, str(f.relative_to(root))))
for n, name in sorted(rows, reverse=True):
    print(f"{n:6d}  {name}")
print(f"{sum(n for n, _ in rows):6d}  total ({len(rows)} files)")
sys.exit(0)
