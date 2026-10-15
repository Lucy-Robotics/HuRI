"""The `huri` command.

huri hub search|list|install ...    find and install modules (huri_core.hub)
"""

import sys
from typing import List, Optional


def main(argv: Optional[List[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["hub"]:
        from huri_core.hub import main as hub

        return hub(args[1:])
    print(__doc__.strip(), file=sys.stderr)
    return 0 if args[:1] in (["-h"], ["--help"]) else 2


if __name__ == "__main__":
    sys.exit(main())
