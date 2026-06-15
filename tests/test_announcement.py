"""Tests for the owner-only launch announcement broadcaster.

Exercises _broadcast_launch_announcement against a fake state + fake channels:
dry-run counting, real sends, and the per-version idempotency guard.

Run from repo root:  python tests/test_announcement.py
"""

import os
import sys
import asyncio
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
os.environ["CHROMIE_DATA_PATH"] = os.path.join(tempfile.gettempdir(), "chromie_announce_test.json")

import chromie  # noqa: E402


class FakeChannel:
    sent = []  # class-level log of (channel_id, embed_title)

    def __init__(self, cid):
        self.id = cid

    async def send(self, *, embed=None, allowed_mentions=None):
        FakeChannel.sent.append((self.id, getattr(embed, "title", None)))


async def _fake_get_text_channel(cid):
    return FakeChannel(int(cid))


def _seed_state():
    chromie.state = {
        "guilds": {
            "1": {
                "channels": {
                    "111": {"pinned_message_id": 999, "events": []},  # active -> target
                    "222": {"events": []},                            # no pin -> skip
                    "333": {"pinned_message_id": 888},                # pre-announced
                },
                "_announced": {chromie.ANNOUNCEMENT_VERSION: ["333"]},
            },
            "2": {"channels": {"444": {"pinned_message_id": 777}}},   # active -> target
        },
        "user_links": {},
    }


def setup_each():
    FakeChannel.sent = []
    _seed_state()
    chromie.get_text_channel = _fake_get_text_channel


def test_embed_builds():
    e = chromie._build_launch_announcement_embed()
    assert "leveled up" in e.title.lower()
    assert "/event" in (e.description or "")
    assert "/countdown" in (e.description or "")


def test_dry_run_counts_only():
    setup_each()
    embed = chromie._build_launch_announcement_embed()
    res = asyncio.run(chromie._broadcast_launch_announcement(embed, confirm=False, throttle=0))
    assert res["would_send"] == 2, res          # 111 + 444
    assert res["skipped"] == 1, res             # 333 already announced
    assert res["sent"] == 0, res                # dry run sends nothing
    assert FakeChannel.sent == [], "dry run must not send"


def test_confirm_sends_then_is_idempotent():
    setup_each()
    embed = chromie._build_launch_announcement_embed()
    res = asyncio.run(chromie._broadcast_launch_announcement(embed, confirm=True, throttle=0))
    assert res["sent"] == 2, res
    assert res["skipped"] == 1, res             # 333 still skipped
    sent_ids = sorted(cid for cid, _ in FakeChannel.sent)
    assert sent_ids == [111, 444], sent_ids     # only active channels, not 222/333

    # Re-run: everything now recorded -> nothing sent again.
    FakeChannel.sent = []
    res2 = asyncio.run(chromie._broadcast_launch_announcement(embed, confirm=True, throttle=0))
    assert res2["sent"] == 0, res2
    assert res2["skipped"] == 3, res2           # 111, 333, 444 all done now
    assert FakeChannel.sent == [], "idempotent re-run must not resend"


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
