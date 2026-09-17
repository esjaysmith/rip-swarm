import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rip_swarm.cli import main

_COMMANDS = {"heartbeat", "complete", "release", "reject"}
_argv = sys.argv[1:]
if _argv and _argv[0] in _COMMANDS:
    raise SystemExit(main(_argv))
raise SystemExit(main(["claim", *_argv]))
