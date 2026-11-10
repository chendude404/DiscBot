"""
Plain-code helpers the Discord side (discbot/) builds on: storage and parsing for the to-do
board, the class schedule, the FAQ, slide-deck rendering, the Claude and Gradescope clients,
and (in lib/jobs/) job-link parsing, the job archive and application stages.

ROOT is the repo folder; DATA (ROOT/data) holds everything personal or generated, all
gitignored: data.json, archive.json, bot.log and my_schedule.py.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
