"""Read-only host/dictionary check; never selects, clones, edits, or exports a photo."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from c1ai.macos_backend import MacOSBackend, inspect_installation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata-only", action="store_true", help="Do not send Apple Events")
    args = parser.parse_args()
    try:
        if sys.platform != "darwin":
            raise OSError("This check requires macOS")
        result = inspect_installation() if args.metadata_only else MacOSBackend().context()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("native_connection") == "unavailable" else 0
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "refused", "error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
