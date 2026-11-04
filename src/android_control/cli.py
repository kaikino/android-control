"""`droid` — minimal CLI over the same device layer, for quick shell use and scripting.

  droid shot [out.png] [--grid N]   screenshot (prints path)
  droid tree [--all] [--package P]  UI hierarchy
  droid find TEXT                   elements whose text/desc contains TEXT
  droid tap X Y | droid tap TEXT    tap coordinates or element
  droid swipe X1 Y1 X2 Y2 [MS]
  droid type TEXT
  droid key NAME [NAME...]
  droid pixel X Y [X Y ...]
  droid unlock [PIN]
  droid info
"""

from __future__ import annotations

import json
import sys

from . import imaging
from .adb import AdbError, Device, Selector


def main(argv: list[str] | None = None) -> int:
    a = list(sys.argv[1:] if argv is None else argv)
    if not a or a[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, rest = a[0], a[1:]
    try:
        d = Device()
        d.ensure_ready()
        if cmd == "shot":
            grid = 0
            if "--grid" in rest:
                i = rest.index("--grid"); grid = int(rest[i + 1]); del rest[i:i + 2]
            out = rest[0] if rest else "/tmp/android-control/screen.png"
            img = d.screenshot_image()
            if grid:
                img = imaging.draw_grid(img, grid)
            from pathlib import Path
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            img.save(out)
            print(out)
        elif cmd == "tree":
            nodes = d.ui_nodes()
            if "--package" in rest:
                p = rest[rest.index("--package") + 1]
                nodes = [n for n in nodes if n.package == p]
            if "--all" not in rest:
                nodes = [n for n in nodes if n.is_interesting()]
            for n in nodes:
                print("  " * n.depth + n.describe())
        elif cmd == "find":
            for n in d.find(Selector(text_contains=" ".join(rest))):
                print(n.describe())
        elif cmd == "tap":
            if len(rest) == 2 and all(r.lstrip("-").isdigit() for r in rest):
                d.tap(int(rest[0]), int(rest[1]))
            else:
                found = d.find(Selector(text_contains=" ".join(rest)))
                if not found:
                    raise AdbError(f"no element contains {' '.join(rest)!r}")
                d.tap(*found[0].center)
                print("tapped", found[0].describe())
        elif cmd == "swipe":
            v = [int(x) for x in rest]
            d.swipe(*v[:4], v[4] if len(v) > 4 else 300)
        elif cmd == "type":
            d.type_text(" ".join(rest))
        elif cmd == "key":
            d.key(*rest)
        elif cmd == "pixel":
            img = d.screenshot_image()
            for i in range(0, len(rest) - 1, 2):
                x, y = int(rest[i]), int(rest[i + 1])
                print(x, y, imaging.hex_of(img.getpixel((x, y))))
        elif cmd == "unlock":
            print(d.wake_and_unlock(rest[0] if rest else None))
        elif cmd == "info":
            print(json.dumps(d.info(), indent=1))
        else:
            print(f"unknown command {cmd!r}\n{__doc__}", file=sys.stderr)
            return 2
    except AdbError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
