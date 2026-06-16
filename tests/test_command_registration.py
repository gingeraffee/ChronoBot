"""Guards against the 'command defined after bot.run()' bug.

bot.run() blocks the event loop, so ANY @bot.tree.command defined below it in the
source never registers in production — even though importing the module (which
skips the __main__ guard) would still show it in the tree. That mismatch is what
hid /owner_unlock and /announce_update on the live bot. This test reads the source
and asserts the run block is dead last.

Run from repo root:  python tests/test_command_registration.py
"""

import os
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
SRC = (REPO / "chromie.py").read_text(encoding="utf-8")


def test_bot_run_is_after_every_command_registration():
    # Match the actual call (with TOKEN), not the words "bot.run()" in comments.
    run_pos = SRC.find("bot.run(TOKEN)")
    assert run_pos != -1, "bot.run(TOKEN) not found in chromie.py"

    # Every place a command/group gets registered must appear BEFORE bot.run().
    patterns = [r"@bot\.tree\.command\(", r"bot\.tree\.add_command\("]
    for pat in patterns:
        positions = [m.start() for m in re.finditer(pat, SRC)]
        if not positions:
            continue
        last = max(positions)
        line_of_last = SRC.count("\n", 0, last) + 1
        assert last < run_pos, (
            f"A command registration matching /{pat}/ appears at line {line_of_last}, "
            f"AFTER bot.run() — it will never register in production. "
            f"Move the run block to the end of the file."
        )


def test_expected_commands_are_in_the_tree():
    os.environ.setdefault("CHROMIE_DATA_PATH", os.path.join(tempfile.gettempdir(), "chromie_cmdreg.json"))
    import chromie
    names = {c.name for c in chromie.bot.tree.get_commands()}
    for required in ("announce_update", "owner_unlock", "event", "countdown", "addevent", "seteventchannel"):
        assert required in names, f"command '{required}' is missing from the tree"


def test_owner_only_command_set_references_real_commands():
    # OWNER_ONLY_COMMANDS are relocated to the dev guild at runtime; guard against
    # typos / renamed commands that would silently leave them registered globally.
    os.environ.setdefault("CHROMIE_DATA_PATH", os.path.join(tempfile.gettempdir(), "chromie_cmdreg.json"))
    import chromie
    names = {c.name for c in chromie.bot.tree.get_commands()}
    assert chromie.OWNER_ONLY_COMMANDS, "OWNER_ONLY_COMMANDS should not be empty"
    for name in chromie.OWNER_ONLY_COMMANDS:
        assert name in names, f"OWNER_ONLY_COMMANDS lists '{name}', which is not a registered command"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
            except Exception as e:
                failures += 1
                print(f"ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    sys.exit(1 if failures else 0)
