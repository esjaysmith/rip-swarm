import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rip_swarm.cli import main

raise SystemExit(main(["leave", *sys.argv[1:]]))
