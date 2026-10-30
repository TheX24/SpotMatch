from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop"))

from release_support import validate_release_tag  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a SpotMatch stable release tag.")
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    version = (ROOT / "desktop" / "VERSION").read_text(encoding="utf-8").strip()
    try:
        validate_release_tag(args.tag, version)
    except ValueError as error:
        parser.exit(1, f"error: {error}\n")
    print(f"{args.tag} matches VERSION {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
