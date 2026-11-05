"""Check pinned npm bundles; --sync mechanically copies reviewed upstream files."""

import argparse
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "custom_components/tuya_recordings/frontend/vendor"
ASSETS = {
    "plyr/dist/plyr.min.js": "plyr/plyr.min.js",
    "plyr/dist/plyr.css": "plyr/plyr.css",
    "plyr/dist/plyr.svg": "plyr/plyr.svg",
    "plyr/LICENSE.md": "plyr/LICENSE.md",
    "vis-timeline/standalone/esm/vis-timeline-graph2d.min.mjs": "vis-timeline/vis-timeline.min.mjs",
    "vis-timeline/styles/vis-timeline-graph2d.min.css": "vis-timeline/vis-timeline.min.css",
    "vis-timeline/LICENSE.Apache-2.0.txt": "vis-timeline/LICENSE.Apache-2.0.txt",
    "vis-timeline/LICENSE.MIT.txt": "vis-timeline/LICENSE.MIT.txt",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sync", action="store_true", help="Copy reviewed npm assets after npm ci --ignore-scripts")
    args = parser.parse_args()
    stale = []
    for upstream, local in ASSETS.items():
        source, target = ROOT / "node_modules" / upstream, TARGET / local
        if not source.is_file():
            raise SystemExit("Run npm ci --ignore-scripts before checking frontend dependencies")
        if args.sync:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        if not target.is_file() or target.read_bytes() != source.read_bytes():
            stale.append(local)
    if stale:
        raise SystemExit("Bundled assets differ from pinned npm packages: " + ", ".join(stale))
    print(f"Verified {len(ASSETS)} frontend assets against the pinned npm packages")


if __name__ == "__main__":
    main()
