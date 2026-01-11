import os
import json
import traceback
from pathlib import Path
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List, Tuple, Dict, Any
import time
import discord
from discord.errors import NotFound, HTTPException
from discord.ext import commands, tasks
from discord import app_commands
from threading import Lock
import random
import re
import aiohttp
import difflib
import hashlib
from discord.errors import NotFound as DiscordNotFound, Forbidden as DiscordForbidden, HTTPException
# ==========================
# CONFIG
# ==========================

VERSION = "2025-12-29"

DEFAULT_TZ = ZoneInfo("America/Chicago")
UPDATE_INTERVAL_SECONDS = 60
DEFAULT_MILESTONES = [100, 60, 30, 14, 7, 2, 1, 0]
MILESTONE_CLEANUP_AFTER_EVENT_SECONDS = 86400  # 24 hours

DATA_FILE = Path(os.getenv("CHROMIE_DATA_PATH", "/var/data/chromie_state.json"))
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()

FAQ_URL = "https://gingeraffee.github.io/chronobot-faq/"
SUPPORT_SERVER_URL = os.getenv("CHROMIE_SUPPORT_SERVER_URL", "").strip()  # set in Render/hosting env

EMBED_COLOR = discord.Color.from_rgb(140, 82, 255)  # ChronoBot purple

DEFAULT_THEME_ID = "classic"  # Chrono purple classic theme

LOG_THROTTLE_SECONDS = 60 * 30  # 30 minutes
_last_log = {}  # (guild_id, code) -> last_time

_STATE_LOCK = Lock()

TOPGG_TOKEN = os.getenv("TOPGG_TOKEN", "").strip()
TOPGG_BOT_ID = os.getenv("TOPGG_BOT_ID", "").strip()
TOPGG_FAIL_OPEN = False 
    
def log_throttled(guild_id: int, code: str, msg: str):
    key = (guild_id, code)
    now = time.time()
    last = _last_log.get(key, 0)
    if now - last >= LOG_THROTTLE_SECONDS:
        _last_log[key] = now
        print(msg)


# ==========================
# TOP.GG VOTE GATING
# ==========================

_vote_cache: Dict[int, Tuple[float, bool]] = {}  # user_id -> (cached_at_monotonic, voted)
VOTE_CACHE_TTL_SECONDS = 60  # keep short so votes register quickly
_vote_ask_cooldown: Dict[int, float] = {}  # user_id -> last ask epoch
VOTE_ASK_COOLDOWN_SECONDS = 60 * 60 * 24  # 24h

async def maybe_vote_nudge(interaction: discord.Interaction, reason: str) -> None:
    # Only nudge if they haven't voted
    if await topgg_has_voted(interaction.user.id):
        return

    now = time.time()
    last = _vote_ask_cooldown.get(interaction.user.id, 0)
    if now - last < VOTE_ASK_COOLDOWN_SECONDS:
        return

    _vote_ask_cooldown[interaction.user.id] = now

    msg = (
        f"💜 {reason}\n"
        "Voting unlocks supporter perks:\n"
        "• /theme • /milestones advanced • /template save/load • /banner set • /digest enable"
    )

    # Use followup if already responded
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True, view=build_vote_view())
    else:
        await interaction.response.send_message(msg, ephemeral=True, view=build_vote_view())

def build_vote_view() -> discord.ui.View:
    view = discord.ui.View()
    url = f"https://top.gg/bot/{TOPGG_BOT_ID}/vote" if TOPGG_BOT_ID else "https://top.gg"
    view.add_item(discord.ui.Button(label="Vote on Top.gg", style=discord.ButtonStyle.link, url=url))
    return view
        
TOPGG_API_V1_BASE = "https://top.gg/api/v1"

TOPGG_API_BASE = "https://top.gg/api"

async def topgg_has_voted(user_id: int, *, force: bool = False) -> bool:
    now = time.monotonic()
    cached = _vote_cache.get(user_id)

    if (not force) and cached and (now - cached[0] <= VOTE_CACHE_TTL_SECONDS):
        return cached[1]

    # Need BOTH token + bot id for the documented check endpoint
    if not TOPGG_TOKEN or not TOPGG_BOT_ID:
        voted = True if TOPGG_FAIL_OPEN else False
        _vote_cache[user_id] = (now, voted)
        return voted

    url = f"{TOPGG_API_BASE}/bots/{TOPGG_BOT_ID}/check"
    headers = {"Authorization": TOPGG_TOKEN.strip()}  # v0 docs: no Bearer :contentReference[oaicite:2]{index=2}
    params = {"userId": str(user_id)}
    timeout = aiohttp.ClientTimeout(total=6)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    voted = bool(int(data.get("voted", 0) or 0))
                else:
                    voted = True if TOPGG_FAIL_OPEN else False
    except Exception:
        voted = True if TOPGG_FAIL_OPEN else False

    _vote_cache[user_id] = (now, voted)
    return voted

async def send_vote_required(interaction: discord.Interaction, feature_label: str):
    content = (
        f"🗳️ **Vote required** to use **{feature_label}**.\n"
        "Vote on Top.gg, then try again!"
    )
    view = build_vote_view()  # whatever you already use

    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True, view=view)
        else:
            await interaction.response.send_message(content, ephemeral=True, view=view)
    except NotFound:
        # Interaction expired (10062). Nothing we can do at this point.
        return
    except HTTPException:
        # Any transient Discord API issue; don't explode your command task.
        return

def require_vote(feature_label: str):
    async def predicate(interaction: discord.Interaction) -> bool:
        user_id = interaction.user.id

        # Only check if they have voted in the last 12 hours
        voted = await topgg_has_voted(user_id)
        if not voted:
            # try forcing a refresh
            voted = await topgg_has_voted(user_id, force=True)

        if voted:
            return True

        await send_vote_required(interaction, feature_label)
        raise VoteRequired()

    return app_commands.check(predicate)

    
# ==========================
# STATE HANDLING
# ==========================

"""
State structure (high level):

{
  "guilds": {
    "guild_id_str": {
      "event_channel_id": int | None,
      "pinned_message_id": int | None,
      "mention_role_id": int | None,
      "events": [
        {
          "name": str,
          "timestamp": int,
          "milestones": [int, ...],
          "announced_milestones": [int, ...],
          "repeat_every_days": int | None,
          "repeat_anchor_date": "YYYY-MM-DD" | None,
          "announced_repeat_dates": ["YYYY-MM-DD", ...],
          "silenced": bool,
          "owner_user_id": int | None
        }
      ],
      "welcomed": bool
    }
  },
  "user_links": {
    "user_id_str": guild_id_int
  }
}
"""

EVENT_START_GRACE_SECONDS = 60 * 60  # 1 hour: announce if bot wakes up within 1 hour after start

PREMIUM_PERKS_TEXT = (
    "Voting unlocks:\n"
    "• /theme (premium themes)\n"
    "• /milestones advanced\n"
    "• /template save + /template load\n"
    "• /banner set\n"
    "• /digest enable"
)

class VoteRequired(app_commands.CheckFailure):
    """Raised when a vote-locked command is used without a valid vote."""
    pass
    
def build_event_start_blast(event_name: str) -> str:
    # Keep it short, loud, and celebratory.
    templates = [
        "🎉 IT’S HAPPENING!! 🎉 **{name}** starts **RIGHT NOW** — the countdown has officially paid off! 🥳✨",
        "🚨 TIME’S UP (in the best way) 🚨 **{name}** is **LIVE NOW**! Everybody scream internally! 🎊",
        "🔥 THE MOMENT HAS ARRIVED 🔥 **{name}** is starting **NOW**! Let’s GOOOOOO! 💜🎉",
        "✨ ZERO HAS BEEN REACHED ✨ **{name}** is happening **RIGHT NOW** — main character energy only. 🎇",
    ]
    return random.choice(templates).format(name=event_name or "The Event")

def load_state() -> dict:
    data = {}
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            # Preserve the broken file so data isn't permanently lost
            try:
                ts = datetime.now(DEFAULT_TZ).strftime("%Y%m%d-%H%M%S")
                corrupt_path = DATA_FILE.with_suffix(DATA_FILE.suffix + f".corrupt.{ts}")
                DATA_FILE.rename(corrupt_path)
                print(f"[STATE] State file was invalid JSON. Renamed to: {corrupt_path.name}")
            except Exception:
                print("[STATE] State file was invalid JSON and could not be renamed.")
            data = {}

    data.setdefault("guilds", {})
    data.setdefault("user_links", {})
    return data


def sort_events(guild_state: dict):
    events = guild_state.get("events")
    if not isinstance(events, list):
        events = []
    events.sort(key=lambda ev: ev.get("timestamp", 0))
    guild_state["events"] = events


def save_state():
    with _STATE_LOCK:
        try:
            DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

            tmp_path = DATA_FILE.with_suffix(DATA_FILE.suffix + ".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)

            os.replace(tmp_path, DATA_FILE)  # atomic on most platforms

            # Clean up if still present (paranoia)
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass

        except Exception as e:
            print(f"[STATE] save_state failed: {type(e).__name__}: {e}")

# ==========================
# STATE INIT (must exist globally)
# ==========================

state = load_state()
for _, g_state in state.get("guilds", {}).items():
    sort_events(g_state)
save_state()


def get_guild_state(guild_id: int) -> dict:
    gid = str(guild_id)
    guilds = state.setdefault("guilds", {})
    if gid not in guilds:
        guilds[gid] = {
            "event_channel_id": None,
            "pinned_message_id": None,
            "mention_role_id": None,
            "events": [],
            "welcomed": False,

            # NEW (audit)
            "event_channel_set_by": None,
            "event_channel_set_at": None,

            # NEW (supporter features)
            "theme": DEFAULT_THEME_ID,
            "countdown_title_override": None,
            "countdown_description_override": None,
            "default_milestones": DEFAULT_MILESTONES.copy(),
            "templates": {},  # { "name_key": {...template...} }
            "digest": {
                "enabled": False,
                "channel_id": None,
                "last_sent_date": None,  # "YYYY-MM-DD"
            },
        }

    else:
        guilds[gid].setdefault("event_channel_id", None)
        guilds[gid].setdefault("pinned_message_id", None)
        guilds[gid].setdefault("mention_role_id", None)
        guilds[gid].setdefault("events", [])
        guilds[gid].setdefault("welcomed", False)
        guilds[gid].setdefault("event_channel_set_by", None)
        guilds[gid].setdefault("event_channel_set_at", None)
        guilds[gid].setdefault("theme", DEFAULT_THEME_ID)
        guilds[gid].setdefault("countdown_title_override", None)
        guilds[gid].setdefault("countdown_description_override", None)
        guilds[gid].setdefault("default_milestones", DEFAULT_MILESTONES.copy())
        guilds[gid].setdefault("templates", {})
        guilds[gid].setdefault("digest", {"enabled": False, "channel_id": None, "last_sent_date": None})
    return guilds[gid]


def get_user_links() -> dict:
    return state.setdefault("user_links", {})


def _today_local_date() -> date:
    return datetime.now(DEFAULT_TZ).date()


def calendar_days_left(dt: datetime) -> int:
    now = datetime.now(DEFAULT_TZ)
    return (dt.date() - now.date()).days


def compute_time_left(now: datetime, target_dt: datetime) -> tuple[str, int, bool]:
    """
    Return (human_string, days_until_or_since, is_past).

    Human string intentionally uses only days/hours/minutes (no seconds)
    to keep pinned messages compact.
    """
    delta = target_dt - now
    total_seconds = int(delta.total_seconds())
    is_past = total_seconds < 0

    total_seconds_abs = abs(total_seconds)

    # Special-case: less than a minute
    if total_seconds_abs < 60:
        desc = "less than 1 minute"
        days = 0
        return (f"Happened {desc} ago", days, True) if is_past else (desc, days, False)

    days, rem = divmod(total_seconds_abs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours or days:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    # Always show minutes once we're above 1 minute
    parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")

    desc = " • ".join(parts)
    if is_past:
        return f"Happened {desc} ago", days, True
    return desc, days, False

def parse_milestones(text: str) -> Optional[List[int]]:
    """
    Parse milestone input like:
      "100, 50, 30, 14, 7, 2, 1, 0"
      "100 50 30"
      "100,50,30"
    Returns sorted unique list or None if invalid.
    """
    if not text or not text.strip():
        return None

    cleaned = text.replace(",", " ").replace(";", " ").strip()
    parts = [p for p in cleaned.split() if p.strip()]

    out: List[int] = []
    try:
        for p in parts:
            n = int(p)
            if n < 0 or n > 5000:
                return None
            out.append(n)
    except ValueError:
        return None

    out = sorted(set(out), reverse=True)
    return out


# How long to keep events after they start (so start blast doesn’t delete them immediately)
EVENT_START_GRACE_SECONDS = 60 * 60  # 1 hour
STARTED_EVENT_KEEP_SECONDS = EVENT_START_GRACE_SECONDS  # or set to 10*60 for 10 minutes

def prune_past_events(guild_state: dict, now: Optional[datetime] = None) -> int:
    """Delete events whose timestamp has passed, but keep 'just started' events for a short window."""
    if now is None:
        now = datetime.now(DEFAULT_TZ)

    # Keep window must be at least the start-blast grace window
    keep_seconds = max(STARTED_EVENT_KEEP_SECONDS, EVENT_START_GRACE_SECONDS)

    sort_events(guild_state)
    events = guild_state.get("events", [])
    if not isinstance(events, list) or not events:
        guild_state["events"] = [] if not isinstance(events, list) else events
        return 0

    kept: list[dict] = []
    removed = 0

    for ev in events:
        ts = ev.get("timestamp")
        if not isinstance(ts, (int, float)):
            kept.append(ev)
            continue

        try:
            dt = datetime.fromtimestamp(ts, tz=DEFAULT_TZ)
        except Exception:
            kept.append(ev)
            continue

        if dt <= now:
            age = (now - dt).total_seconds()
            if age <= keep_seconds:
                kept.append(ev)
            else:
                removed += 1
        else:
            kept.append(ev)

    if removed:
        guild_state["events"] = kept
        sort_events(guild_state)

    return removed
async def cleanup_milestones_if_due(guild_state: dict, ev: dict):
    # Backward compatible defaults
    ev.setdefault("milestone_messages", [])
    ev.setdefault("milestones_cleaned", False)

    if ev["milestones_cleaned"]:
        return

    now_ts = int(time.time())
    delete_after = int(ev.get("timestamp", 0)) + MILESTONE_CLEANUP_AFTER_EVENT_SECONDS

    # Not due yet
    if now_ts < delete_after:
        return

    msgs = ev.get("milestone_messages", [])
    if not msgs:
        ev["milestones_cleaned"] = True
        save_state()
        return

    for item in msgs:
        ch = bot.get_channel(int(item.get("channel_id", 0)))
        mid = int(item.get("message_id", 0))
        if not isinstance(ch, discord.TextChannel) or mid <= 0:
            continue

        try:
            await ch.get_partial_message(mid).delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            # If it can't be deleted, don't loop forever.
            pass

    ev["milestone_messages"] = []
    ev["milestones_cleaned"] = True
    save_state()

# ==========================
# DISCORD SETUP
# ==========================

intents = discord.Intents.default()


class ChromieBot(commands.Bot):
    async def setup_hook(self):
        try:
            await self.tree.sync()
            print(f"Slash commands synced (setup_hook). [{VERSION}]")
        except Exception as e:
            print(f"Error syncing commands (setup_hook): {e}")

        if not update_countdowns.is_running():
            update_countdowns.start()
        if not weekly_digest_loop.is_running():
            weekly_digest_loop.start()


bot = ChromieBot(command_prefix="!", intents=intents)


# ==========================
# PERMISSION NOTIFY (OWNER DM)
# ==========================

PERM_ALERT_COOLDOWN_SECONDS = 60 * 60 * 24  # 1 day (persisted in JSON state)

RECOMMENDED_CHANNEL_PERMS = (
    "view_channel",
    "send_messages",
    "embed_links",
    "read_message_history",
    "manage_messages",  # needed for pin/unpin + editing
)

PERM_LABELS = {
    "view_channel": "View Channel",
    "send_messages": "Send Messages",
    "embed_links": "Embed Links",
    "read_message_history": "Read Message History",
    "manage_messages": "Manage Messages (pin/unpin)",
}


def _bot_member_cached(guild: discord.Guild) -> Optional[discord.Member]:
    if guild.me is not None:
        return guild.me
    if bot.user is None:
        return None
    return guild.get_member(bot.user.id)


def missing_channel_perms(channel: discord.abc.GuildChannel, guild: discord.Guild) -> list[str]:
    me = _bot_member_cached(guild)
    if me is None:
        return list(RECOMMENDED_CHANNEL_PERMS)

    perms = channel.permissions_for(me)
    return [p for p in RECOMMENDED_CHANNEL_PERMS if not getattr(perms, p, False)]


def _perm_alert_key(guild_id: int, channel_id: int, code: str) -> str:
    return f"{guild_id}:{channel_id}:{code}"


def _get_perm_alerts_bucket(guild_state: dict) -> dict:
    # Stored in chromie_state.json so Render restarts don’t reset the cooldown
    bucket = guild_state.get("perm_alerts")
    if not isinstance(bucket, dict):
        bucket = {}
        guild_state["perm_alerts"] = bucket
    return bucket


def _should_send_perm_alert(guild_state: dict, key: str) -> bool:
    bucket = _get_perm_alerts_bucket(guild_state)
    now = int(time.time())
    last = int(bucket.get(key, 0) or 0)
    return (now - last) >= PERM_ALERT_COOLDOWN_SECONDS


def _mark_perm_alert_sent(guild_state: dict, key: str):
    bucket = _get_perm_alerts_bucket(guild_state)
    bucket[key] = int(time.time())


def build_perm_howto(channel: discord.abc.GuildChannel, missing: list[str]) -> str:
    pretty = "\n".join(f"• {PERM_LABELS.get(p, p)}" for p in missing)
    ch_name = getattr(channel, "name", "this channel")

    return (
        f"**Missing permissions in #{ch_name}:**\n"
        f"{pretty}\n\n"
        "**Fix (channel-specific):**\n"
        f"1) Right-click **#{ch_name}** → **Edit Channel**\n"
        "2) Go to **Permissions**\n"
        "3) **Add Members or Roles** → select **ChronoBot/Chromie** (or its role)\n"
        "4) Set these to **Allow (✅)**:\n"
        "   • View Channel\n"
        "   • Send Messages\n"
        "   • Embed Links\n"
        "   • Read Message History\n"
        "   • Manage Messages\n"
        "5) Remove any **red ❌ denies** for the bot/role (deny overrides allow)\n\n"
        "✅ Then run `/healthcheck` to confirm everything is fixed."
    )


async def notify_owner_missing_perms(
    guild: discord.Guild,
    channel: Optional[discord.abc.GuildChannel],
    *,
    missing: list[str],
    action: str,
):
    """DM server owner once per day per (guild, channel, missing-perms-set) about a permission issue."""
    if not missing:
        return

    guild_state = get_guild_state(guild.id)
    channel_id = getattr(channel, "id", 0) or 0

    # This makes “once per day per missing permission” behave the way you want:
    # same missing set + same channel + same action => 1 alert/day
    code = f"{action}|" + ",".join(sorted(missing))
    key = _perm_alert_key(guild.id, channel_id, code)

    if not _should_send_perm_alert(guild_state, key):
        return

    chan_name = f"#{getattr(channel, 'name', 'unknown')}" if channel else "(unknown channel)"
    header = (
        "⚠️ **ChronoBot permission issue**\n\n"
        f"I tried to **{action}** in **{chan_name}** on **{guild.name}**, but I’m missing permissions.\n\n"
    )

    howto = build_perm_howto(channel, missing) if channel else (
        "Please ensure the bot can View Channel, Send Messages, Embed Links, Read Message History, "
        "and Manage Messages in the channel you set for countdowns.\n\n"
        "✅ Then run `/healthcheck` to confirm everything is fixed."
    )

    footer = (
        "\n\n📅 I’ll only send one reminder per day for this specific issue.\n"
        "Next step: run `/healthcheck` (Manage Server) for diagnostics."
    )

    text = header + howto + footer

    # Try DM owner
    owner = guild.owner
    if owner is None:
        try:
            owner = await bot.fetch_user(guild.owner_id)
        except Exception:
            owner = None

    sent = False
    if owner:
        try:
            await owner.send(text)
            sent = True
        except (discord.Forbidden, discord.HTTPException):
            sent = False

    # Fallback: system channel or first sendable channel
    if not sent:
        fallback = guild.system_channel
        if fallback is None:
            me = _bot_member_cached(guild)
            for ch in guild.text_channels:
                if me and ch.permissions_for(me).send_messages:
                    fallback = ch
                    break

        if fallback:
            try:
                await fallback.send(text, allowed_mentions=discord.AllowedMentions.none())
            except Exception:
                pass

    # Mark (even if delivery failed) to avoid spam loops; will try again tomorrow
    _mark_perm_alert_sent(guild_state, key)
    save_state()

async def notify_event_channel_changed(
    guild: discord.Guild,
    *,
    actor: discord.abc.User,
    old_channel_id: Optional[int],
    new_channel: discord.TextChannel,
):
    """Notify owner + optionally post a lightweight audit message when event channel changes."""
    when = datetime.now(DEFAULT_TZ).strftime("%B %d, %Y at %I:%M %p %Z")

    old_ch_mention = "(not set)"
    if old_channel_id:
        old_ch = await get_text_channel(int(old_channel_id))
        if old_ch is not None:
            old_ch_mention = old_ch.mention
        else:
            old_ch_mention = f"(unknown channel id {old_channel_id})"

    msg = (
        "🔧 **ChronoBot configuration updated**\n"
        f"• Event channel: {old_ch_mention} → {new_channel.mention}\n"
        f"• Changed by: {getattr(actor, 'name', 'unknown')} (ID: {actor.id})\n"
        f"• When: {when}"
    )

    # ---- DM server owner (primary) ----
    owner = guild.owner
    if owner is None:
        try:
            owner = await bot.fetch_user(guild.owner_id)
        except Exception:
            owner = None

    if owner:
        try:
            await owner.send(msg)
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ---- Optional: post audit note in the NEW channel (nice for transparency) ----
    try:
        bot_member = await get_bot_member(guild)
        if bot_member and new_channel.permissions_for(bot_member).send_messages:
            await new_channel.send(msg, allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        pass

    # ---- Optional: post a breadcrumb in the OLD channel (helps people discover the move) ----
    if old_channel_id and int(old_channel_id) != int(new_channel.id):
        try:
            old_ch = await get_text_channel(int(old_channel_id))
            if old_ch:
                bot_member = await get_bot_member(guild)
                if bot_member and old_ch.permissions_for(bot_member).send_messages:
                    await old_ch.send(
                        f"🔁 ChronoBot event channel was moved to {new_channel.mention} on {when}.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
        except Exception:
            pass


async def get_bot_member(guild: discord.Guild) -> Optional[discord.Member]:
    if not bot.user:
        return None
    m = guild.get_member(bot.user.id)
    if m:
        return m
    try:
        return await guild.fetch_member(bot.user.id)
    except Exception:
        return None

async def send_onboarding_for_guild(guild: discord.Guild):
    guild_state = get_guild_state(guild.id)

    if guild_state.get("welcomed"):
        return

    contact_user = guild.owner
    if contact_user is None:
        try:
            contact_user = await bot.fetch_user(guild.owner_id)
        except Exception:
            contact_user = None

    mention = contact_user.mention if contact_user else ""
    milestone_str = ", ".join(str(x) for x in DEFAULT_MILESTONES)

    # -----------------------------
    # Message 1: Base features
    # -----------------------------
    base_message = (
        f"Hey {mention}! Thanks for inviting **ChronoBot** to **{guild.name}** 🕒✨\n\n"
        "I’m **Chromie** — your server’s confident little timekeeper. I pin a clean countdown list and post reminders "
        "so nobody has to do the mental math (or the panic).\n\n"
        "**⚡ Quick start (30 seconds):**\n"
        "1) In your events channel: `/seteventchannel`\n"
        "2) Add an event: `/addevent date: 04/12/2026 time: 09:00 name: Game Night 🎲`\n\n"
        "**🧭 Core commands:**\n"
        "• `/listevents` (shows event numbers)\n"
        "• `/eventinfo index:` (details)\n"
        "• `/editevent` • `/dupeevent` • `/removeevent`\n"
        "• `/remindall` (manual reminder)\n"
        "• `/silence` (pause reminders without deleting)\n\n"
        "**🔔 Reminders & mentions:**\n"
        f"Milestone reminders post in your event channel ({milestone_str} by default). "
        "Timezone is **America/Chicago**.\n"
        "Want role pings? Use `/setmentionrole` (clear with `/clearmentionrole`).\n\n"
        "**🛠️ Troubleshooting:**\n"
        "Run `/healthcheck` — it shows your configured channel + whether I can view/send/embed/read history/pin.\n"
        "(Past events auto-remove after they pass so the list stays tidy.)\n\n"
        "**More help:** `/chronohelp`\n"
        f"FAQ: {FAQ_URL}\n"
        f"Support server: {SUPPORT_SERVER_URL}\n\n"
        "Alright — I’ll be over here, politely bullying time into behaving. 💜"
    )

    # -----------------------------
    # Message 2: Supporter features
    # -----------------------------
    supporter_message = (
        "**💜 Supporter perks (free vote unlocks):**\n"
        "ChronoBot is free. Voting on Top.gg helps it grow — and unlocks bonus features.\n\n"
        "Run `/vote` to get the link + confirm your status. Voting unlocks:\n"
        "• `/theme` — style the pinned countdown\n"
        "• `/milestones advanced` — server-wide default milestone schedule\n"
        "• `/template save` + `/template load` — reusable event setups\n"
        "• `/banner set` — event banner images\n"
        "• `/digest enable` — weekly “next 7 days” recap\n\n"
        "If anything seems stuck after unlocking, run `/vote` again (Top.gg can take a moment to reflect your vote)."
    )

    sent_dm = False

    # Try DM first (preferred)
    if contact_user:
        try:
            await contact_user.send(base_message)
            await contact_user.send(supporter_message)
            sent_dm = True
        except discord.Forbidden:
            sent_dm = False
        except Exception:
            sent_dm = False

    # Fallback: post in a channel (base message only to avoid “promo spam” vibe)
    if not sent_dm:
        fallback_channel = guild.system_channel

        if fallback_channel is None:
            bot_m = await get_bot_member(guild)
            for ch in guild.text_channels:
                target = bot_m if bot_m is not None else guild.default_role
                perms = ch.permissions_for(target)
                if perms.view_channel and perms.send_messages:
                    fallback_channel = ch
                    break

        if fallback_channel is not None:
            try:
                await fallback_channel.send(
                    base_message,
                    allowed_mentions=discord.AllowedMentions.none()
                )
            except Exception:
                pass

    guild_state["welcomed"] = True
    save_state()



async def notify_owner_countdown_unpinned(
    guild: discord.Guild,
    channel: discord.TextChannel,
    *,
    reason: str,
):
    """
    DM server owner once/day if the countdown message is NOT pinned
    (e.g., someone unpinned it, pin limit reached, or Discord error).
    """
    guild_state = get_guild_state(guild.id)

    # Reuse the persisted 1/day cooldown bucket
    key = _perm_alert_key(guild.id, channel.id, f"countdown_unpinned|{reason}")
    if not _should_send_perm_alert(guild_state, key):
        return

    ch_name = getattr(channel, "name", "this channel")
    text = (
        "📌 **ChronoBot notice: countdown message is not pinned**\n\n"
        f"I found the countdown message in **#{ch_name}**, but it is currently **not pinned**.\n"
        "That means it can scroll away and won’t stay at the top.\n\n"
        "**How to fix:**\n"
        "1) In that channel, make sure the bot has **Manage Messages** (pin/unpin/delete reminders)\n"
        "2) If the channel has too many pinned messages, unpin one (Discord has a pin limit)\n\n"
        "✅ Then run `/healthcheck` to confirm everything is fixed."
    )

    # Try DM owner
    owner = guild.owner
    if owner is None:
        try:
            owner = await bot.fetch_user(guild.owner_id)
        except Exception:
            owner = None

    sent = False
    if owner:
        try:
            await owner.send(text)
            sent = True
        except (discord.Forbidden, discord.HTTPException):
            sent = False

    # Fallback: system channel or first sendable channel
    if not sent:
        fallback = guild.system_channel
        if fallback is None:
            me = _bot_member_cached(guild)
            for ch in guild.text_channels:
                if me and ch.permissions_for(me).send_messages:
                    fallback = ch
                    break

        if fallback:
            try:
                await fallback.send(text, allowed_mentions=discord.AllowedMentions.none())
            except Exception:
                pass

    _mark_perm_alert_sent(guild_state, key)
    save_state()


async def ensure_countdown_pinned(
    guild: discord.Guild,
    channel: discord.TextChannel,
    msg: discord.Message,
    *,
    perms: Optional[discord.Permissions] = None,
):
    """
    If msg is not pinned, attempt to pin (if possible), otherwise DM the owner.
    """
    # Resolve perms if caller didn't provide them
    if perms is None:
        bot_member = await get_bot_member(guild)
        if bot_member is None:
            await notify_owner_missing_perms(
                guild,
                channel,
                missing=list(RECOMMENDED_CHANNEL_PERMS),
                action="pin the countdown message (it is currently unpinned)",
            )
            return
        perms = channel.permissions_for(bot_member)

    try:
        if msg.pinned:
            return
    except Exception:
        return

    if not perms.manage_messages:
        await notify_owner_missing_perms(
            guild,
            channel,
            missing=["manage_messages"],
            action="pin the countdown message (it is currently unpinned)",
        )
        return

    try:
        await msg.pin()
    except discord.Forbidden:
        await notify_owner_missing_perms(
            guild,
            channel,
            missing=["manage_messages"],
            action="pin the countdown message (it is currently unpinned)",
        )
    except discord.HTTPException:
        await notify_owner_countdown_unpinned(guild, channel, reason="pin_failed_http")
    except Exception:
        await notify_owner_countdown_unpinned(guild, channel, reason="pin_failed_unknown")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id}) [{VERSION}]")


@bot.event
async def on_guild_join(guild: discord.Guild):
    g_state = get_guild_state(guild.id)
    sort_events(g_state)
    save_state()
    await send_onboarding_for_guild(guild)


# ==========================
# EMBED HELPERS
# ==========================
MAX_EMBED_EVENTS = 25  # Discord embed field limit

def _append_vote_footer(existing: Optional[str]) -> str:
    tail = "💜 Support Chromie: /vote"
    if not existing:
        return tail
    return f"{existing} • {tail}"
    
# ---------------------------
# HELP PAGES (short + scannable)
# ---------------------------

HELP_PAGES = {
    "quick": {
        "title": "Chromie Help 🕒✨",
        "desc": (
            "**Quick start:**\n"
            "1) In your events channel: `/seteventchannel`\n"
            "2) Add one: `/addevent`\n"
            "3) Chromie keeps the pinned countdown updated.\n\n"
            "Use the dropdown to browse commands by category."
        ),
        "lines": [
            "`/seteventchannel` — set the pinned countdown channel",
            "`/addevent` — add an event",
            "`/listevents` — list events + index numbers",
            "`/update_countdown` — force refresh (troubleshooting)",
        ],
    },
    "events": {
        "title": "Events",
        "desc": "Create, view, edit, and organize events.",
        "lines": [
            "`/addevent` — add an event",
            "`/listevents` — list all events",
            "`/nextevent` — show next upcoming event",
            "`/eventinfo index:` — details for one event",
            "`/editevent index:` — edit name/date/time",
            "`/dupeevent index:` — duplicate an event",
            "`/removeevent index:` — delete an event",
        ],
    },
    "reminders": {
        "title": "Reminders",
        "desc": "Milestones + manual reminders + repeating reminders.",
        "lines": [
            "`/remindall index:` — post a reminder now",
            "`/setmilestones index: milestones:` — custom milestone days",
            "`/resetmilestones index:` — restore default milestones",
            "`/silence index:` — stop reminders for an event",
            "`/setrepeat index: every_days:` — repeat reminders",
            "`/clearrepeat index:` — turn repeat off",
        ],
    },
    "customize": {
        "title": "Customization",
        "desc": "Make the pinned countdown match your server vibe.",
        "lines": [
            "`/theme` — change the countdown theme",
            "`/banner set` — set a banner image",
            "`/banner clear` — remove the banner",
            "`/countdown title` — set pinned title (supporter-only if enabled)",
            "`/countdown description` — set pinned description (supporter-only if enabled)",
        ],
    },
    "owner": {
        "title": "Owner DMs",
        "desc": "Assign an owner to an event.",
        "lines": [
            "`/seteventowner index: user:` — assign owner",
            "`/cleareventowner index:` — remove owner",
        ],
    },
    "supporter": {
        "title": "Supporter (Top.gg Vote)",
        "desc": "Vote perks + diagnostics.",
        "lines": [
            "`/vote` — check supporter status + link",
            "`/vote_debug` — admin vote verification debug",
        ],
    },
    "maintenance": {
        "title": "Maintenance",
        "desc": "Config resets + cleanup + diagnostics.",
        "lines": [
            "`/healthcheck` — check permissions/config",
            "`/resendsetup` — resend onboarding",
            "`/resetchannel` — clear event channel",
            "`/purgeevents confirm: YES` — delete all events",
            "`/archivepast` — manual cleanup (rare)",
        ],
    },
    "dm": {
        "title": "DM Control",
        "desc": "Add events from DMs after linking a server.",
        "lines": [
            "`/linkserver` — link your DMs to this server",
            "Then DM: `/addevent` — adds to your linked server",
        ],
    },
}

HELP_OPTIONS = [
    ("Quick Start", "quick", "Start here"),
    ("Events", "events", "Add/edit/list"),
    ("Reminders", "reminders", "Milestones & repeats"),
    ("Customization", "customize", "Themes & banners"),
    ("Owner DMs", "owner", "Assign event owner"),
    ("Supporter", "supporter", "Vote perks"),
    ("Maintenance", "maintenance", "Healthcheck & resets"),
    ("DM Control", "dm", "Link + DM add"),
]


def build_help_embed(page_key: str) -> discord.Embed:
    page = HELP_PAGES.get(page_key, HELP_PAGES["quick"])
    e = discord.Embed(
        title=page["title"],
        description=page["desc"],
        color=EMBED_COLOR,
    )

    e.add_field(
        name="Commands",
        value="\n".join(page["lines"]),
        inline=False,
    )

    # Keep links out of the main text so it stays readable
    links = []
    if FAQ_URL:
        links.append(f"FAQ: {FAQ_URL}")
    if SUPPORT_SERVER_URL:
        links.append(f"Support: {SUPPORT_SERVER_URL}")
    if links:
        e.set_footer(text=" • ".join(links))

    return e


class HelpSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=label, value=value, description=desc)
            for (label, value, desc) in HELP_OPTIONS
        ]
        super().__init__(
            placeholder="Pick a help category…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        await interaction.response.edit_message(embed=build_help_embed(key), view=self.view)


class HelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(HelpSelect())


# Backwards-compatible wrapper (optional)
def build_chronohelp_embed() -> discord.Embed:
    return build_help_embed("quick")


def chunk_text(text: str, limit: int = 1900) -> list[str]:
    """
    Split text into Discord-safe chunks.
    Uses newlines when possible so it doesn't chop in the middle of a line.
    """
    text = (text or "").strip()
    if not text:
        return ["(no help text)"]

    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut == -1 or cut < int(limit * 0.6):
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n").lstrip()
    if text:
        chunks.append(text)
    return chunks



# ---- THEMES (pool-based) ----
# These replace all prior supporter themes.
THEME_ALIASES: Dict[str, str] = {
    "default": "classic",
    "classic": "classic",
    "football": "football",
    "basketball": "basketball",
    "baseball": "baseball",
    "raid": "raidnight",
    "dnd": "dnd",
    "girly": "girly",
    "cute": "girly",
    "work": "workplace",
    "office": "workplace",
    "romance": "romance",
    "vacation": "vacation",
    "hype": "hype",
    "minimal": "minimal",
    "school": "school",
    "spooky": "spooky",
}

DEFAULT_FOOTER_POOL = [
    "⏳ ChronoBot • Time is fake, deadlines are real.",
    "💫 ChronoBot • Tip: /chronohelp for commands",
    "🗳️ Supporter themes unlock with /vote",
]

def pick_theme_footer(theme_id: str, profile: Dict[str, Any], *, seed: str) -> str:
    pool = profile.get("footer_pool") or DEFAULT_FOOTER_POOL
    label = profile.get("label", theme_id.title())
    text = _stable_pick(pool, f"{theme_id}|footer|{seed}")
    return text.replace("{label}", label)

# Full 14-theme registry (Chrono Purple Classic is the default + always available)
# Keys are the canonical theme IDs you’ll reference in guild_state["theme"] / /settheme.

THEMES: Dict[str, Dict[str, Any]] = {
    "classic": {
        "label": "Chrono Purple (Classic)",
        "supporter_only": False,
        "color": EMBED_COLOR,  # Chrono Purple
        "pin_title_pool": [
            "⏳ Chrono Countdown Board",
            "💜 Chrono Purple Timeline",
            "🕒 Chrono Countdown",
            "✨ Countdown Board",
            "⌛ Event Timeline",
        ],
        "event_emoji_pool": ["🕒", "⏳", "⌛", "💜", "✨", "🔔"],
        "milestone_emoji_pool": ["⏳", "🕒", "🔔", "✨", "💜"],
        "milestone_templates": {
            "default": [
                "{emoji} **{event}** is in **{days} days** ({time_left}) • **{date}**",
                "{emoji} Countdown check-in: **{event}** — **{days} days** left ({time_left}) • **{date}**",
                "{emoji} Time update: **{event}** arrives in **{days} days** ({time_left}) • **{date}**",
                "{emoji} Heads up — **{event}** is **{days} days** out ({time_left}) • **{date}**",
                "{emoji} On the horizon: **{event}** in **{days} days** ({time_left}) • **{date}**",
            ],
            "one_day": [
                "{emoji} **{event}** is **tomorrow** ({time_left}) • **{date}**",
                "{emoji} Tomorrow: **{event}** ({time_left}) • **{date}**",
                "{emoji} 1-day warning: **{event}** is tomorrow ({time_left}) • **{date}**",
                "{emoji} Almost there — **{event}** is tomorrow ({time_left}) • **{date}**",
                "{emoji} Tomorrow’s schedule: **{event}** ({time_left}) • **{date}**",
            ],
            "zero_day": [
                "{emoji} **{event}** is **today** ({time_left}) • **{date}**",
                "{emoji} Today’s the day: **{event}** ({time_left}) • **{date}**",
                "{emoji} It’s happening today: **{event}** ({time_left}) • **{date}**",
                "{emoji} Today: **{event}** ({time_left}) • **{date}**",
                "{emoji} The wait is over — **{event}** is today ({time_left}) • **{date}**",
            ],
        },
        "repeat_templates": [
            "{emoji} 🔁 **{event}** repeats — next up in **{time_left}** (on **{date}**).",
            "{emoji} 🔁 Looping event: **{event}** — **{time_left}** until the next round (on **{date}**).",
            "{emoji} 🔁 Recurring: **{event}** returns in **{time_left}** (on **{date}**).",
            "{emoji} 🔁 **{event}** cycles again in **{time_left}** • **{date}**",
            "{emoji} 🔁 Next occurrence of **{event}** in **{time_left}** • **{date}**",
        ],
        "remindall_templates": [
            "{emoji} Reminder: **{event}** is in **{time_left}** (on **{date}**).",
            "{emoji} Don’t forget: **{event}** in **{time_left}** • **{date}**",
            "{emoji} Upcoming: **{event}** — **{time_left}** remaining • **{date}**",
            "{emoji} Calendar ping: **{event}** in **{time_left}** • **{date}**",
            "{emoji} Heads up — **{event}** is **{time_left}** away • **{date}**",
        ],
        "start_blast_templates": [
            "⏰ **{event}** is happening now!",
            "🚀 It’s time: **{event}** starts now!",
            "✨ Go time! **{event}** is live!",
            "🔔 Now: **{event}**",
            "🕒 **{event}** begins now!",
        ],
    },

    "football": {
        "label": "Football",
        "supporter_only": True,
        "color": discord.Color.from_rgb(31, 139, 76),  # turf green
        "pin_title_pool": [
            "🏈 Game Day Countdown Board",
            "🏈 Kickoff Counter",
            "🏟️ Sunday Schedule",
            "📣 Next Kickoffs",
            "⏱️ The Play Clock",
        ],
        "event_emoji_pool": ["🏈", "🏟️", "📣", "🧢", "🔥", "⏱️"],
        "milestone_emoji_pool": ["🏈", "📣", "⏱️", "🏟️", "🔥"],
        "milestone_templates": {
            "default": [
                "{emoji} Clock’s running: **{event}** in **{days} days** ({time_left}) • **{date}**",
                "{emoji} Pregame notice — **{event}** is **{days} days** out ({time_left}) • **{date}**",
                "{emoji} Drive update: **{event}** — **{days} days** to go ({time_left}) • **{date}**",
                "{emoji} On the schedule: **{event}** in **{days} days** ({time_left}) • **{date}**",
                "{emoji} Game plan check: **{event}** is **{days} days** away ({time_left}) • **{date}**",
            ],
            "one_day": [
                "{emoji} Final warm-up — **{event}** is **tomorrow** ({time_left}) • **{date}**",
                "{emoji} Tomorrow’s kickoff: **{event}** ({time_left}) • **{date}**",
                "{emoji} 1-day drill: **{event}** is tomorrow ({time_left}) • **{date}**",
                "{emoji} Tomorrow: **{event}** — no false starts ({time_left}) • **{date}**",
                "{emoji} Almost kickoff — **{event}** is tomorrow ({time_left}) • **{date}**",
            ],
            "zero_day": [
                "{emoji} It’s game day: **{event}** is **today** ({time_left}) • **{date}**",
                "{emoji} Kickoff day — **{event}** is today ({time_left}) • **{date}**",
                "{emoji} TODAY: **{event}** ({time_left}) • **{date}**",
                "{emoji} No timeouts — **{event}** is today ({time_left}) • **{date}**",
                "{emoji} We’re live today: **{event}** ({time_left}) • **{date}**",
            ],
        },
        "repeat_templates": [
            "{emoji} 🔁 **{event}** repeats — next kickoff in **{time_left}** (on **{date}**).",
            "{emoji} 🔁 Run it back: **{event}** returns in **{time_left}** • **{date}**",
            "{emoji} 🔁 Replay scheduled — **{event}** in **{time_left}** (on **{date}**).",
            "{emoji} 🔁 Next drive: **{event}** in **{time_left}** • **{date}**",
            "{emoji} 🔁 **{event}** cycles again in **{time_left}** • **{date}**",
        ],
        "remindall_templates": [
            "{emoji} Reminder: **{event}** in **{time_left}** (on **{date}**).",
            "{emoji} Pregame ping: **{event}** in **{time_left}** • **{date}**",
            "{emoji} Schedule update — **{event}** is **{time_left}** away • **{date}**",
            "{emoji} Two-minute warning (but longer): **{event}** in **{time_left}** • **{date}**",
            "{emoji} Keep your eyes up: **{event}** in **{time_left}** • **{date}**",
        ],
        "start_blast_templates": [
            "🏈 **{event}** starts now — kickoff time!",
            "📣 It’s on: **{event}** is live!",
            "⏱️ Clock’s live — **{event}** begins now!",
            "🏟️ Welcome to game time: **{event}** starts now!",
            "🔥 GO TIME: **{event}** is happening now!",
        ],
    },

    "basketball": {
        "label": "Basketball",
        "supporter_only": True,
        "color": discord.Color.from_rgb(242, 140, 40),  # orange
        "pin_title_pool": [
            "🏀 Tip-Off Countdown",
            "🏀 Court Calendar",
            "⏱️ Shot Clock Schedule",
            "🔥 Clutch Time Board",
            "📣 Next Tip-Offs",
        ],
        "event_emoji_pool": ["🏀", "⛹️", "🔥", "⏱️", "📣", "🏟️"],
        "milestone_emoji_pool": ["🏀", "⏱️", "🔥", "📣", "🏟️"],
        "milestone_templates": {
            "default": [
                "{emoji} On the clock: **{event}** in **{days} days** ({time_left}) • **{date}**",
                "{emoji} Court update — **{event}** is **{days} days** away ({time_left}) • **{date}**",
                "{emoji} Warmups pending: **{event}** in **{days} days** ({time_left}) • **{date}**",
                "{emoji} Next possession: **{event}** in **{days} days** ({time_left}) • **{date}**",
                "{emoji} Scoreboard check: **{event}** — **{days} days** left ({time_left}) • **{date}**",
            ],
            "one_day": [
                "{emoji} Tomorrow: **{event}** tips off ({time_left}) • **{date}**",
                "{emoji} 1 day left — **{event}** is tomorrow ({time_left}) • **{date}**",
                "{emoji} Final warmup — **{event}** tomorrow ({time_left}) • **{date}**",
                "{emoji} Tomorrow’s matchup: **{event}** ({time_left}) • **{date}**",
                "{emoji} Almost tip-off — **{event}** is tomorrow ({time_left}) • **{date}**",
            ],
            "zero_day": [
                "{emoji} It’s tip-off day: **{event}** is **today** ({time_left}) • **{date}**",
                "{emoji} TODAY: **{event}** ({time_left}) • **{date}**",
                "{emoji} Buzzer’s coming — **{event}** is today ({time_left}) • **{date}**",
                "{emoji} Game time today: **{event}** ({time_left}) • **{date}**",
                "{emoji} Clutch time — **{event}** happens today ({time_left}) • **{date}**",
            ],
        },
        "repeat_templates": [
            "{emoji} 🔁 **{event}** repeats — next tip-off in **{time_left}** (on **{date}**).",
            "{emoji} 🔁 Run it back — **{event}** in **{time_left}** • **{date}**",
            "{emoji} 🔁 Next game: **{event}** returns in **{time_left}** • **{date}**",
            "{emoji} 🔁 Replay scheduled — **{event}** in **{time_left}** (on **{date}**).",
            "{emoji} 🔁 **{event}** cycles again in **{time_left}** • **{date}**",
        ],
        "remindall_templates": [
            "{emoji} Reminder: **{event}** in **{time_left}** (on **{date}**).",
            "{emoji} Shot clock ping: **{event}** in **{time_left}** • **{date}**",
            "{emoji} Court schedule — **{event}** is **{time_left}** away • **{date}**",
            "{emoji} Keep it moving: **{event}** in **{time_left}** • **{date}**",
            "{emoji} Next up: **{event}** in **{time_left}** • **{date}**",
        ],
        "start_blast_templates": [
            "🏀 **{event}** starts now — tip-off!",
            "⏱️ Shot clock starts — **{event}** is live!",
            "🔥 It’s on: **{event}** begins now!",
            "📣 Game time: **{event}** starts now!",
            "🏟️ Welcome to the court — **{event}** is live!",
        ],
    },

    "baseball": {
        "label": "Baseball",
        "supporter_only": True,
        "color": discord.Color.from_rgb(11, 31, 91),  # deep navy
        "pin_title_pool": [
            "⚾ Diamond Dateboard",
            "⚾ On-Deck Countdowns",
            "🏟️ Ballpark Schedule",
            "📣 Next First Pitches",
            "🧢 Dugout Timeline",
        ],
        "event_emoji_pool": ["⚾", "🧢", "🏟️", "📣", "🔥", "🧤"],
        "milestone_emoji_pool": ["⚾", "🧢", "🏟️", "📣", "🔥"],
        "milestone_templates": {
            "default": [
                "{emoji} On deck: **{event}** in **{days} days** ({time_left}) • **{date}**",
                "{emoji} Scoreboard check — **{event}** is **{days} days** away ({time_left}) • **{date}**",
                "{emoji} Dugout note: **{event}** in **{days} days** ({time_left}) • **{date}**",
                "{emoji} Next inning up: **{event}** — **{days} days** left ({time_left}) • **{date}**",
                "{emoji} First pitch approaches: **{event}** in **{days} days** ({time_left}) • **{date}**",
            ],
            "one_day": [
                "{emoji} Tomorrow: **{event}** — first pitch soon ({time_left}) • **{date}**",
                "{emoji} 1 day out — **{event}** is tomorrow ({time_left}) • **{date}**",
                "{emoji} Tomorrow’s lineup: **{event}** ({time_left}) • **{date}**",
                "{emoji} Almost game time — **{event}** tomorrow ({time_left}) • **{date}**",
                "{emoji} Tomorrow on the diamond: **{event}** ({time_left}) • **{date}**",
            ],
            "zero_day": [
                "{emoji} Play ball — **{event}** is **today** ({time_left}) • **{date}**",
                "{emoji} TODAY: **{event}** ({time_left}) • **{date}**",
                "{emoji} It’s game day: **{event}** today ({time_left}) • **{date}**",
                "{emoji} First pitch day — **{event}** is today ({time_left}) • **{date}**",
                "{emoji} Ballpark time — **{event}** happens today ({time_left}) • **{date}**",
            ],
        },
        "repeat_templates": [
            "{emoji} 🔁 **{event}** repeats — next first pitch in **{time_left}** (on **{date}**).",
            "{emoji} 🔁 Rerun scheduled — **{event}** in **{time_left}** • **{date}**",
            "{emoji} 🔁 Next game: **{event}** in **{time_left}** (on **{date}**).",
            "{emoji} 🔁 **{event}** cycles again in **{time_left}** • **{date}**",
            "{emoji} 🔁 Back on the roster: **{event}** returns in **{time_left}** • **{date}**",
        ],
        "remindall_templates": [
            "{emoji} Reminder: **{event}** in **{time_left}** (on **{date}**).",
            "{emoji} On-deck ping: **{event}** in **{time_left}** • **{date}**",
            "{emoji} Ballpark schedule — **{event}** is **{time_left}** away • **{date}**",
            "{emoji} Keep your eye on it: **{event}** in **{time_left}** • **{date}**",
            "{emoji} Next up: **{event}** in **{time_left}** • **{date}**",
        ],
        "start_blast_templates": [
            "⚾ **{event}** starts now — play ball!",
            "📣 Now batting: **{event}**!",
            "🏟️ First pitch — **{event}** is live!",
            "🔥 It’s on: **{event}** begins now!",
            "🧢 Game time — **{event}** starts now!",
        ],
    },

    "raidnight": {
        "label": "Raid Night",
        "supporter_only": True,
        "color": discord.Color.from_rgb(155, 93, 229),  # neon purple
        "pin_title_pool": [
            "🎮 Raid Night Queue",
            "🛡️ Party Finder",
            "⚔️ Pull Timer Board",
            "🧩 Objective HUD",
            "🔔 Ready Check Board",
        ],
        "event_emoji_pool": ["🎮", "🛡️", "⚔️", "🧩", "🗡️", "🪙", "🏆"],
        "milestone_emoji_pool": ["🛡️", "⚔️", "🎮", "🔔", "🏆"],
        "milestone_templates": {
            "default": [
                "{emoji} Queue update: **{event}** in **{days} days** ({time_left}) • **{date}**",
                "{emoji} Objective ping — **{event}** is **{days} days** out ({time_left}) • **{date}**",
                "{emoji} Prep check: **{event}** in **{days} days** ({time_left}) • **{date}**",
                "{emoji} Party up soon: **{event}** — **{days} days** left ({time_left}) • **{date}**",
                "{emoji} Cooldown ticking — **{event}** in **{days} days** ({time_left}) • **{date}**",
            ],
            "one_day": [
                "{emoji} Tomorrow: **{event}** — ready check soon ({time_left}) • **{date}**",
                "{emoji} 1 day left — **{event}** is tomorrow ({time_left}) • **{date}**",
                "{emoji} Consumables reminder: **{event}** tomorrow ({time_left}) • **{date}**",
                "{emoji} Tomorrow’s raid: **{event}** ({time_left}) • **{date}**",
                "{emoji} Almost pull time — **{event}** tomorrow ({time_left}) • **{date}**",
            ],
            "zero_day": [
                "{emoji} It’s raid day: **{event}** is **today** ({time_left}) • **{date}**",
                "{emoji} TODAY: **{event}** ({time_left}) • **{date}**",
                "{emoji} Boss is up — **{event}** today ({time_left}) • **{date}**",
                "{emoji} Ready check: **{event}** is today ({time_left}) • **{date}**",
                "{emoji} Pull timer at zero — **{event}** today ({time_left}) • **{date}**",
            ],
        },
        "repeat_templates": [
            "{emoji} 🔁 **{event}** repeats — next respawn in **{time_left}** (on **{date}**).",
            "{emoji} 🔁 Reset complete — **{event}** returns in **{time_left}** • **{date}**",
            "{emoji} 🔁 Next run of **{event}** in **{time_left}** • **{date}**",
            "{emoji} 🔁 Looping objective: **{event}** in **{time_left}** (on **{date}**).",
            "{emoji} 🔁 **{event}** cycles again in **{time_left}** • **{date}**",
        ],
        "remindall_templates": [
            "{emoji} Reminder ping: **{event}** in **{time_left}** (on **{date}**).",
            "{emoji} Queue notice — **{event}** is **{time_left}** away • **{date}**",
            "{emoji} Party finder: **{event}** in **{time_left}** • **{date}**",
            "{emoji} Don’t AFK: **{event}** in **{time_left}** • **{date}**",
            "{emoji} Start time locked — **{event}** in **{time_left}** • **{date}**",
        ],
        "start_blast_templates": [
            "🛡️ **{event}** is live — ready check!",
            "⚔️ Pull now: **{event}** starts!",
            "🎮 GG — **{event}** begins now!",
            "🔔 Start signal: **{event}** is happening now!",
            "🏆 Go time: **{event}** starts now!",
        ],
    },

    "dnd": {
        "label": "D&D Campaign Night",
        "supporter_only": True,
        "color": discord.Color.from_rgb(139, 94, 52),  # leather/parchment
        "pin_title_pool": [
            "🐉 Campaign Night Ledger",
            "🎲 Session Countdown",
            "📜 The Next Chapter",
            "🕯️ Tavern Calendar",
            "🗺️ Quest Schedule",
        ],
        "event_emoji_pool": ["🎲", "🐉", "📜", "🕯️", "🗺️", "🗡️", "🛡️"],
        "milestone_emoji_pool": ["🎲", "🐉", "📜", "🕯️", "🗡️"],
        "milestone_templates": {
            "default": [
                "{emoji} Session notice: **{event}** in **{days} days** ({time_left}) • **{date}**",
                "{emoji} From the DM’s notes — **{event}** is **{days} days** away ({time_left}) • **{date}**",
                "{emoji} The tale continues: **{event}** in **{days} days** ({time_left}) • **{date}**",
                "{emoji} Party gathers soon: **{event}** — **{days} days** left ({time_left}) • **{date}**",
                "{emoji} Map check: **{event}** in **{days} days** ({time_left}) • **{date}**",
            ],
            "one_day": [
                "{emoji} Tomorrow: **{event}** — prepare your spells ({time_left}) • **{date}**",
                "{emoji} 1 day left — **{event}** is tomorrow ({time_left}) • **{date}**",
                "{emoji} Tomorrow’s session: **{event}** ({time_left}) • **{date}**",
                "{emoji} Long rest tonight — **{event}** tomorrow ({time_left}) • **{date}**",
                "{emoji} The tavern doors open tomorrow: **{event}** ({time_left}) • **{date}**",
            ],
            "zero_day": [
                "{emoji} Roll initiative — **{event}** is **today** ({time_left}) • **{date}**",
                "{emoji} TODAY: **{event}** ({time_left}) • **{date}**",
                "{emoji} The party assembles today: **{event}** ({time_left}) • **{date}**",
                "{emoji} The chapter begins today: **{event}** ({time_left}) • **{date}**",
                "{emoji} Adventure time — **{event}** is today ({time_left}) • **{date}**",
            ],
        },
        "repeat_templates": [
            "{emoji} 🔁 **{event}** repeats — next session in **{time_left}** (on **{date}**).",
            "{emoji} 🔁 The story loops — **{event}** returns in **{time_left}** • **{date}**",
            "{emoji} 🔁 Next chapter of **{event}** in **{time_left}** • **{date}**",
            "{emoji} 🔁 Recurring quest: **{event}** in **{time_left}** (on **{date}**).",
            "{emoji} 🔁 **{event}** cycles again in **{time_left}** • **{date}**",
        ],
        "remindall_templates": [
            "{emoji} Reminder: **{event}** in **{time_left}** (on **{date}**).",
            "{emoji} Session ping — **{event}** is **{time_left}** away • **{date}**",
            "{emoji} Don’t forget your dice: **{event}** in **{time_left}** • **{date}**",
            "{emoji} Next on the ledger: **{event}** in **{time_left}** • **{date}**",
            "{emoji} Story time soon — **{event}** in **{time_left}** • **{date}**",
        ],
        "start_blast_templates": [
            "🎲 **{event}** starts now — roll initiative!",
            "🐉 The session begins: **{event}** is live!",
            "🕯️ The tavern doors open — **{event}** starts now!",
            "📜 New chapter: **{event}** begins now!",
            "🗡️ Adventure begins — **{event}** is happening now!",
        ],
    },

    "girly": {
        "label": "Cute Aesthetic",
        "supporter_only": True,
        "color": discord.Color.from_rgb(255, 93, 162),  # bubblegum pink
        "pin_title_pool": [
            "🎀 Pretty Plans Countdown",
            "💖 Pink Calendar Board",
            "✨ Cute Countdowns",
            "🌸 Soft Schedule",
            "🫧 Sweet Timeline",
        ],
        "event_emoji_pool": ["🎀", "💖", "💕", "✨", "🌸", "🫧", "🩷"],
        "milestone_emoji_pool": ["💖", "🎀", "✨", "🌸", "🫧"],
        "milestone_templates": {
            "default": [
                "{emoji} Friendly ping: **{event}** in **{days} days** ({time_left}) • **{date}**",
                "{emoji} Hearts up — **{event}** is **{days} days** away ({time_left}) • **{date}**",
                "{emoji} Cute reminder: **{event}** in **{days} days** ({time_left}) • **{date}**",
                "{emoji} Soft schedule check: **{event}** — **{days} days** left ({time_left}) • **{date}**",
                "{emoji} Little countdown moment: **{event}** in **{days} days** ({time_left}) • **{date}**",
            ],
            "one_day": [
                "{emoji} Tomorrow!! **{event}** ({time_left}) • **{date}**",
                "{emoji} Aaa— **{event}** is tomorrow ({time_left}) • **{date}**",
                "{emoji} One sleep left: **{event}** tomorrow ({time_left}) • **{date}**",
                "{emoji} Tomorrow’s the moment: **{event}** ({time_left}) • **{date}**",
                "{emoji} Almost here — **{event}** is tomorrow ({time_left}) • **{date}**",
            ],
            "zero_day": [
                "{emoji} It’s **today**!! **{event}** ({time_left}) • **{date}**",
                "{emoji} Sparkly alert: **{event}** is today ({time_left}) • **{date}**",
                "{emoji} TODAY: **{event}** ({time_left}) • **{date}**",
                "{emoji} The cute countdown hits zero — **{event}** today ({time_left}) • **{date}**",
                "{emoji} We made it: **{event}** is today ({time_left}) • **{date}**",
            ],
        },
        "repeat_templates": [
            "{emoji} 🔁 **{event}** repeats — next cute countdown in **{time_left}** (on **{date}**).",
            "{emoji} 🔁 Again soon: **{event}** in **{time_left}** • **{date}**",
            "{emoji} 🔁 Recurring sparkle: **{event}** returns in **{time_left}** • **{date}**",
            "{emoji} 🔁 Looping plans — **{event}** in **{time_left}** (on **{date}**).",
            "{emoji} 🔁 **{event}** cycles again in **{time_left}** • **{date}**",
        ],
        "remindall_templates": [
            "{emoji} Tiny reminder: **{event}** in **{time_left}** (on **{date}**).",
            "{emoji} Cute ping — **{event}** is **{time_left}** away • **{date}**",
            "{emoji} Calendar sparkle: **{event}** in **{time_left}** • **{date}**",
            "{emoji} Heads up bestie: **{event}** in **{time_left}** • **{date}**",
            "{emoji} Just a lil note: **{event}** in **{time_left}** • **{date}**",
        ],
        "start_blast_templates": [
            "💖 **{event}** is happening now!",
            "🎀 It’s time! **{event}** starts now!",
            "✨ Go time — **{event}** is live!",
            "🌸 Now: **{event}**",
            "🫧 The moment is here: **{event}** starts now!",
        ],
    },

    "workplace": {
        "label": "Workplace Ops",
        "supporter_only": True,
        "color": discord.Color.from_rgb(75, 85, 99),  # slate
        "pin_title_pool": [
            "📌 Key Dates",
            "🗓️ Operations Schedule",
            "📋 Calendar Board",
            "📈 Timeline Overview",
            "✅ Upcoming Items",
        ],
        "event_emoji_pool": ["📌", "🗓️", "📋", "✅", "📣", "⏱️"],
        "milestone_emoji_pool": ["📌", "🗓️", "📋", "✅", "⏱️"],
        "milestone_templates": {
            "default": [
                "{emoji} Scheduled: **{event}** in **{days} days** ({time_left}) • **{date}**",
                "{emoji} Reminder — **{event}** occurs in **{days} days** ({time_left}) • **{date}**",
                "{emoji} Upcoming: **{event}** — **{days} days** remaining ({time_left}) • **{date}**",
                "{emoji} Notice: **{event}** in **{days} days** ({time_left}) • **{date}**",
                "{emoji} Calendar item: **{event}** in **{days} days** ({time_left}) • **{date}**",
            ],
            "one_day": [
                "{emoji} Reminder: **{event}** is **tomorrow** ({time_left}) • **{date}**",
                "{emoji} Notice — **{event}** tomorrow ({time_left}) • **{date}**",
                "{emoji} 1-day reminder: **{event}** is tomorrow ({time_left}) • **{date}**",
                "{emoji} Scheduled for tomorrow: **{event}** ({time_left}) • **{date}**",
                "{emoji} Tomorrow: **{event}** ({time_left}) • **{date}**",
            ],
            "zero_day": [
                "{emoji} Today: **{event}** ({time_left}) • **{date}**",
                "{emoji} Scheduled for today: **{event}** ({time_left}) • **{date}**",
                "{emoji} Notice — **{event}** is **today** ({time_left}) • **{date}**",
                "{emoji} Action today: **{event}** ({time_left}) • **{date}**",
                "{emoji} Today’s item: **{event}** ({time_left}) • **{date}**",
            ],
        },
        "repeat_templates": [
            "{emoji} 🔁 Recurring: **{event}** repeats in **{time_left}** (on **{date}**).",
            "{emoji} 🔁 Next occurrence: **{event}** in **{time_left}** • **{date}**",
            "{emoji} 🔁 Repeating item — **{event}** in **{time_left}** (on **{date}**).",
            "{emoji} 🔁 Scheduled again: **{event}** in **{time_left}** • **{date}**",
            "{emoji} 🔁 **{event}** cycles again in **{time_left}** • **{date}**",
        ],
        "remindall_templates": [
            "{emoji} Reminder: **{event}** in **{time_left}** (on **{date}**).",
            "{emoji} Upcoming: **{event}** in **{time_left}** • **{date}**",
            "{emoji} Calendar reminder — **{event}** in **{time_left}** • **{date}**",
            "{emoji} Scheduled item: **{event}** in **{time_left}** • **{date}**",
            "{emoji} Notice: **{event}** is **{time_left}** away • **{date}**",
        ],
        "start_blast_templates": [
            "⏰ **{event}** begins now.",
            "✅ Now starting: **{event}**.",
            "📣 **{event}** is live now.",
            "🗓️ **{event}** starts now.",
            "⏱️ Start time reached: **{event}**.",
        ],
    },

    "celebration": {
        "label": "Celebration",
        "supporter_only": True,
        "color": discord.Color.from_rgb(246, 201, 69),  # gold
        "pin_title_pool": [
            "🎉 Celebration Countdown",
            "🎊 Party Countdown Board",
            "🥳 Good Times Ahead",
            "✨ Big Moments Board",
            "🎈 Milestone Tracker",
        ],
        "event_emoji_pool": ["🎉", "🎊", "🥳", "✨", "🎈", "🍾", "🪩"],
        "milestone_emoji_pool": ["🎉", "🎊", "🥳", "✨", "🪩"],
        "milestone_templates": {
            "default": [
                "{emoji} Countdown! **{event}** in **{days} days** ({time_left}) • **{date}**",
                "{emoji} Hype check — **{event}** is **{days} days** away ({time_left}) • **{date}**",
                "{emoji} Party planning ping: **{event}** in **{days} days** ({time_left}) • **{date}**",
                "{emoji} Confetti pending: **{event}** — **{days} days** left ({time_left}) • **{date}**",
                "{emoji} Big moment incoming: **{event}** in **{days} days** ({time_left}) • **{date}**",
            ],
            "one_day": [
                "{emoji} Tomorrow we celebrate: **{event}** ({time_left}) • **{date}**",
                "{emoji} One day left — **{event}** is tomorrow ({time_left}) • **{date}**",
                "{emoji} Tomorrow’s the party: **{event}** ({time_left}) • **{date}**",
                "{emoji} Final countdown: **{event}** tomorrow ({time_left}) • **{date}**",
                "{emoji} Get ready — **{event}** is tomorrow ({time_left}) • **{date}**",
            ],
            "zero_day": [
                "{emoji} It’s celebration day: **{event}** is **today** ({time_left}) • **{date}**",
                "{emoji} TODAY: **{event}** ({time_left}) • **{date}**",
                "{emoji} Pop the confetti — **{event}** today ({time_left}) • **{date}**",
                "{emoji} It’s here! **{event}** is today ({time_left}) • **{date}**",
                "{emoji} Celebrate now: **{event}** is today ({time_left}) • **{date}**",
            ],
        },
        "repeat_templates": [
            "{emoji} 🔁 **{event}** repeats — next party in **{time_left}** (on **{date}**).",
            "{emoji} 🔁 Encore! **{event}** returns in **{time_left}** • **{date}**",
            "{emoji} 🔁 Next celebration cycle: **{event}** in **{time_left}** • **{date}**",
            "{emoji} 🔁 Rerun scheduled — **{event}** in **{time_left}** (on **{date}**).",
            "{emoji} 🔁 **{event}** cycles again in **{time_left}** • **{date}**",
        ],
        "remindall_templates": [
            "{emoji} Reminder: **{event}** in **{time_left}** (on **{date}**).",
            "{emoji} Party ping — **{event}** in **{time_left}** • **{date}**",
            "{emoji} Save the date: **{event}** in **{time_left}** • **{date}**",
            "{emoji} Countdown’s on: **{event}** in **{time_left}** • **{date}**",
            "{emoji} Big moment soon — **{event}** in **{time_left}** • **{date}**",
        ],
        "start_blast_templates": [
            "🎊 **{event}** starts now — make it loud!",
            "🥳 It’s here: **{event}** is live!",
            "🎉 Go time: **{event}** begins now!",
            "✨ Celebration mode: **{event}** starts now!",
            "🍾 Now: **{event}**!",
        ],
    },

    "romance": {
        "label": "Romance",
        "supporter_only": True,
        "color": discord.Color.from_rgb(225, 29, 72),  # rose
        "pin_title_pool": [
            "💞 Date Night Countdowns",
            "🌹 Romance Timeline",
            "💌 Love & Plans",
            "🕯️ Sweet Moments Board",
            "✨ Little Milestones",
        ],
        "event_emoji_pool": ["💞", "🌹", "💌", "🕯️", "✨", "💕", "🍷"],
        "milestone_emoji_pool": ["💞", "🌹", "💌", "🕯️", "✨"],
        "milestone_templates": {
            "default": [
                "{emoji} Soft reminder: **{event}** in **{days} days** ({time_left}) • **{date}**",
                "{emoji} Little note — **{event}** is **{days} days** away ({time_left}) • **{date}**",
                "{emoji} Hearts-up: **{event}** in **{days} days** ({time_left}) • **{date}**",
                "{emoji} Candlelight countdown: **{event}** — **{days} days** left ({time_left}) • **{date}**",
                "{emoji} Coming soon: **{event}** in **{days} days** ({time_left}) • **{date}**",
            ],
            "one_day": [
                "{emoji} Tomorrow: **{event}** ({time_left}) • **{date}**",
                "{emoji} One day left — **{event}** is tomorrow ({time_left}) • **{date}**",
                "{emoji} Tomorrow’s a sweet one: **{event}** ({time_left}) • **{date}**",
                "{emoji} Almost there — **{event}** tomorrow ({time_left}) • **{date}**",
                "{emoji} Tomorrow’s plan: **{event}** ({time_left}) • **{date}**",
            ],
            "zero_day": [
                "{emoji} Today: **{event}** ({time_left}) • **{date}**",
                "{emoji} It’s today — **{event}** ({time_left}) • **{date}**",
                "{emoji} The moment is here: **{event}** is today ({time_left}) • **{date}**",
                "{emoji} Today’s the date: **{event}** ({time_left}) • **{date}**",
                "{emoji} Love on the calendar: **{event}** today ({time_left}) • **{date}**",
            ],
        },
        "repeat_templates": [
            "{emoji} 🔁 **{event}** repeats — next date in **{time_left}** (on **{date}**).",
            "{emoji} 🔁 Again soon: **{event}** in **{time_left}** • **{date}**",
            "{emoji} 🔁 Recurring romance: **{event}** returns in **{time_left}** • **{date}**",
            "{emoji} 🔁 Next sweet moment: **{event}** in **{time_left}** (on **{date}**).",
            "{emoji} 🔁 **{event}** cycles again in **{time_left}** • **{date}**",
        ],
        "remindall_templates": [
            "{emoji} Reminder: **{event}** in **{time_left}** (on **{date}**).",
            "{emoji} Sweet ping — **{event}** in **{time_left}** • **{date}**",
            "{emoji} Little note: **{event}** is **{time_left}** away • **{date}**",
            "{emoji} Save the date: **{event}** in **{time_left}** • **{date}**",
            "{emoji} Hearts up — **{event}** in **{time_left}** • **{date}**",
        ],
        "start_blast_templates": [
            "💞 **{event}** starts now.",
            "🌹 It’s time: **{event}** begins now.",
            "💌 Now: **{event}**.",
            "🕯️ The moment arrives — **{event}** starts now.",
            "✨ Sweet timing — **{event}** is live now.",
        ],
    },

    "vacation": {
        "label": "Vacation",
        "supporter_only": True,
        "color": discord.Color.from_rgb(20, 184, 166),  # teal
        "pin_title_pool": [
            "🧳 Trip Countdown Board",
            "✈️ Departures & Dates",
            "🌴 Getaway Timeline",
            "🗺️ Travel Plans Board",
            "🧃 Vacation Scheduler",
        ],
        "event_emoji_pool": ["🧳", "✈️", "🌴", "🗺️", "🧃", "🏖️", "📸"],
        "milestone_emoji_pool": ["✈️", "🧳", "🌴", "🗺️", "🏖️"],
        "milestone_templates": {
            "default": [
                "{emoji} Packing list ping: **{event}** in **{days} days** ({time_left}) • **{date}**",
                "{emoji} Travel countdown — **{event}** is **{days} days** away ({time_left}) • **{date}**",
                "{emoji} Route check: **{event}** in **{days} days** ({time_left}) • **{date}**",
                "{emoji} Getaway approaching: **{event}** — **{days} days** left ({time_left}) • **{date}**",
                "{emoji} Almost escape time: **{event}** in **{days} days** ({time_left}) • **{date}**",
            ],
            "one_day": [
                "{emoji} Tomorrow: **{event}** — boarding soon ({time_left}) • **{date}**",
                "{emoji} One day left — **{event}** is tomorrow ({time_left}) • **{date}**",
                "{emoji} Tomorrow’s departure: **{event}** ({time_left}) • **{date}**",
                "{emoji} Final check — **{event}** tomorrow ({time_left}) • **{date}**",
                "{emoji} Almost vacation-real — **{event}** tomorrow ({time_left}) • **{date}**",
            ],
            "zero_day": [
                "{emoji} Departure day: **{event}** is **today** ({time_left}) • **{date}**",
                "{emoji} TODAY: **{event}** ({time_left}) • **{date}**",
                "{emoji} Boarding now (emotionally): **{event}** today ({time_left}) • **{date}**",
                "{emoji} Travel day — **{event}** is today ({time_left}) • **{date}**",
                "{emoji} The trip begins: **{event}** today ({time_left}) • **{date}**",
            ],
        },
        "repeat_templates": [
            "{emoji} 🔁 **{event}** repeats — next trip in **{time_left}** (on **{date}**).",
            "{emoji} 🔁 Again soon: **{event}** in **{time_left}** • **{date}**",
            "{emoji} 🔁 Next getaway cycle: **{event}** returns in **{time_left}** • **{date}**",
            "{emoji} 🔁 Recurring travel — **{event}** in **{time_left}** (on **{date}**).",
            "{emoji} 🔁 **{event}** cycles again in **{time_left}** • **{date}**",
        ],
        "remindall_templates": [
            "{emoji} Reminder: **{event}** in **{time_left}** (on **{date}**).",
            "{emoji} Travel ping — **{event}** in **{time_left}** • **{date}**",
            "{emoji} Don’t forget — **{event}** is **{time_left}** away • **{date}**",
            "{emoji} Packing reminder: **{event}** in **{time_left}** • **{date}**",
            "{emoji} Trip check: **{event}** in **{time_left}** • **{date}**",
        ],
        "start_blast_templates": [
            "✈️ **{event}** starts now — wheels up!",
            "🧳 Go time: **{event}** begins now!",
            "🌴 Vacation mode: **{event}** is live!",
            "🏖️ Now departing: **{event}**!",
            "🗺️ The journey begins — **{event}** starts now!",
        ],
    },

    "hype": {
        "label": "Hype Mode",
        "supporter_only": True,
        "color": discord.Color.from_rgb(255, 61, 127),  # hot pink
        "pin_title_pool": [
            "🚀 Hype Tracker",
            "🔥 Big Energy Board",
            "⚡ Incoming Moments",
            "🎉 Hype Countdown",
            "📣 All Eyes On This",
        ],
        "event_emoji_pool": ["🚀", "🔥", "⚡", "🎉", "📣", "💥", "🌟"],
        "milestone_emoji_pool": ["🔥", "🚀", "⚡", "💥", "🌟"],
        "milestone_templates": {
            "default": [
                "{emoji} Incoming: **{event}** in **{days} days** ({time_left}) • **{date}**",
                "{emoji} Hype check — **{event}** is **{days} days** away ({time_left}) • **{date}**",
                "{emoji} Energy build: **{event}** in **{days} days** ({time_left}) • **{date}**",
                "{emoji} Final approach: **{event}** — **{days} days** left ({time_left}) • **{date}**",
                "{emoji} We’re counting down: **{event}** in **{days} days** ({time_left}) • **{date}**",
            ],
            "one_day": [
                "{emoji} TOMORROW: **{event}** ({time_left}) • **{date}**",
                "{emoji} One day left — **{event}** is tomorrow ({time_left}) • **{date}**",
                "{emoji} Tomorrow we GO: **{event}** ({time_left}) • **{date}**",
                "{emoji} Last sleep — **{event}** tomorrow ({time_left}) • **{date}**",
                "{emoji} Almost here — **{event}** is tomorrow ({time_left}) • **{date}**",
            ],
            "zero_day": [
                "{emoji} TODAY: **{event}** ({time_left}) • **{date}**",
                "{emoji} It’s happening today: **{event}** ({time_left}) • **{date}**",
                "{emoji} We’re live today — **{event}** ({time_left}) • **{date}**",
                "{emoji} Zero hour: **{event}** is today ({time_left}) • **{date}**",
                "{emoji} No more waiting — **{event}** today ({time_left}) • **{date}**",
            ],
        },
        "repeat_templates": [
            "{emoji} 🔁 **{event}** repeats — next wave in **{time_left}** (on **{date}**).",
            "{emoji} 🔁 Again soon — **{event}** in **{time_left}** • **{date}**",
            "{emoji} 🔁 Rerun scheduled — **{event}** returns in **{time_left}** • **{date}**",
            "{emoji} 🔁 Next round: **{event}** in **{time_left}** (on **{date}**).",
            "{emoji} 🔁 **{event}** cycles again in **{time_left}** • **{date}**",
        ],
        "remindall_templates": [
            "{emoji} Reminder: **{event}** in **{time_left}** (on **{date}**).",
            "{emoji} Don’t miss it — **{event}** is **{time_left}** away • **{date}**",
            "{emoji} Countdown’s on: **{event}** in **{time_left}** • **{date}**",
            "{emoji} Hype ping — **{event}** in **{time_left}** • **{date}**",
            "{emoji} Eyes up: **{event}** in **{time_left}** • **{date}**",
        ],
        "start_blast_templates": [
            "💥 **{event}** starts NOW!",
            "🚀 Launch: **{event}** is live!",
            "🔥 GO GO GO — **{event}** begins now!",
            "⚡ It’s time: **{event}** starts now!",
            "🌟 Main character moment: **{event}** is happening now!",
        ],
    },

    "minimal": {
        "label": "Minimalistic",
        "supporter_only": True,
        "color": discord.Color.from_rgb(156, 163, 175),  # neutral gray
        "pin_title_pool": [
            "Upcoming Events",
            "Schedule",
            "Timeline",
            "Events",
            "Countdowns",
        ],
        "event_emoji_pool": ["▫️", "▪️", "◻️", "◽", "◇", "▸", "✧"],
        "milestone_emoji_pool": ["▫️", "▪️", "✧", "▸", "◇"],
        "milestone_templates": {
            "default": [
                "{emoji} **{event}** — **{days} days** ({time_left}) • **{date}**",
                "{emoji} **{event}** — **{days} days** remaining ({time_left}) • **{date}**",
                "{emoji} **{event}** in **{days} days** ({time_left}) • **{date}**",
                "{emoji} **{event}** — **{days} days** left ({time_left}) • **{date}**",
                "{emoji} **{event}** — **{days} days** • **{date}**",
            ],
            "one_day": [
                "{emoji} **{event}** — **tomorrow** ({time_left}) • **{date}**",
                "{emoji} **{event}** — tomorrow • **{date}**",
                "{emoji} **{event}** tomorrow ({time_left}) • **{date}**",
                "{emoji} **{event}** — 1 day left ({time_left}) • **{date}**",
                "{emoji} **{event}** — tomorrow • **{date}**",
            ],
            "zero_day": [
                "{emoji} **{event}** — **today** ({time_left}) • **{date}**",
                "{emoji} **{event}** — today • **{date}**",
                "{emoji} **{event}** today ({time_left}) • **{date}**",
                "{emoji} **{event}** — 0 days • **{date}**",
                "{emoji} **{event}** — today • **{date}**",
            ],
        },
        "repeat_templates": [
            "{emoji} 🔁 **{event}** — repeats in **{time_left}** (on **{date}**).",
            "{emoji} 🔁 **{event}** repeats in **{time_left}** • **{date}**",
            "{emoji} 🔁 Next **{event}** in **{time_left}** • **{date}**",
            "{emoji} 🔁 **{event}** cycles in **{time_left}** (on **{date}**).",
            "{emoji} 🔁 **{event}** — next in **{time_left}** • **{date}**",
        ],
        "remindall_templates": [
            "{emoji} **{event}** — **{time_left}** (on **{date}**).",
            "{emoji} **{event}** — **{time_left}** • **{date}**",
            "{emoji} Reminder: **{event}** in **{time_left}** • **{date}**",
            "{emoji} Upcoming: **{event}** in **{time_left}** • **{date}**",
            "{emoji} **{event}** — **{time_left}** • **{date}**",
        ],
        "start_blast_templates": [
            "⏱️ **{event}** starts now.",
            "• **{event}** is live.",
            "Now: **{event}**.",
            "**{event}** begins now.",
            "**{event}** — start.",
        ],
    },

    "school": {
        "label": "School",
        "supporter_only": True,
        "color": discord.Color.from_rgb(37, 99, 235),  # study blue
        "pin_title_pool": [
            "📚 Study & Deadlines",
            "📝 Syllabus Board",
            "📌 Due Dates",
            "⏳ Study Sprint Timer",
            "✅ Prep Checklist",
        ],
        "event_emoji_pool": ["📚", "📝", "📌", "⏳", "✅", "🧠", "📖"],
        "milestone_emoji_pool": ["📚", "📝", "📌", "✅", "⏳"],
        "milestone_templates": {
            "default": [
                "{emoji} Study ping: **{event}** in **{days} days** ({time_left}) • **{date}**",
                "{emoji} On the syllabus: **{event}** — **{days} days** left ({time_left}) • **{date}**",
                "{emoji} Prep reminder: **{event}** in **{days} days** ({time_left}) • **{date}**",
                "{emoji} Calendar check: **{event}** is **{days} days** away ({time_left}) • **{date}**",
                "{emoji} Keep pace: **{event}** in **{days} days** ({time_left}) • **{date}**",
            ],
            "one_day": [
                "{emoji} Tomorrow: **{event}** ({time_left}) • **{date}**",
                "{emoji} One day left — **{event}** is tomorrow ({time_left}) • **{date}**",
                "{emoji} Quick review time: **{event}** tomorrow ({time_left}) • **{date}**",
                "{emoji} Tomorrow’s deadline/session: **{event}** ({time_left}) • **{date}**",
                "{emoji} Final prep — **{event}** tomorrow ({time_left}) • **{date}**",
            ],
            "zero_day": [
                "{emoji} Today: **{event}** ({time_left}) • **{date}**",
                "{emoji} It’s today — **{event}** ({time_left}) • **{date}**",
                "{emoji} Show time: **{event}** today ({time_left}) • **{date}**",
                "{emoji} You’ve got this — **{event}** today ({time_left}) • **{date}**",
                "{emoji} Today on the schedule: **{event}** ({time_left}) • **{date}**",
            ],
        },
        "repeat_templates": [
            "{emoji} 🔁 **{event}** repeats — next session in **{time_left}** (on **{date}**).",
            "{emoji} 🔁 Recurring study block: **{event}** in **{time_left}** • **{date}**",
            "{emoji} 🔁 Next **{event}** in **{time_left}** • **{date}**",
            "{emoji} 🔁 Repeats again — **{event}** in **{time_left}** (on **{date}**).",
            "{emoji} 🔁 **{event}** cycles again in **{time_left}** • **{date}**",
        ],
        "remindall_templates": [
            "{emoji} Reminder: **{event}** in **{time_left}** (on **{date}**).",
            "{emoji} Study ping — **{event}** in **{time_left}** • **{date}**",
            "{emoji} Prep reminder: **{event}** is **{time_left}** away • **{date}**",
            "{emoji} Don’t cram last-minute — **{event}** in **{time_left}** • **{date}**",
            "{emoji} Calendar note: **{event}** in **{time_left}** • **{date}**",
        ],
        "start_blast_templates": [
            "📚 **{event}** starts now — focus time.",
            "📝 Now starting: **{event}**.",
            "✅ Go time: **{event}** begins now!",
            "⏳ Timer’s live — **{event}** starts now!",
            "🧠 Lock in: **{event}** is live now.",
        ],
    },

    "spooky": {
        "label": "Spooky",
        "supporter_only": True,
        "color": discord.Color.from_rgb(249, 115, 22),  # pumpkin orange
        "pin_title_pool": [
            "🕯️ Spooky Season Countdowns",
            "🎃 Haunted Countdown Board",
            "🔮 Witching Hour Timeline",
            "🕸️ Cobweb Calendar",
            "🦇 Midnight Schedule",
        ],
        "event_emoji_pool": ["🎃", "👻", "🕸️", "🕯️", "🦇", "🔮", "🧙‍♀️"],
        "milestone_emoji_pool": ["🕯️", "🎃", "👻", "🦇", "🔮"],
        "milestone_templates": {
            "default": [
                "{emoji} The clock creaks… **{event}** in **{days} days** ({time_left}) • **{date}**",
                "{emoji} Omen update: **{event}** — **{days} days** remain ({time_left}) • **{date}**",
                "{emoji} From the shadows: **{event}** in **{days} days** ({time_left}) • **{date}**",
                "{emoji} Nightfall approaches — **{event}** is **{days} days** away ({time_left}) • **{date}**",
                "{emoji} Tangled in time: **{event}** in **{days} days** ({time_left}) • **{date}**",
            ],
            "one_day": [
                "{emoji} One night away: **{event}** is **tomorrow** ({time_left}) • **{date}**",
                "{emoji} Tomorrow… **{event}** ({time_left}) • **{date}**",
                "{emoji} The veil thins tomorrow: **{event}** ({time_left}) • **{date}**",
                "{emoji} Almost midnight — **{event}** tomorrow ({time_left}) • **{date}**",
                "{emoji} Tomorrow’s haunting: **{event}** ({time_left}) • **{date}**",
            ],
            "zero_day": [
                "{emoji} The veil is thin: **{event}** is **today** ({time_left}) • **{date}**",
                "{emoji} Tonight’s the night: **{event}** begins **today** ({time_left}) • **{date}**",
                "{emoji} TODAY: **{event}** ({time_left}) • **{date}**",
                "{emoji} A chill in the air — **{event}** is today ({time_left}) • **{date}**",
                "{emoji} The hour arrives: **{event}** is today ({time_left}) • **{date}**",
            ],
        },
        "repeat_templates": [
            "{emoji} 🔁 **{event}** repeats — next haunting in **{time_left}** (on **{date}**).",
            "{emoji} 🔁 It returns… **{event}** in **{time_left}** • **{date}**",
            "{emoji} 🔁 Recurring omen: **{event}** in **{time_left}** (on **{date}**).",
            "{emoji} 🔁 Next creepy cycle: **{event}** returns in **{time_left}** • **{date}**",
            "{emoji} 🔁 **{event}** cycles again in **{time_left}** • **{date}**",
        ],
        "remindall_templates": [
            "{emoji} Reminder: **{event}** in **{time_left}** (on **{date}**).",
            "{emoji} From the shadows — **{event}** in **{time_left}** • **{date}**",
            "{emoji} Boo! **{event}** is **{time_left}** away • **{date}**",
            "{emoji} Cobweb calendar: **{event}** in **{time_left}** • **{date}**",
            "{emoji} Nightfall notice — **{event}** in **{time_left}** • **{date}**",
        ],
        "start_blast_templates": [
            "🕯️ **{event}** begins… now.",
            "👻 The moment arrives: **{event}** is live!",
            "🎃 It’s time: **{event}** starts now!",
            "🦇 Midnight strikes — **{event}** begins now!",
            "🔮 The omen unfolds: **{event}** starts now!",
        ],
    },
}

_THEME_LABELS: Dict[str, str] = {
    "classic": "Chrono Purple (default) 💜",
    "football": "Football 🏈",
    "basketball": "Basketball 🏀",
    "baseball": "Baseball ⚾",
    "raidnight": "Raid Night 🎮",
    "dnd": "D&D 🐉",
    "girly": "Cute Aesthetic 🎀",
    "workplace": "Workplace Ops 📌",
    "celebration": "Celebration 🎉",
    "romance": "Romance 💞",
    "vacation": "Vacation 🧳",
    "hype": "Hype Mode 🚀",
    "minimal": "Minimalist ▫️",
    "school": "School 📚",
    "spooky": "Spooky 🎃",
}

# ---- THEME FOOTER POOLS (must come AFTER THEMES is defined) ----
FOOTER_POOLS: Dict[str, List[str]] = {
    "classic": [
        "💜 Chrono Purple • /chronohelp",
        "⏳ ChronoBot • Time is fake. Reminders are real.",
        "✨ ChronoBot • Keeping your chaos on a schedule.",
        "🕒 ChronoBot • One timeline to rule them all.",
        "🗳️ Supporter themes unlock with /vote",
        "📌 Tip: Use /theme anytime to swap vibes.",
    ],
    "football": [
        "🏈 Game Day • No timeouts on time.",
        "⏱️ Play Clock • Counting down to kickoff.",
        "📣 Sideline Report • /chronohelp for commands",
        "🔥 Huddle Up • Big plays need good planning.",
        "🗳️ Supporter theme • Unlock more with /vote",
        "🏟️ Stadium Mode • Keep your schedule in-bounds.",
    ],
    "basketball": [
        "🏀 Tip-Off • The shot clock is always running.",
        "⏱️ Shot Clock • Scheduling like a pro.",
        "🔥 Clutch Time • Don’t leave it to overtime.",
        "📣 Courtside • /chronohelp for commands",
        "🗳️ Supporter theme • Unlock more with /vote",
        "🏟️ Arena Lights • Next up on the board…",
    ],
    "baseball": [
        "⚾ On Deck • First pitch is coming.",
        "🧢 Dugout Notes • Keep your dates in the lineup.",
        "🏟️ Ballpark Board • /chronohelp for commands",
        "🔥 Extra Innings • Planning beats panic.",
        "🗳️ Supporter theme • Unlock more with /vote",
        "🧤 Diamond Time • Don’t get caught off-base.",
    ],
    "raidnight": [
        "🎮 Raid Night • Ready check in progress.",
        "🛡️ Party Finder • Don’t be late to the pull.",
        "⚔️ Pull Timer • We go when the timer hits zero.",
        "🧩 Objective HUD • /chronohelp for commands",
        "🗳️ Supporter theme • Unlock more with /vote",
        "🏆 Loot Council • Timers > excuses.",
    ],
    "dnd": [
        "🎲 Campaign Night • Roll initiative… later.",
        "🐉 DM Notes • Respect the schedule, fear the dragon.",
        "📜 The Next Chapter • /chronohelp for commands",
        "🕯️ Tavern Board • Arrive on time, get inspiration.",
        "🗳️ Supporter theme • Unlock more with /vote",
        "🗺️ Quest Log • Side quests welcome. Missed sessions? Not so much.",
    ],
    "girly": [
        "🎀 Cute Aesthetic • Tiny plans, big sparkle.",
        "💖 Soft Schedule • Your calendar, but make it cute.",
        "✨ Pretty Timing • /chronohelp for commands",
        "🌸 Sweet Reminder • Future-you says thank you.",
        "🗳️ Supporter theme • Unlock more with /vote",
        "🫧 Sparkle Mode • Countdowns with character.",
    ],
    "workplace": [
        "📌 Workplace Ops • Clear dates, clean execution.",
        "🗓️ Operations Board • /chronohelp for commands",
        "✅ Action Items • Planning beats firefighting.",
        "📋 Timeline View • Keep the machine humming.",
        "🗳️ Supporter theme • Unlock more with /vote",
        "⏱️ On Schedule • Meetings don’t wait.",
    ],
    "celebration": [
        "🎉 Celebration • Confetti pending…",
        "🎊 Party Board • Don’t forget the good stuff.",
        "🥳 Good Times Ahead • /chronohelp for commands",
        "🍾 Pop Soon • The countdown is part of the fun.",
        "🗳️ Supporter theme • Unlock more with /vote",
        "✨ Big Moment • Make it legendary.",
    ],
    "romance": [
        "💞 Romance • Soft plans, strong intentions.",
        "🌹 Date Night • /chronohelp for commands",
        "💌 Love Notes • Keep the magic on the calendar.",
        "🕯️ Candlelight Mode • Timing is part of the spell.",
        "🗳️ Supporter theme • Unlock more with /vote",
        "🍷 Sweet Timing • Don’t be late to your own moment.",
    ],
    "vacation": [
        "🧳 Vacation • Out of office (emotionally).",
        "✈️ Departures • /chronohelp for commands",
        "🌴 Getaway Mode • Countdown to freedom.",
        "🗺️ Travel Board • Future-you is already packing.",
        "🗳️ Supporter theme • Unlock more with /vote",
        "🏖️ Beach Brain • The trip starts when you plan it.",
    ],
    "hype": [
        "🚀 Hype Mode • Main character scheduling.",
        "🔥 Big Energy • /chronohelp for commands",
        "⚡ Incoming • Don’t blink — it’s soon.",
        "🎉 Countdown Heat • We love a dramatic timer.",
        "🗳️ Supporter theme • Unlock more with /vote",
        "💥 Let’s Go • Future you is screaming.",
    ],
    "minimal": [
        "• Minimal • /chronohelp",
        "⏱️ Simple timers. Clean schedule.",
        "▫️ Less clutter. More clarity.",
        "• Planning > panic.",
        "🗳️ Supporter theme • Unlock more with /vote",
        "• ChronoBot • Quietly keeping time.",
    ],
    "school": [
        "📚 School • Study now, celebrate later.",
        "📝 Syllabus Mode • /chronohelp for commands",
        "✅ Prep Checklist • Due dates don’t negotiate.",
        "🧠 Focus Time • Small steps, big grades.",
        "🗳️ Supporter theme • Unlock more with /vote",
        "⏳ Deadline Energy • Start early, finish calm.",
    ],
    "spooky": [
        "🎃 Spooky • The clock creaks… closer.",
        "🕯️ Witching Hour • /chronohelp for commands",
        "🕸️ Cobweb Calendar • Don’t get caught in the delay.",
        "👻 Haunted Schedule • Time is… watching.",
        "🗳️ Supporter theme • Unlock more with /vote",
        "🦇 Midnight Mode • The countdown stirs.",
    ],
}

for theme_id, pool in FOOTER_POOLS.items():
    if theme_id in THEMES:
        THEMES[theme_id]["footer_pool"] = pool

def normalize_theme_key(raw: Optional[str]) -> str:
    t = (raw or DEFAULT_THEME_ID).strip().lower()
    t = re.sub(r"[^a-z0-9_\-]", "", t)
    return THEME_ALIASES.get(t, t)

def _stable_pick(pool: List[str], seed: str) -> str:
    if not pool:
        return ""
    h = hashlib.blake2b(seed.encode("utf-8"), digest_size=8).hexdigest()
    idx = int(h, 16) % len(pool)
    return pool[idx]

def get_theme_profile(guild_state: dict) -> Tuple[str, Dict[str, Any]]:
    theme_id = normalize_theme_key(guild_state.get("theme"))
    profile = THEMES.get(theme_id) or THEMES[DEFAULT_THEME_ID]
    return theme_id if theme_id in THEMES else DEFAULT_THEME_ID, profile

def pick_event_emoji(theme_id: str, profile: Dict[str, Any], *, seed: str) -> str:
    pool = profile.get("event_emoji_pool") or THEMES[DEFAULT_THEME_ID]["event_emoji_pool"]
    return _stable_pick(pool, f"{theme_id}|{seed}")

def pick_title(theme_id: str, profile: Dict[str, Any], *, seed: str) -> str:
    pool = profile.get("pin_title_pool") or THEMES[DEFAULT_THEME_ID]["pin_title_pool"]
    return _stable_pick(pool, f"{theme_id}|title|{seed}")

def pick_milestone_emoji(profile: Dict[str, Any]) -> str:
    pool = profile.get("milestone_emoji_pool") or THEMES[DEFAULT_THEME_ID]["milestone_emoji_pool"]
    return random.choice(pool) if pool else "⏳"

def pick_template(profile: Dict[str, Any], key: str, fallback_key: str = "default") -> str:
    bucket = profile.get("milestone_templates", {})
    pool = bucket.get(key) or bucket.get(fallback_key) or THEMES[DEFAULT_THEME_ID]["milestone_templates"]["default"]
    return random.choice(pool) if pool else "{emoji} **{event}**"

def build_milestone_message(guild_state: dict, *, event_name: str, days_left: int, time_left: str, date_str: str) -> str:
    theme_id, profile = get_theme_profile(guild_state)
    emoji = pick_milestone_emoji(profile)
    key = "zero_day" if days_left == 0 else ("one_day" if days_left == 1 else "default")
    template = pick_template(profile, key)
    return template.format(emoji=emoji, event=event_name, days=days_left, time_left=time_left, date=date_str)

def build_repeat_message(guild_state: dict, *, event_name: str, time_left: str, date_str: str) -> str:
    _, profile = get_theme_profile(guild_state)
    emoji = pick_milestone_emoji(profile)
    pool = profile.get("repeat_templates") or THEMES[DEFAULT_THEME_ID]["repeat_templates"]
    template = random.choice(pool) if pool else "{emoji} 🔁 **{event}** repeats — next up in **{time_left}** (on **{date}**)."
    return template.format(emoji=emoji, event=event_name, time_left=time_left, date=date_str)

def build_remindall_message(guild_state: dict, *, event_name: str, time_left: str, date_str: str) -> str:
    _, profile = get_theme_profile(guild_state)
    emoji = pick_milestone_emoji(profile)
    pool = profile.get("remindall_templates") or THEMES[DEFAULT_THEME_ID]["remindall_templates"]
    template = random.choice(pool) if pool else "{emoji} Reminder: **{event}** is in **{time_left}** (on **{date}**)."
    return template.format(emoji=emoji, event=event_name, time_left=time_left, date=date_str)

def build_start_blast_message(guild_state: dict, *, event_name: str) -> str:
    _, profile = get_theme_profile(guild_state)
    pool = profile.get("start_blast_templates") or THEMES[DEFAULT_THEME_ID]["start_blast_templates"]
    template = random.choice(pool) if pool else "⏰ **{event}** is happening now!"
    return template.format(event=event_name)

# ==========================
# UNIFIED THEME VISUAL LAYOUTS
# ==========================

THEME_LAYOUTS = {
    "classic": {
        "title": "⏳ Chrono Countdown Board",
        "subtitle": "Timelines woven in Chrono purple.",
        "footer": "Updated every minute • Time is fake, reminders are real",
        "color": discord.Color.from_rgb(140, 82, 255),
        "emoji": "🕒",
    },
    "football": {
        "title": "🏈 Game Day Countdown Board",
        "subtitle": "Next kickoffs on the schedule.",
        "footer": "Updated every minute • Timeouts are imaginary",
        "color": discord.Color.from_rgb(31, 139, 76),
        "emoji": "🏟️",
    },
    "basketball": {
        "title": "🏀 Tip-Off Countdown",
        "subtitle": "Next tip-offs and matchups.",
        "footer": "Updated every minute • Keep your head in the game",
        "color": discord.Color.from_rgb(242, 140, 40),
        "emoji": "🔥",
    },
    "baseball": {
        "title": "⚾ Diamond Dateboard",
        "subtitle": "Upcoming first pitches and innings.",
        "footer": "Updated every minute • No rain delays for time",
        "color": discord.Color.from_rgb(11, 31, 91),
        "emoji": "🧢",
    },
    "raidnight": {
        "title": "⚔️ Raid Night Queue",
        "subtitle": "Ready checks and pull timers ahead.",
        "footer": "Updated every minute • Wipes build character",
        "color": discord.Color.from_rgb(155, 93, 229),
        "emoji": "🛡️",
    },
    "dnd": {
        "title": "🎲 Campaign Night Ledger",
        "subtitle": "When the party gathers again.",
        "footer": "Updated every minute • Roll initiative for punctuality",
        "color": discord.Color.from_rgb(139, 94, 52),
        "emoji": "🕯️",
    },
    "girly": {
        "title": "🎀 Pretty Plans Countdown",
        "subtitle": "Cute vibes, perfectly timed.",
        "footer": "Updated every minute • Sparkles optional",
        "color": discord.Color.from_rgb(255, 93, 162),
        "emoji": "💖",
    },
    "workplace": {
        "title": "📋 Operations Schedule",
        "subtitle": "Upcoming key dates and deliverables.",
        "footer": "Updated every minute • Meetings don’t wait",
        "color": discord.Color.from_rgb(75, 85, 99),
        "emoji": "📌",
    },
    "celebration": {
        "title": "🎉 Celebration Countdown",
        "subtitle": "Big milestones and bright moments ahead.",
        "footer": "Updated every minute • Confetti pending",
        "color": discord.Color.from_rgb(246, 201, 69),
        "emoji": "🎊",
    },
    "romance": {
        "title": "💞 Date Night Countdown",
        "subtitle": "Soft plans and sweet intentions.",
        "footer": "Updated every minute • Timing is everything",
        "color": discord.Color.from_rgb(225, 29, 72),
        "emoji": "🌹",
    },
    "vacation": {
        "title": "🌴 Vacation Countdown Board",
        "subtitle": "Getaway vibes incoming.",
        "footer": "Updated every minute • Bags packed mentally",
        "color": discord.Color.from_rgb(20, 184, 166),
        "emoji": "🧳",
    },
    "hype": {
        "title": "🚀 Hype Tracker",
        "subtitle": "Big energy and countdown chaos.",
        "footer": "Updated every minute • Main character timing",
        "color": discord.Color.from_rgb(255, 61, 127),
        "emoji": "⚡",
    },
    "minimal": {
        "title": "▫️ Countdown Board",
        "subtitle": "Neat, clean, and tidy timelines.",
        "footer": "Updated every minute • Simplicity wins",
        "color": discord.Color.from_rgb(156, 163, 175),
        "emoji": "▫️",
    },
    "school": {
        "title": "📚 Study & Deadlines Board",
        "subtitle": "Assignments, exams, and focus sessions.",
        "footer": "Updated every minute • Start early, stress less",
        "color": discord.Color.from_rgb(37, 99, 235),
        "emoji": "📝",
    },
    "spooky": {
        "title": "🎃 Spooky Season Countdowns",
        "subtitle": "The clock creaks… the event approaches.",
        "footer": "Updated every minute • The vibes are haunted",
        "color": discord.Color.from_rgb(249, 115, 22),
        "emoji": "🕯️",
    },
}

def get_theme_layout(guild_state: dict, theme_id: Optional[str] = None) -> dict:
    tid = (theme_id or guild_state.get("theme") or "classic")
    tid = str(tid).lower()

    layout = THEME_LAYOUTS.get(tid, THEME_LAYOUTS["classic"]).copy()

    # Guarantee required keys exist
    layout.setdefault("title", "Event Countdown")
    layout.setdefault("subtitle", "")
    layout.setdefault("footer", "")
    layout.setdefault("emoji", "🕒")
    layout.setdefault("color", EMBED_COLOR)

    return layout

def format_event_dt(dt: datetime) -> str:
    # Example: January 5, 2026 • 8:30 PM CST
    date_part = dt.strftime("%B %d, %Y")
    time_part = dt.strftime("%I:%M %p").lstrip("0")  # removes leading 0
    tz_part = dt.strftime("%Z")
    if tz_part:
        return f"{date_part} • {time_part} {tz_part}"
    return f"{date_part} • {time_part}"

def compute_dhm(target: datetime, now: datetime) -> tuple[int, int, int, bool]:
    delta_seconds = int((target - now).total_seconds())
    passed = delta_seconds <= 0
    if passed:
        delta_seconds = abs(delta_seconds)

    days = delta_seconds // 86400
    hours = (delta_seconds % 86400) // 3600
    minutes = (delta_seconds % 3600) // 60
    return days, hours, minutes, passed


# ==========================
# EMBED RENDERING
# ==========================

def build_embed_for_guild(guild_state: dict) -> discord.Embed:
    layout = get_theme_layout(guild_state) or {}

    # Harden events
    events = guild_state.get("events", [])
    if not isinstance(events, list):
        events = []
    events = list(events)  # shallow copy of list

    now = datetime.now(DEFAULT_TZ)

    # Sort by timestamp so "next upcoming" logic is true
    def _ts(ev):
        try:
            return float(ev.get("timestamp", 0))
        except Exception:
            return 0.0
    events.sort(key=_ts)

    override_title = (guild_state.get("countdown_title_override") or "").strip()
    embed_title = override_title[:256] if override_title else (layout.get("title") or "Event Countdown")[:256]

    embed = discord.Embed(
        title=embed_title,
        color=layout.get("color", discord.Color.from_rgb(140, 82, 255)),  # safe default
    )

    emoji = layout.get("emoji", "🕒")

    # Theme-provided subtitle/heading (fallback)
    theme_subtitle = layout.get("subtitle", layout.get("description", "📅 Upcoming events:"))

    # NEW: Supporter custom intro shown above the list
    custom_intro = (guild_state.get("countdown_description_override") or "").strip()

    # Build the header area (intro first, then the theme subtitle)
    header_lines = []
    if custom_intro:
        header_lines.append(custom_intro)
    if theme_subtitle:
        header_lines.append(theme_subtitle)

    header = "\n".join(header_lines).strip() or "📅 Upcoming events:"
    footer = layout.get("footer", "")

    blocks = []
    banner_url = None

    for ev in events:
        try:
            dt = datetime.fromtimestamp(float(ev["timestamp"]), tz=DEFAULT_TZ)
        except Exception:
            continue

        delta = dt - now
        if delta.total_seconds() < 0:
            continue

        # capture banner for the *next upcoming* event that has one
        if banner_url is None:
            u = ev.get("banner_url")
            if isinstance(u, str) and u.strip():
                banner_url = u.strip()

        days = delta.days
        hours = (delta.seconds // 3600)
        minutes = (delta.seconds % 3600) // 60

        name = str(ev.get("name", "Untitled Event"))[:256]  # avoid absurdly long names

        lines = [
            f"{emoji} {name}",
            f"🕒 {days} days • {hours} hours • {minutes} minutes remaining",
            f"📅 {dt.strftime('%B %d, %Y • %I:%M %p %Z')}",
        ]

        owner_id = ev.get("owner_user_id")
        owner_name = ev.get("owner_name")

        # allow int-like strings too
        if isinstance(owner_id, str) and owner_id.isdigit():
            owner_id = int(owner_id)

        if isinstance(owner_id, int) and owner_id > 0:
            lines.append(f"👤 Hosted by <@{owner_id}>")
        elif isinstance(owner_name, str) and owner_name.strip():
            lines.append(f"👤 Hosted by {owner_name.strip()}")

        blocks.append("\n".join(lines))

        # optional: cap how many you show to avoid giant embeds
        if len(blocks) >= 10:
            break

    body = f"{header}\n\n" + ("\n\n".join(blocks) if blocks else "_No upcoming events yet._")

    # enforce Discord embed description limit
    if len(body) > 4096:
        body = body[:4093] + "..."

    embed.description = body

    if footer:
        embed.set_footer(text=footer[:2048])

    if banner_url:
        embed.set_image(url=banner_url)

    return embed


async def rebuild_pinned_message(guild_id: int, channel: discord.TextChannel, guild_state: dict):
    sort_events(guild_state)

    old_id = guild_state.get("pinned_message_id")
    if old_id:
        try:
            old_msg = await channel.fetch_message(int(old_id))
            try:
                await old_msg.unpin()
            except discord.Forbidden:
                # Optional: alert owner you can't unpin (not critical)
                pass
        except (discord.NotFound, discord.HTTPException, discord.Forbidden):
            pass

    embed = build_embed_for_guild(guild_state)

    try:
        msg = await channel.send(embed=embed)
    except discord.Forbidden:
        missing = missing_channel_perms(channel, channel.guild)
        await notify_owner_missing_perms(
            channel.guild,
            channel,
            missing=missing,
            action="send the countdown message",
        )
        return None
    except discord.HTTPException:
        return None

    # ✅ Single authority: ensure_countdown_pinned handles pin-or-owner-DM
    try:
        bot_member = await get_bot_member(channel.guild)
        perms = channel.permissions_for(bot_member) if bot_member else None
        await ensure_countdown_pinned(channel.guild, channel, msg, perms=perms)
    except Exception:
        # ensure_countdown_pinned should ideally swallow its own errors,
        # but this keeps rebuild_pinned_message from ever crashing.
        pass

    guild_state["pinned_message_id"] = msg.id
    save_state()
    return msg

async def get_or_create_pinned_message(
    guild_id: int,
    channel: discord.TextChannel,
    *,
    allow_create: bool = False,
):
    guild_state = get_guild_state(guild_id)
    sort_events(guild_state)
    pinned_id = guild_state.get("pinned_message_id")

    bot_member = await get_bot_member(channel.guild)
    if bot_member is None:
        await notify_owner_missing_perms(
            channel.guild,
            channel,
            missing=list(RECOMMENDED_CHANNEL_PERMS),
            action="update the countdown message",
        )
        return None

    perms = channel.permissions_for(bot_member)

    if not perms.view_channel or not perms.send_messages:
        missing = missing_channel_perms(channel, channel.guild)
        await notify_owner_missing_perms(
            channel.guild,
            channel,
            missing=missing,
            action="access the event channel to send/update the countdown",
        )
        return None

    # If creating, we can still create WITHOUT read_message_history.
    # We only need manage_messages to pin/unpin, embed_links to display nicely.
    if allow_create:
        needed = []
        if not perms.embed_links:
            needed.append("embed_links")
        if not perms.manage_messages:
            needed.append("manage_messages")
        if needed:
            await notify_owner_missing_perms(
                channel.guild,
                channel,
                missing=needed,
                action="pin + display the countdown embed",
            )
        # IMPORTANT: do NOT return here. Missing perms may degrade features,
        # but we can still try to send/update.

    # -------------------------
    # 1) If we have a saved pinned ID:
    # -------------------------
    if pinned_id:
        # ✅ If we can't read history, we can still edit by ID using a PartialMessage
        if not perms.read_message_history:
            try:
                return channel.get_partial_message(int(pinned_id))
            except Exception:
                return None

        try:
            msg = await channel.fetch_message(int(pinned_id))
            await ensure_countdown_pinned(channel.guild, channel, msg, perms=perms)
            return msg
        except discord.NotFound:
            guild_state["pinned_message_id"] = None
            save_state()
            pinned_id = None
        except discord.Forbidden:
            missing = missing_channel_perms(channel, channel.guild)
            await notify_owner_missing_perms(
                channel.guild,
                channel,
                missing=missing,
                action="access the pinned countdown message",
            )
            return None
        except discord.HTTPException:
            return None

    # -------------------------
    # 2) Recovery (ONLY if we can read history)
    # -------------------------
    if not pinned_id and perms.read_message_history:
        try:
            pins = await channel.pins()
            bot_pins = [m for m in pins if m.author and m.author.id == bot_member.id]
            if bot_pins:
                m = max(bot_pins, key=lambda x: x.created_at)
                guild_state["pinned_message_id"] = m.id
                save_state()
                await ensure_countdown_pinned(channel.guild, channel, m, perms=perms)
                return m
        except discord.Forbidden:
            missing = missing_channel_perms(channel, channel.guild)
            await notify_owner_missing_perms(
                channel.guild,
                channel,
                missing=missing,
                action="access pinned messages to reuse the existing countdown pin",
            )
            # Fall through to create if allowed
        except discord.HTTPException:
            # Fall through to create if allowed
            pass

    # -------------------------
    # 3) Create (even if we can't read history)
    # -------------------------
    if not allow_create:
        return None

    embed = build_embed_for_guild(guild_state)
    try:
        msg = await channel.send(embed=embed)
        await ensure_countdown_pinned(channel.guild, channel, msg, perms=perms)

        # Cleanup old bot pins only if we can read history AND manage pins
        if perms.manage_messages and perms.read_message_history:
            try:
                pins = await channel.pins()
                for m in pins:
                    if m.id != msg.id and m.author and m.author.id == bot_member.id:
                        try:
                            await m.unpin(reason="Cleaning up older ChronoBot pins")
                        except (discord.Forbidden, discord.HTTPException):
                            pass
            except (discord.Forbidden, discord.HTTPException):
                pass

    except discord.Forbidden:
        missing = missing_channel_perms(channel, channel.guild)
        await notify_owner_missing_perms(
            channel.guild,
            channel,
            missing=missing,
            action="send the countdown message",
        )
        return None
    except discord.HTTPException:
        return None

    guild_state["pinned_message_id"] = msg.id
    save_state()
    return msg


async def get_text_channel(channel_id) -> Optional[discord.TextChannel]:
    try:
        cid = int(channel_id)
    except (TypeError, ValueError):
        return None

    ch = bot.get_channel(cid)
    if isinstance(ch, discord.TextChannel):
        return ch
    try:
        ch = await bot.fetch_channel(cid)
        return ch if isinstance(ch, discord.TextChannel) else None
    except Exception:
        return None

        
def format_created_by_inline(ev: dict) -> str:
    name = ev.get("created_by_name")
    if isinstance(name, str) and name.strip():
        return f"📝 Created by: {name.strip()}"
    # Back-compat fallback (older events)
    name2 = ev.get("owner_name")
    if isinstance(name2, str) and name2.strip():
        return f"📝 Created by: {name2.strip()}"
    return ""

def format_owner_inline(ev: dict) -> str:
    """
    Non-pinging owner label for lists/embeds.
    Uses cached owner_name when available.
    """
    owner_name = ev.get("owner_name")
    if isinstance(owner_name, str) and owner_name.strip():
        return f"👤 Owner: {owner_name.strip()}"
    return ""


async def ensure_owner_name_cached(guild: discord.Guild, ev: dict) -> bool:
    """
    Populate ev['owner_name'] once (only if missing) using guild member display name.
    Returns True if the event dict was updated.
    """
    owner_id = ev.get("owner_user_id")
    if not isinstance(owner_id, int) or owner_id <= 0:
        # keep it clean if owner removed
        if ev.get("owner_name") is not None:
            ev["owner_name"] = None
            return True
        return False

    existing = ev.get("owner_name")
    if isinstance(existing, str) and existing.strip():
        return False  # already cached

    member = guild.get_member(owner_id)
    if member is None:
        try:
            member = await guild.fetch_member(owner_id)
        except Exception:
            member = None

    if member is not None:
        ev["owner_name"] = member.display_name
        return True

    # Last-ditch: user object (no nickname, but better than nothing)
    try:
        u = await bot.fetch_user(owner_id)
        ev["owner_name"] = getattr(u, "name", None)
        return True
    except Exception:
        return False

async def dm_owner_if_set(guild: discord.Guild, ev: dict, message: str):
    owner_id = ev.get("owner_user_id")
    if not owner_id:
        return
    try:
        user = guild.get_member(owner_id) or await bot.fetch_user(owner_id)
        if user:
            await user.send(message)
    except discord.Forbidden:
        pass
    except Exception:
        pass


def get_event_by_index(guild_state: dict, index: int) -> Optional[dict]:
    sort_events(guild_state)
    events = guild_state.get("events", [])
    if index < 1 or index > len(events):
        return None
    return events[index - 1]


def build_milestone_mention(channel: discord.TextChannel, guild_state: dict) -> Tuple[str, discord.AllowedMentions]:
    role_id = guild_state.get("mention_role_id")
    if role_id:
        role = channel.guild.get_role(int(role_id))
        if role:
            if getattr(role, "is_default", lambda: False)():
                return "", discord.AllowedMentions.none()                
            return f"{role.mention} ", discord.AllowedMentions(roles=True)
    return "", discord.AllowedMentions.none()

def build_everyone_mention() -> Tuple[str, discord.AllowedMentions]:
    return "@everyone ", discord.AllowedMentions(everyone=True)

async def refresh_countdown_message(guild: discord.Guild, guild_state: dict) -> None:
    ch_id = guild_state.get("event_channel_id")
    if not ch_id:
        return

    channel = await get_text_channel(int(ch_id))
    if channel is None:
        return

    pinned = await get_or_create_pinned_message(guild.id, channel, allow_create=True)
    if pinned is None:
        return

    try:
        await pinned.edit(embed=build_embed_for_guild(guild_state))
    except discord.NotFound:
        gs = get_guild_state(guild.id)
        if gs.get("pinned_message_id") == pinned.id:
            gs["pinned_message_id"] = None
            save_state()
    except discord.Forbidden:
        missing = missing_channel_perms(channel, channel.guild)
        await notify_owner_missing_perms(
            channel.guild,
            channel,
            missing=missing,
            action="edit/update the pinned countdown message",
        )
    except discord.HTTPException as e:
        print(f"[Guild {guild.id}] Failed to edit pinned message: {e}")

# ==========================
# AUTOCOMPLETE HELPERS
# ==========================
async def event_index_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[int]]:
    """Autocomplete for any command `index` param: shows '1. Name — mm/dd/yyyy hh:mm'."""
    guild = interaction.guild
    if guild is None:
        return []

    g = get_guild_state(guild.id)
    sort_events(g)

    now = datetime.now(DEFAULT_TZ)
    cur = (current or "").strip().lower()
    grace = timedelta(seconds=EVENT_START_GRACE_SECONDS)

    choices: List[app_commands.Choice[int]] = []

    for idx, ev in enumerate(g.get("events", []), start=1):
        ts = ev.get("timestamp")
        if not isinstance(ts, (int, float)):
            continue

        try:
            dt = datetime.fromtimestamp(ts, tz=DEFAULT_TZ)
        except Exception:
            continue

        # Hide events that are effectively "past" (including grace window)
        if dt + grace <= now:
            continue

        name = ev.get("name") or "Event"
        label = f"{idx}. {name} — {dt.strftime('%m/%d/%Y %H:%M')}"
        label_l = label.lower()
        name_l = name.lower()

        if cur:
            if cur.isdigit():
                if not str(idx).startswith(cur) and cur not in name_l:
                    continue
            else:
                if cur not in name_l and cur not in label_l:
                    continue

        choices.append(app_commands.Choice(name=label[:100], value=idx))
        if len(choices) >= 25:
            break

    return choices

# ==========================
# BACKGROUND LOOP
# ==========================
@tasks.loop(minutes=15)
async def weekly_digest_loop():
    now = datetime.now(DEFAULT_TZ)

    # Send once each Monday any time after 9:00 AM local time.
    if now.weekday() != 0:  # Monday = 0
        return
    if now.hour < 9:
        return

    today_str = now.date().isoformat()
    now_ts = int(now.timestamp())
    cutoff_ts = now_ts + (7 * 86400)

    for gid_str, guild_state in list(state.get("guilds", {}).items()):
        try:
            d = guild_state.get("digest")
            if not isinstance(d, dict) or not d.get("enabled"):
                continue
            if d.get("last_sent_date") == today_str:
                continue

            ch_id = d.get("channel_id") or guild_state.get("event_channel_id")
            if not ch_id:
                continue

            channel = await get_text_channel(int(ch_id))
            if channel is None:
                continue

            sort_events(guild_state)

            upcoming = []
            for ev in guild_state.get("events", []):
                ts = ev.get("timestamp")
                if isinstance(ts, int) and now_ts < ts <= cutoff_ts:
                    dt = datetime.fromtimestamp(ts, tz=DEFAULT_TZ)
                    desc, _, _ = compute_time_left(now, dt)
                    upcoming.append(
                        f"• **{ev.get('name', 'Event')}** — {dt.strftime('%m/%d %I:%M %p')} ({desc})"
                    )

            text = "📬 **Weekly Digest (Next 7 days)**\n"
            text += "\n".join(upcoming[:15]) if upcoming else "No events in the next 7 days."

            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())

            d["last_sent_date"] = today_str
            guild_state["digest"] = d
            save_state()

        except Exception as e:
            print(f"[Digest] guild {gid_str} failed: {type(e).__name__}: {e}")
            continue


@weekly_digest_loop.before_loop
async def before_weekly_digest_loop():
    await bot.wait_until_ready()

@tasks.loop(seconds=UPDATE_INTERVAL_SECONDS)
async def update_countdowns():
    guilds = state.get("guilds", {})
    for gid_str, guild_state in list(guilds.items()):
        try:
            guild_id = int(gid_str)
            sort_events(guild_state)

            channel_id = guild_state.get("event_channel_id")
            if not channel_id:
                continue

            channel = await get_text_channel(channel_id)
            if channel is None:
                continue

            bot_member = await get_bot_member(channel.guild)
            if bot_member is None:
                continue

            # ----------------------------
            # ✅ Dirty flag + flush helpers
            # ----------------------------
            state_changed = False

            def mark_dirty():
                nonlocal state_changed
                state_changed = True

            def flush_if_dirty():
                nonlocal state_changed
                if state_changed:
                    save_state()
                    state_changed = False

            # ---- EVENT CHECKS (start blast + milestones + repeats) ----
            today = _today_local_date()
            now = datetime.now(DEFAULT_TZ)
            now_dt = now
            for ev in list(guild_state.get("events", [])):
                if ev.get("silenced", False):
                    continue

                ts = ev.get("timestamp")
                if not isinstance(ts, (int, float)):
                    continue

                try:
                    dt = datetime.fromtimestamp(ts, tz=DEFAULT_TZ)
                except Exception:
                    continue
                # ----------------------------
                # ✅ Reminder tracking defaults + post-event cleanup (24h)
                # ----------------------------
                ev.setdefault("reminder_messages", [])      # [{channel_id, message_id}, ...]
                ev.setdefault("reminders_cleaned", False)  # guard

                # Harden types (older saved states)
                if not isinstance(ev.get("reminder_messages"), list):
                    ev["reminder_messages"] = []
                    mark_dirty()

                # If event passed AND it's been 24h, delete all stored reminder messages
                if (not ev.get("reminders_cleaned", False)) and (now_dt >= (dt + timedelta(seconds=MILESTONE_CLEANUP_AFTER_EVENT_SECONDS))):
                    msgs = ev.get("reminder_messages", []) or []
                    if msgs:
                        had_forbidden = False

                        for item in msgs:
                            try:
                                ch_id = int(item.get("channel_id") or channel.id)
                                msg_id = int(item.get("message_id") or 0)
                            except Exception:
                                continue

                            if msg_id <= 0:
                                continue

                            ch = await get_text_channel(ch_id)
                            if ch is None:
                                continue

                            try:
                                await ch.get_partial_message(msg_id).delete()
                            except discord.Forbidden:
                                had_forbidden = True
                                # Don’t spam attempts forever; notify once then stop trying.
                                try:
                                    missing = missing_channel_perms(ch, ch.guild)
                                    await notify_owner_missing_perms(
                                        ch.guild,
                                        ch,
                                        missing=missing,
                                        action="delete old milestone/repeat reminder messages (needs Manage Messages)",
                                    )
                                except Exception:
                                    pass
                                break
                            except (discord.NotFound, discord.HTTPException):
                                pass  # already gone or transient

                        # Whether we deleted them or couldn't, we stop trying after this cleanup window
                        ev["reminder_messages"] = []
                        ev["reminders_cleaned"] = True
                        mark_dirty()
                        flush_if_dirty()

                    else:
                        ev["reminders_cleaned"] = True
                        mark_dirty()
                        flush_if_dirty()
                        
                # ---- EVENT START BLAST (time-of-event) ----
                if dt <= now:
                    if not bool(ev.get("start_announced", False)):
                        age = (now - dt).total_seconds()
                        if age <= EVENT_START_GRACE_SECONDS:
                            mention_prefix = ""
                            allowed = discord.AllowedMentions.none()

                            perms = channel.permissions_for(bot_member)
                            if perms.mention_everyone:
                                mention_prefix, allowed = build_everyone_mention()
                            else:
                                mention_prefix, allowed = build_milestone_mention(channel, guild_state)

                            text = mention_prefix + build_start_blast_message(
                                guild_state,
                                event_name=ev.get("name", "Event")
                            )

                            try:
                                m = await channel.send(text, allowed_mentions=allowed)

                                # track for cleanup 24h after event passes
                                ev.setdefault("reminder_messages", []).append(
                                    {"channel_id": channel.id, "message_id": m.id}
                                )

                                # ✅ stop re-sending every loop
                                ev["start_announced"] = True
                                mark_dirty()
                                flush_if_dirty()

                            except discord.Forbidden:
                                missing = missing_channel_perms(channel, channel.guild)
                                await notify_owner_missing_perms(
                                    channel.guild,
                                    channel,
                                    missing=missing,
                                    action="send the event start announcement",
                                )
                            except discord.HTTPException as e:
                                print(f"[Guild {guild_id}] Failed to send start blast: {e}")

                    continue  # don’t do milestones/repeats for started/past events

                # ---- Milestones + repeating reminders ----
                desc, _, passed = compute_time_left(now, dt)
                if passed:
                    continue

                days_left = calendar_days_left(dt)
                if days_left < 0:
                    continue

                milestone_sent_today = False

                milestones = ev.get("milestones", DEFAULT_MILESTONES)
                announced = ev.get("announced_milestones", [])
                if not isinstance(announced, list):
                    announced = []
                    ev["announced_milestones"] = announced
                    mark_dirty()  # you changed the event dict

                if days_left in milestones and days_left not in announced:
                    mention_prefix, allowed_mentions = build_milestone_mention(channel, guild_state)

                    event_name = ev.get("name", "Event")
                    try:
                        date_str = dt.strftime("%B %d, %Y")
                    except Exception:
                        date_str = ""
                    body = build_milestone_message(
                        guild_state,
                        event_name=event_name,
                        days_left=days_left,
                        time_left=desc,
                        date_str=date_str,
                    )
                    text = f"{mention_prefix}{body}"

                    try:
                        m = await channel.send(text, allowed_mentions=allowed_mentions)

                        # ✅ track for cleanup 24h after event passes
                        ev.setdefault("reminder_messages", []).append(
                            {"channel_id": channel.id, "message_id": m.id}
                        )

                        # mutate state
                        announced.append(days_left)
                        ev["announced_milestones"] = announced
                        milestone_sent_today = True
                        mark_dirty()

                        # ✅ optional: flush immediately after a public send
                        flush_if_dirty()

                    except discord.Forbidden:
                        missing = missing_channel_perms(channel, channel.guild)
                        await notify_owner_missing_perms(
                            channel.guild,
                            channel,
                            missing=missing,
                            action="send milestone reminders",
                        )
                        continue

                    try:
                        await dm_owner_if_set(
                            channel.guild,
                            ev,
                            f"⏰ Milestone: **{ev.get('name', 'Event')}** is in **{days_left} day{'s' if days_left != 1 else ''}** "
                            f"(on {dt.strftime('%B %d, %Y at %I:%M %p %Z')})."
                        )
                    except Exception:
                        pass

                repeat_every = ev.get("repeat_every_days")
                if isinstance(repeat_every, int) and repeat_every > 0:
                    anchor_str = ev.get("repeat_anchor_date") or today.isoformat()
                    try:
                        anchor = date.fromisoformat(anchor_str)
                    except ValueError:
                        anchor = today
                        ev["repeat_anchor_date"] = anchor.isoformat()
                        mark_dirty()

                    days_since_anchor = (today - anchor).days
                    if days_since_anchor > 0 and (days_since_anchor % repeat_every == 0):
                        sent_dates = ev.get("announced_repeat_dates", [])
                        if not isinstance(sent_dates, list):
                            sent_dates = []
                            ev["announced_repeat_dates"] = sent_dates
                            mark_dirty()

                        if today.isoformat() not in sent_dates and not milestone_sent_today:
                            try:
                                date_str = dt.strftime("%B %d, %Y")
                                text = build_repeat_message(
                                    guild_state,
                                    event_name=ev.get("name", "Event"),
                                    time_left=desc,
                                    date_str=date_str,
                                )
                                m = await channel.send(
                                    text,
                                    allowed_mentions=discord.AllowedMentions.none(),
                                )

                                # ✅ track for cleanup 24h after event passes
                                ev.setdefault("reminder_messages", []).append(
                                    {"channel_id": channel.id, "message_id": m.id}
                                )

                                # mutate state
                                sent_dates.append(today.isoformat())
                                ev["announced_repeat_dates"] = sent_dates[-180:]
                                mark_dirty()

                                # ✅ optional: flush immediately after a public send
                                flush_if_dirty()

                            except discord.Forbidden:
                                missing = missing_channel_perms(channel, channel.guild)
                                await notify_owner_missing_perms(
                                    channel.guild,
                                    channel,
                                    missing=missing,
                                    action="send repeating reminders",
                                )
                            except discord.HTTPException as e:
                                print(f"[Guild {guild_id}] Failed to send repeat reminder: {e}")

                            try:
                                await dm_owner_if_set(
                                    channel.guild,
                                    ev,
                                    f"🔁 Repeat reminder: **{ev.get('name', 'Event')}** is in **{desc}** "
                                    f"(on {dt.strftime('%B %d, %Y at %I:%M %p %Z')})."
                                )
                            except Exception:
                                pass

            # ---- Prune after processing (so start blast can happen) ----
            removed = prune_past_events(
                guild_state,
                now=datetime.now(DEFAULT_TZ) - timedelta(seconds=MILESTONE_CLEANUP_AFTER_EVENT_SECONDS),
            )
            if removed:
                mark_dirty()
                # (No immediate flush needed; no public post happened.)

            # ---- Update pinned embed once at end (reflects changes) ----
            try:
                pinned = await get_or_create_pinned_message(guild_id, channel, allow_create=True)
            except Exception:
                print(f"[Guild {guild_id}] get_or_create_pinned_message failed:\n{traceback.format_exc()}")
                pinned = None

            if pinned is not None:
                try:
                    embed = build_embed_for_guild(guild_state)
                except Exception:
                    print(f"[Guild {guild_id}] build_embed_for_guild failed:\n{traceback.format_exc()}")
                    embed = None

                if embed is not None:
                    try:
                        await pinned.edit(embed=embed)
                    except discord.NotFound:
                        gs = get_guild_state(guild_id)
                        if gs.get("pinned_message_id") == pinned.id:
                            gs["pinned_message_id"] = None
                            mark_dirty()
                            flush_if_dirty()  # worth flushing quickly
                    except discord.Forbidden:
                        missing = missing_channel_perms(channel, channel.guild)
                        await notify_owner_missing_perms(
                            channel.guild,
                            channel,
                            missing=missing,
                            action="edit/update the pinned countdown message",
                        )
                    except discord.HTTPException as e:
                        print(f"[Guild {guild_id}] Failed to edit pinned message: {e}")

            # ✅ Final flush: saves prune/anchor fixes/etc once per guild cycle
            flush_if_dirty()

        except Exception as e:
            print(f"[Guild {gid_str}] update_countdowns crashed for this guild: {type(e).__name__}: {e}")
            continue

@update_countdowns.before_loop
async def before_update_countdowns():
    await bot.wait_until_ready()


# ==========================
# SLASH COMMANDS
# ==========================

async def _safe_ephemeral(interaction: discord.Interaction, content: str):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)
    except Exception:
        pass

@bot.tree.command(name="vote", description="Vote for Chromie on Top.gg to unlock supporter perks.")
async def vote_cmd(interaction: discord.Interaction):
    voted = await topgg_has_voted(interaction.user.id, force=True)
    status = "✅ You currently have supporter access." if voted else "❌ You don’t have an active vote yet."

    await interaction.response.send_message(
        f"{status}\n\n{PREMIUM_PERKS_TEXT}",
        ephemeral=True,
        view=build_vote_view(),
    )

@bot.tree.command(name="vote_debug", description="Debug Top.gg vote verification (admin).")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def vote_debug_cmd(interaction: discord.Interaction):
    # bypass cache so you see reality right now
    _vote_cache.pop(interaction.user.id, None)
    voted = await topgg_has_voted(interaction.user.id, force=True)

    cfg = "✅" if (TOPGG_TOKEN and TOPGG_BOT_ID) else "❌"
    await interaction.response.send_message(
        "🔎 **Top.gg Vote Debug**\n"
        f"Configured (token + bot id): {cfg}\n"
        f"Bot ID set to: `{TOPGG_BOT_ID or 'MISSING'}`\n"
        f"Vote active (last 12h): {'✅' if voted else '❌'}\n\n"
        "If this shows ❌ but you *just* voted, it’s usually one of:\n"
        "• vote is older than 12 hours\n"
        "• you voted a different bot (dev vs prod ID mismatch)\n"
        "• token is wrong/expired (look for HTTP 401/403 in logs)\n",
        ephemeral=True,
    )

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await _safe_ephemeral(interaction, "You need **Manage Server** to use that command.")
        return
    if isinstance(error, VoteRequired):
        # We already responded in the check with the vote message.
        return        
    if isinstance(error, app_commands.CheckFailure):
        await _safe_ephemeral(interaction, "You don’t have permission to use that command.")
        return

    # Anything else: log for you
    print(f"[APP_COMMAND_ERROR] {type(error).__name__}: {error}")

def format_events_list(guild_state: dict) -> str:
    sort_events(guild_state)
    events = guild_state.get("events", [])
    if not events:
        return "There are no events set for this server yet.\nAdd one with `/addevent`."

    lines = []
    for idx, ev in enumerate(events, start=1):
        ts = ev.get("timestamp")
        if not isinstance(ts, (int, float)):
            continue

        dt = datetime.fromtimestamp(ts, tz=DEFAULT_TZ)
        now = datetime.now(DEFAULT_TZ)
        desc, _, passed = compute_time_left(now, dt)
        status = "✅ done" if passed else "⏳ active"

        repeat_every = ev.get("repeat_every_days")
        repeat_note = ""
        if isinstance(repeat_every, int) and repeat_every > 0:
            repeat_note = f" 🔁 every {repeat_every} day{'s' if repeat_every != 1 else ''}"

        silenced = ev.get("silenced", False)
        silenced_note = " 🔕 silenced" if silenced and not passed else ""

        owner_note = ""
        ol = "" if passed else format_owner_inline(ev)
        if ol:
            owner_note = f" • {ol}"

        lines.append(
            f"**{idx}. {ev.get('name', 'Event')}** — {dt.strftime('%m/%d/%Y %H:%M')} "
            f"({desc}) [{status}]{repeat_note}{silenced_note}{owner_note}"
        )

    return "\n".join(lines)


@bot.tree.command(name="seteventchannel", description="Set this channel as the event countdown channel.")
@app_commands.default_permissions(manage_guild=True)  # ✅ hides command from non-manage-server users
@app_commands.checks.has_permissions(manage_guild=True)  # ✅ runtime enforcement
@app_commands.guild_only()
async def seteventchannel(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    await interaction.response.defer(ephemeral=True)

    # ✅ Guard: only allow normal text channels (not threads, not forums, etc.)
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.edit_original_response(
            content="Please run `/seteventchannel` in a **regular text channel** (not a thread/forum)."
        )
        return

    # ✅ Defense-in-depth: verify perms via resolved Member object
    member = interaction.user
    if not isinstance(member, discord.Member):
        member = guild.get_member(interaction.user.id) or member  # best effort

    perms = getattr(member, "guild_permissions", None)
    if not perms or not (perms.manage_guild or perms.administrator):
        await interaction.edit_original_response(
            content="You need **Manage Server** (or **Administrator**) to change the event channel."
        )
        return

    guild_state = get_guild_state(guild.id)

    old_channel_id = guild_state.get("event_channel_id")
    new_channel = interaction.channel

    # If no-op, don’t spam notifications
    if old_channel_id and int(old_channel_id) == int(new_channel.id):
        await interaction.edit_original_response(
            content="✅ This channel is already the event countdown channel."
        )
        return

    guild_state["event_channel_id"] = new_channel.id
    guild_state["pinned_message_id"] = None

    # audit fields (optional)
    guild_state["event_channel_set_by"] = int(interaction.user.id)
    guild_state["event_channel_set_at"] = int(time.time())

    sort_events(guild_state)
    save_state()

    # Permissions check + owner DM (you already do this)
    missing: list[str] = []
    if hasattr(new_channel, "permissions_for"):
        missing = missing_channel_perms(new_channel, guild)
        if missing:
            await notify_owner_missing_perms(
                guild,
                new_channel,
                missing=missing,
                action="set up the countdown (send + pin + update)",
            )

    # 🔔 NEW: Notify owner + optionally post audit note(s)
    await notify_event_channel_changed(
        guild,
        actor=interaction.user,
        old_channel_id=old_channel_id if isinstance(old_channel_id, int) else None,
        new_channel=new_channel,
    )

    extra = ""
    if missing:
        extra = (
            "\n\n⚠️ I’m missing some permissions in this channel, so the countdown may not work yet. "
            "I’ve messaged the server owner with a quick fix guide."
        )

    await interaction.edit_original_response(
        content="✅ This channel is now the event countdown channel for this server.\nUse `/addevent` to add events." + extra
    )

@bot.tree.command(name="linkserver", description="Link yourself to this server for DM control.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def linkserver(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    user_links = get_user_links()
    user_links[str(interaction.user.id)] = guild.id
    save_state()

    await interaction.response.send_message(
        "🔗 Linked your user to this server.\nYou can now DM me `/addevent` and I’ll add events to this server (Manage Server required).",
        ephemeral=True,
    )

digest_group = app_commands.Group(name="digest", description="Weekly event digest")
bot.tree.add_command(digest_group)

@digest_group.command(name="enable", description="Enable the weekly digest (Supporter perk).")
@require_vote("/digest enable")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def digest_enable_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    g = get_guild_state(guild.id)
    d = g.setdefault("digest", {"enabled": False, "channel_id": None, "last_sent_date": None})

    ch_id = g.get("event_channel_id") or interaction.channel_id
    d["enabled"] = True
    d["channel_id"] = int(ch_id)
    save_state()

    await interaction.response.send_message("✅ Weekly digest enabled.", ephemeral=True)


@digest_group.command(name="disable", description="Disable the weekly digest.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def digest_disable_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    g = get_guild_state(guild.id)
    d = g.setdefault("digest", {"enabled": False, "channel_id": None, "last_sent_date": None})
    d["enabled"] = False
    save_state()

    await interaction.response.send_message("🛑 Weekly digest disabled.", ephemeral=True)

# ==========================
# COUNTDOWN TITLE/DESCRIPTION (Supporter perk)
# ==========================

countdown_group = app_commands.Group(name="countdown", description="Customize the pinned countdown embed")
bot.tree.add_command(countdown_group)

@countdown_group.command(name="title", description="Set a custom title for the pinned countdown embed (Supporter perk).")
@require_vote("/countdown title")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
@app_commands.describe(text="New title (max 256 characters). Use 'default' to clear.")
async def countdown_title_cmd(interaction: discord.Interaction, text: str):
    guild = interaction.guild
    assert guild is not None
    await interaction.response.defer(ephemeral=True)

    g = get_guild_state(guild.id)

    raw = (text or "").strip()
    if raw.lower() == "default":
        g["countdown_title_override"] = None
        save_state()
        await refresh_countdown_message(guild, g)
        await interaction.edit_original_response(content="✅ Countdown title reset to the theme default.")
        return

    g["countdown_title_override"] = raw[:256]
    save_state()
    await refresh_countdown_message(guild, g)
    await interaction.edit_original_response(content=f"✅ Countdown title set to: **{g['countdown_title_override']}**")


@countdown_group.command(name="cleartitle", description="Clear the custom countdown title (back to theme default).")
@require_vote("/countdown cleartitle")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def countdown_cleartitle_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None
    await interaction.response.defer(ephemeral=True)

    g = get_guild_state(guild.id)
    g["countdown_title_override"] = None
    save_state()

    await refresh_countdown_message(guild, g)
    await interaction.edit_original_response(content="✅ Countdown title cleared (using theme default).")


@countdown_group.command(name="description", description="Set a custom intro/description shown above the event list (Supporter perk).")
@require_vote("/countdown description")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
@app_commands.describe(text="Intro text shown above the list (max 1024 recommended). Use 'clear' to remove.")
async def countdown_description_cmd(interaction: discord.Interaction, text: str):
    guild = interaction.guild
    assert guild is not None
    await interaction.response.defer(ephemeral=True)

    g = get_guild_state(guild.id)

    raw = (text or "").strip()
    if raw.lower() in ("clear", "none", "off"):
        g["countdown_description_override"] = None
        save_state()
        await refresh_countdown_message(guild, g)
        await interaction.edit_original_response(content="✅ Countdown description cleared.")
        return

    # Keep it comfortably within embed limits.
    # (Description total max is 4096; we prepend this above the list.)
    g["countdown_description_override"] = raw[:1500]
    save_state()

    await refresh_countdown_message(guild, g)
    await interaction.edit_original_response(content="✅ Countdown description updated.")


@countdown_group.command(name="cleardescription", description="Clear the custom countdown description.")
@require_vote("/countdown cleardescription")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def countdown_cleardescription_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None
    await interaction.response.defer(ephemeral=True)

    g = get_guild_state(guild.id)
    g["countdown_description_override"] = None
    save_state()

    await refresh_countdown_message(guild, g)
    await interaction.edit_original_response(content="✅ Countdown description cleared.")


@bot.tree.command(name="addevent", description="Add a new event to the countdown.")
@app_commands.describe(
    date="Date in MM/DD/YYYY format",
    time="Time in 24-hour HH:MM format (America/Chicago)",
    name="Name of the event",
)
async def addevent(interaction: discord.Interaction, date: str, time: str, name: str):
    await interaction.response.defer(ephemeral=True)
    user = interaction.user

    if interaction.guild is not None:
        guild = interaction.guild

        member = interaction.user
        if not isinstance(member, discord.Member):
            member = guild.get_member(user.id)

        if member is None:
            try:
                member = await guild.fetch_member(user.id)
            except Exception:
                member = None

        perms = getattr(member, "guild_permissions", None)
        if not perms or not (perms.manage_guild or perms.administrator):
            await interaction.edit_original_response(
                content="You need the **Manage Server** or **Administrator** permission to add events in this server."
            )
            return

        guild_state = get_guild_state(guild.id)
        is_dm = False

    else:
        user_links = get_user_links()
        linked_guild_id = user_links.get(str(user.id))
        if not linked_guild_id:
            await interaction.edit_original_response(
                content="I don't know which server to use for your DMs yet.\nIn the server you want to control, run `/linkserver`, then DM me `/addevent` again."
            )
            return

        guild = bot.get_guild(linked_guild_id)
        if not guild:
            await interaction.edit_original_response(
                content="I can't find the linked server anymore. Maybe I was removed from it?\nRe-add me and run `/linkserver` again."
            )
            return

        member = guild.get_member(user.id)
        if member is None:
            try:
                member = await guild.fetch_member(user.id)
            except Exception:
                member = None

        perms = getattr(member, "guild_permissions", None)
        if not perms or not (perms.manage_guild or perms.administrator):
            await interaction.edit_original_response(
                content="You no longer have **Manage Server** (or **Administrator**) in the linked server, so I can’t add events via DM."
            )
            return

        guild_state = get_guild_state(guild.id)
        is_dm = True

    if not guild_state.get("event_channel_id"):
        msg = "I don't know which channel to use yet.\nRun `/seteventchannel` in the channel where you want the countdown pinned."
        if is_dm:
            msg += "\n(Do this in the linked server.)"
        await interaction.edit_original_response(content=msg)
        return

    try:
        dt = datetime.strptime(f"{date} {time}", "%m/%d/%Y %H:%M")
    except ValueError:
        await interaction.edit_original_response(
            content="I couldn't understand that date/time.\nUse: `date: 04/12/2026` `time: 09:00` (MM/DD/YYYY + 24-hour HH:MM)."
        )
        return

    dt = dt.replace(tzinfo=DEFAULT_TZ)

    if dt <= datetime.now(DEFAULT_TZ):
        await interaction.edit_original_response(
            content="That date/time is in the past. Please choose a future time."
        )
        return

    # display name for creator/owner (use the resolved member you already fetched)
    creator_display = getattr(member, "display_name", None) or interaction.user.name

    event = {
        "name": name,
        "timestamp": int(dt.timestamp()),
        "owner_id": interaction.user.id,
        "owner_tag": str(interaction.user),
        "milestones": guild_state.get("default_milestones", DEFAULT_MILESTONES.copy()).copy(),
        "announced_milestones": [],
        "milestone_messages": [],     # ✅ store messages we sent
        "milestones_cleaned": False,  # ✅ prevents repeat attempts
        "repeat_every_days": None,
        "repeat_anchor_date": None,
        "announced_repeat_dates": [],
        "silenced": False,

        "created_by_user_id": int(user.id),
        "created_by_name": creator_display,

        "owner_user_id": int(user.id),
        "owner_name": creator_display,
        "banner_url": None,
        "start_announced": False,
        "banner_url": None,
    }

    guild_state["events"].append(event)
    sort_events(guild_state)
    save_state()

    channel_id = guild_state.get("event_channel_id")
    if channel_id:
        channel = await get_text_channel(channel_id)
        if channel is not None:
            await refresh_countdown_message(guild, guild_state)

    await interaction.edit_original_response(
        content=f"✅ Added event **{name}** on {dt.strftime('%B %d, %Y at %I:%M %p %Z')} in server **{guild.name}**."
    )
    await maybe_vote_nudge(interaction, "Event scheduled! If Chromie’s been useful, a Top.gg vote helps a ton.")



@bot.tree.command(name="listevents", description="List all events for this server.")
@app_commands.guild_only()
async def listevents(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None
    guild_state = get_guild_state(guild.id)

    text = format_events_list(guild_state)
    chunks = chunk_text(text, limit=1900)

    await interaction.response.send_message(chunks[0], ephemeral=True)
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk, ephemeral=True)


@bot.tree.command(name="nextevent", description="Show the next upcoming event.")
@app_commands.guild_only()
async def nextevent(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    g = get_guild_state(guild.id)
    sort_events(g)

    now = datetime.now(DEFAULT_TZ)
    next_ev = None
    for ev in g.get("events", []):
        dt = datetime.fromtimestamp(ev["timestamp"], tz=DEFAULT_TZ)
        if dt > now:
            next_ev = (ev, dt)
            break

    if not next_ev:
        await interaction.response.send_message("No upcoming events found.", ephemeral=True)
        return

    ev, dt = next_ev
    desc, _, _ = compute_time_left(now, dt)
    await interaction.response.send_message(
        f"⏭️ Next event: **{ev['name']}**\n"
        f"🗓️ {dt.strftime('%B %d, %Y at %I:%M %p %Z')}\n"
        f"⏱️ {desc} remaining",
        ephemeral=True,
    )


@bot.tree.command(name="eventinfo", description="Show details for one event.")
@app_commands.describe(index="The number shown in /listevents (1, 2, 3, ...)")
@app_commands.autocomplete(index=event_index_autocomplete)
@app_commands.guild_only()
async def eventinfo(interaction: discord.Interaction, index: int):
    guild = interaction.guild
    assert guild is not None
    g = get_guild_state(guild.id)
    ev = get_event_by_index(g, index)
    if not ev:
        await interaction.response.send_message("Invalid index. Use `/listevents` to see event numbers.", ephemeral=True)
        return

    dt = datetime.fromtimestamp(ev["timestamp"], tz=DEFAULT_TZ)
    now = datetime.now(DEFAULT_TZ)
    desc, _, passed = compute_time_left(now, dt)
    miles = ", ".join(str(x) for x in ev.get("milestones", DEFAULT_MILESTONES))
    repeat_every = ev.get("repeat_every_days")
    repeat_note = "off"
    if isinstance(repeat_every, int) and repeat_every > 0:
        repeat_note = f"every {repeat_every} day(s) (anchor: {ev.get('repeat_anchor_date')})"

    silenced = ev.get("silenced", False)
    owner_id = ev.get("owner_user_id")
    owner_note = f"<@{owner_id}>" if owner_id else "none"
    creator_name = ev.get("created_by_name") or ev.get("owner_name") or "unknown"
    creator_id = ev.get("created_by_user_id") or ev.get("owner_user_id")
    creator_note = f"{creator_name}" + (f" (ID: {creator_id})" if creator_id else "")

    await interaction.response.send_message(
        f"**Event #{index}: {ev['name']}**\n"
        f"🗓️ {dt.strftime('%B %d, %Y at %I:%M %p %Z')}\n"
        f"⏱️ {desc} remaining\n"
        f"📝 Created by: {creator_note}\n"
        f"👤 Owner (DM): {owner_note}\n"
        f"🔔 Milestones: {miles}\n"
        f"🔁 Repeat: {repeat_note}\n"
        f"🔕 Silenced: {'yes' if silenced and not passed else 'no'}",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )



@bot.tree.command(name="removeevent", description="Remove an event by its list number (from /listevents).")
@app_commands.describe(index="The number shown in /listevents (1, 2, 3, ...)")
@app_commands.autocomplete(index=event_index_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def removeevent(interaction: discord.Interaction, index: int):
    guild = interaction.guild
    assert guild is not None
    
    await interaction.response.defer(ephemeral=True)
    guild_state = get_guild_state(guild.id)
    sort_events(guild_state)
    events = guild_state.get("events", [])

    if not events:
        await interaction.edit_original_response(content="There are no events to remove.")
        return

    if index < 1 or index > len(events):
        await interaction.edit_original_response(content=f"Index must be between 1 and {len(events)}.")
        return

    ev = events.pop(index - 1)
    save_state()

    channel_id = guild_state.get("event_channel_id")
    if channel_id:
        ch = await get_text_channel(channel_id)
        if ch:
            await refresh_countdown_message(guild, guild_state)

    await interaction.edit_original_response(content=f"🗑 Removed event **{ev['name']}**.")


@bot.tree.command(name="editevent", description="Edit an event's name/date/time.")
@app_commands.describe(
    index="The number shown in /listevents (1, 2, 3, ...)",
    name="New name (optional)",
    date="New date MM/DD/YYYY (optional)",
    time="New time 24-hour HH:MM (optional)",
)
@app_commands.autocomplete(index=event_index_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def editevent(interaction: discord.Interaction, index: int, name: Optional[str] = None, date: Optional[str] = None, time: Optional[str] = None):
    guild = interaction.guild
    assert guild is not None

    await interaction.response.defer(ephemeral=True)
    
    g = get_guild_state(guild.id)
    guild_state = g
    ev = get_event_by_index(g, index)
    if not ev:
        await interaction.edit_original_response(content="Invalid index. Use `/listevents`.")
        return

    if name and name.strip():
        ev["name"] = name.strip()

    if date or time:
        current_dt = datetime.fromtimestamp(ev["timestamp"], tz=DEFAULT_TZ)
        new_date = current_dt.strftime("%m/%d/%Y")
        new_time = current_dt.strftime("%H:%M")

        if date and date.strip():
            new_date = date.strip()
        if time and time.strip():
            new_time = time.strip()

        try:
            dt = datetime.strptime(f"{new_date} {new_time}", "%m/%d/%Y %H:%M").replace(tzinfo=DEFAULT_TZ)
        except ValueError:
            await interaction.edit_original_response(content=
                "I couldn't understand that date/time.\nUse MM/DD/YYYY + 24-hour HH:MM."
            )
            return

        if dt <= datetime.now(DEFAULT_TZ):
            await interaction.edit_original_response(content=
                "That date/time is in the past. Please choose a future time."
            )
            return

        ev["timestamp"] = int(dt.timestamp())
        ev["announced_milestones"] = []
        ev["announced_repeat_dates"] = []

    sort_events(g)
    save_state()

    guild_state = g
    ch_id = g.get("event_channel_id")
    if ch_id:
        ch = await get_text_channel(ch_id)
        if ch:
            await refresh_countdown_message(guild, guild_state)

    dt_final = datetime.fromtimestamp(ev["timestamp"], tz=DEFAULT_TZ)
    await interaction.edit_original_response(content=
        f"✅ Updated event #{index}: **{ev['name']}**\n"
        f"🗓️ {dt_final.strftime('%B %d, %Y at %I:%M %p %Z')}"
    )


@bot.tree.command(name="dupeevent", description="Duplicate an event (optional time/name).")
@app_commands.describe(
    index="The number shown in /listevents (1, 2, 3, ...)",
    date="New date MM/DD/YYYY",
    time="New time 24-hour HH:MM (optional; defaults to original time)",
    name="New name (optional; defaults to original name)",
)
@app_commands.autocomplete(index=event_index_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def dupeevent(interaction: discord.Interaction, index: int, date: str, time: Optional[str] = None, name: Optional[str] = None):
    guild = interaction.guild
    assert guild is not None

    await interaction.response.defer(ephemeral=True)

    g = get_guild_state(guild.id)
    guild_state = g
    sort_events(g)
    ev = get_event_by_index(g, index)
    if not ev:
        await interaction.edit_original_response(content="Invalid index. Use `/listevents`.")
        return

    orig_dt = datetime.fromtimestamp(ev["timestamp"], tz=DEFAULT_TZ)
    use_time = time.strip() if time and time.strip() else orig_dt.strftime("%H:%M")
    use_name = name.strip() if name and name.strip() else ev["name"]

    try:
        dt = datetime.strptime(f"{date.strip()} {use_time}", "%m/%d/%Y %H:%M").replace(tzinfo=DEFAULT_TZ)
    except ValueError:
        await interaction.edit_original_response(content="Invalid date/time. Use MM/DD/YYYY + 24-hour HH:MM.")
        return

    if dt <= datetime.now(DEFAULT_TZ):
        await interaction.edit_original_response(content="That date/time is in the past. Please choose a future time.")
        return

    maker = guild.get_member(interaction.user.id)
    maker_name = maker.display_name if maker else interaction.user.name


    new_ev = {
        "name": use_name,
        "timestamp": int(dt.timestamp()),
        "milestones": ev.get("milestones", DEFAULT_MILESTONES.copy()).copy(),
        "announced_milestones": [],
        "repeat_every_days": ev.get("repeat_every_days"),
        "repeat_anchor_date": None,
        "announced_repeat_dates": [],
        "silenced": ev.get("silenced", False),
        "start_announced": False,
        "banner_url": ev.get("banner_url"),
        "created_by_user_id": int(interaction.user.id),
        "created_by_name": maker_name,
        "owner_user_id": int(interaction.user.id),
        "owner_name": maker_name,

    }

    g["events"].append(new_ev)
    sort_events(g)
    save_state()

    guild_state = g
    ch_id = g.get("event_channel_id")
    if ch_id:
        ch = await get_text_channel(ch_id)
        if ch:
            await refresh_countdown_message(guild, guild_state)

    await interaction.edit_original_response(content=
        f"🧬 Duplicated event #{index} → added **{new_ev['name']}** on {dt.strftime('%B %d, %Y at %I:%M %p %Z')}."
    )


@bot.tree.command(name="remindall", description="Send a notification to the channel about an event.")
@app_commands.describe(index="Optional: event number from /listevents (defaults to next upcoming event)")
@app_commands.autocomplete(index=event_index_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def remindall(interaction: discord.Interaction, index: Optional[int] = None):
    guild = interaction.guild
    assert guild is not None

    await interaction.response.defer(ephemeral=True)
    
    g = get_guild_state(guild.id)
    sort_events(g)

    channel_id = g.get("event_channel_id")
    if not channel_id:
        await interaction.edit_original_response(content="No event channel set. Run `/seteventchannel` first.")
        return

    channel = await get_text_channel(channel_id)
    if channel is None:
        await interaction.edit_original_response(content="I couldn't access the configured event channel.")
        return

    bot_member = await get_bot_member(guild)
    if not bot_member:
        await interaction.edit_original_response(content="I couldn't resolve my own permissions in this server.")
        return

    ev = None
    dt = None
    now = datetime.now(DEFAULT_TZ)

    if index is not None:
        ev = get_event_by_index(g, index)
        if not ev:
            await interaction.edit_original_response(content="Invalid index. Use `/listevents`.")
            return
        dt = datetime.fromtimestamp(ev["timestamp"], tz=DEFAULT_TZ)
    else:
        for candidate in g.get("events", []):
            cdt = datetime.fromtimestamp(candidate["timestamp"], tz=DEFAULT_TZ)
            if cdt > now:
                ev = candidate
                dt = cdt
                break

    if not ev or not dt:
        await interaction.edit_original_response(content="No upcoming event found to remind about.")
        return

    if ev.get("silenced", False):
        await interaction.edit_original_response(content="That event is currently silenced (use `/silence` to toggle it back on).")
        return

    desc, _, passed = compute_time_left(now, dt)
    if passed:
        await interaction.edit_original_response(content="That event has already started or passed.")
        return

    perms = channel.permissions_for(bot_member)
    mention_prefix = ""
    allowed = discord.AllowedMentions.none()

    if perms.mention_everyone:
        mention_prefix, allowed = build_everyone_mention()
    else:
        mention_prefix, allowed = build_milestone_mention(channel, g)

    date_str = dt.strftime("%B %d, %Y at %I:%M %p %Z")
    body = build_remindall_message(g, event_name=ev["name"], time_left=desc, date_str=date_str)
    msg = f"{mention_prefix}{body}"

    try:
        await channel.send(msg, allowed_mentions=allowed)
    except discord.Forbidden:
        missing = missing_channel_perms(channel, channel.guild)
        await notify_owner_missing_perms(
            channel.guild,
            channel,
            missing=missing,
            action="send /remindall notifications",
        )
        await interaction.edit_original_response(content=
            "I don't have permission to send messages in the event channel."
        )
        return

    await interaction.edit_original_response(content="✅ Reminder sent.")
    await maybe_vote_nudge(interaction, "Reminder delivered. If you like Chromie’s vibe, a Top.gg vote unlocks supporter tools.")


@bot.tree.command(name="setmilestones", description="Set custom milestone days for an event.")
@app_commands.describe(
    index="The number shown in /listevents (1, 2, 3, ...)",
    milestones="Comma/space-separated days (example: 100, 50, 30, 14, 7, 2, 1, 0)",
)
@app_commands.autocomplete(index=event_index_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def setmilestones(interaction: discord.Interaction, index: int, milestones: str):
    guild = interaction.guild
    assert guild is not None
    g = get_guild_state(guild.id)

    ev = get_event_by_index(g, index)
    if not ev:
        await interaction.response.send_message("Invalid index. Use `/listevents`.", ephemeral=True)
        return

    parsed = parse_milestones(milestones)
    if not parsed:
        await interaction.response.send_message(
            "Invalid milestones. Use numbers like: `100, 50, 30, 14, 7, 2, 1, 0`.",
            ephemeral=True,
        )
        return

    ev["milestones"] = parsed
    ev["announced_milestones"] = []
    save_state()

    await interaction.response.send_message(
        f"✅ Updated milestones for **{ev['name']}**: {', '.join(str(x) for x in parsed)}",
        ephemeral=True,
    )
    
milestones_group = app_commands.Group(name="milestones", description="Milestone settings (Supporter perk)")
bot.tree.add_command(milestones_group)

@milestones_group.command(name="advanced", description="Set server default milestones (Supporter perk).")
@app_commands.describe(
    milestones="Comma/space-separated days (example: 100, 60, 30, 14, 7, 2, 1, 0)",
    apply_to_all="Also apply this list to all existing events",
)
@require_vote("/milestones advanced")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def milestones_advanced_cmd(interaction: discord.Interaction, milestones: str, apply_to_all: bool = True):
    guild = interaction.guild
    assert guild is not None

    parsed = parse_milestones(milestones)
    if not parsed:
        await interaction.response.send_message("Invalid milestones format.", ephemeral=True)
        return

    g = get_guild_state(guild.id)

    # Capture the old defaults BEFORE we overwrite them
    old_defaults = g.get("default_milestones")
    if not isinstance(old_defaults, list) or not old_defaults:
        old_defaults = DEFAULT_MILESTONES

    g["default_milestones"] = parsed

    updated = 0
    events = g.get("events", [])
    if not isinstance(events, list):
        events = []
        g["events"] = events

    for ev in events:
        if not isinstance(ev, dict):
            continue

        # Always reset announced milestones when changing milestone lists
        def _apply():
            nonlocal updated
            ev["milestones"] = parsed.copy()
            ev["announced_milestones"] = []
            updated += 1

        if apply_to_all:
            _apply()
        else:
            # Only update events that were still using the old default list
            cur = ev.get("milestones")
            if (not isinstance(cur, list) or not cur) or cur == old_defaults:
                _apply()

    save_state()

    note = (
        f"✅ Server default milestones set to: {', '.join(str(x) for x in parsed)}\n"
        f"Updated **{updated}** existing event(s). "
    )
    if not apply_to_all:
        note += "(Only events using the *previous defaults* were updated — customized events were left alone.)"
    else:
        note += "(Applied to all events.)"

    await interaction.response.send_message(note, ephemeral=True)



template_group = app_commands.Group(name="template", description="Event templates (Supporter perk)")
bot.tree.add_command(template_group)

@template_group.command(name="save", description="Save an event as a template (Supporter perk).")
@app_commands.describe(index="Event number from /listevents", name="Template name")
@app_commands.autocomplete(index=event_index_autocomplete)
@require_vote("/template save")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def template_save_cmd(interaction: discord.Interaction, index: int, name: str):
    guild = interaction.guild
    assert guild is not None

    g = get_guild_state(guild.id)
    ev = get_event_by_index(g, index)
    if not ev:
        await interaction.response.send_message("Invalid index. Use /listevents.", ephemeral=True)
        return

    key = (name or "").strip().lower()
    if not key:
        await interaction.response.send_message("Template name can’t be empty.", ephemeral=True)
        return

    templates = g.setdefault("templates", {})
    templates[key] = {
        "display_name": name.strip(),
        "milestones": (ev.get("milestones") or DEFAULT_MILESTONES).copy(),
        "repeat_every_days": ev.get("repeat_every_days"),
        "silenced": bool(ev.get("silenced", False)),
    }
    save_state()

    await interaction.response.send_message(f"✅ Saved template **{name.strip()}**.", ephemeral=True)


@template_group.command(name="load", description="Create a new event from a template (Supporter perk).")
@app_commands.describe(
    name="Template name",
    date="MM/DD/YYYY",
    time="24-hour HH:MM",
    event_name="Name for the new event",
)
@require_vote("/template load")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def template_load_cmd(interaction: discord.Interaction, name: str, date: str, time: str, event_name: str):
    guild = interaction.guild
    assert guild is not None

    g = get_guild_state(guild.id)
    guild_state = g
    templates = g.get("templates", {})
    key = (name or "").strip().lower()
    tpl = templates.get(key)
    if not tpl:
        await interaction.response.send_message("Template not found. Use /template save first.", ephemeral=True)
        return

    try:
        dt = datetime.strptime(f"{date} {time}", "%m/%d/%Y %H:%M").replace(tzinfo=DEFAULT_TZ)
    except ValueError:
        await interaction.response.send_message("Invalid date/time. Use MM/DD/YYYY + 24-hour HH:MM.", ephemeral=True)
        return

    if dt <= datetime.now(DEFAULT_TZ):
        await interaction.response.send_message("That date/time is in the past. Choose a future time.", ephemeral=True)
        return

    maker = guild.get_member(interaction.user.id)
    maker_name = maker.display_name if maker else interaction.user.name

    new_ev = {
        "name": event_name,
        "timestamp": int(dt.timestamp()),
        "milestones": list(tpl.get("milestones") or DEFAULT_MILESTONES),
        "announced_milestones": [],
        "repeat_every_days": tpl.get("repeat_every_days"),
        "repeat_anchor_date": None,
        "announced_repeat_dates": [],
        "silenced": bool(tpl.get("silenced", False)),
        "start_announced": False,
        "banner_url": None,
        "created_by_user_id": int(interaction.user.id),
        "created_by_name": maker_name,
        "owner_user_id": int(interaction.user.id),
        "owner_name": maker_name,
    }

    g["events"].append(new_ev)
    sort_events(g)
    save_state()
    guild_state = g
    ch_id = g.get("event_channel_id")
    if ch_id:
        ch = await get_text_channel(int(ch_id))
        if ch:
            await refresh_countdown_message(guild, guild_state)

    await interaction.response.send_message(
        f"✅ Created **{event_name}** from template **{tpl.get('display_name', name)}**.",
        ephemeral=True,
    )
def _clean_url(u: str) -> str:
    u = (u or "").strip()
    # allow people to paste <https://...>
    if u.startswith("<") and u.endswith(">"):
        u = u[1:-1].strip()
    return u

def _looks_like_image_url(u: str) -> bool:
    if not u:
        return False
    if not (u.startswith("http://") or u.startswith("https://")):
        return False
    # Discord/CDN links often don’t end with extensions, so be permissive:
    img_exts = (".png", ".jpg", ".jpeg", ".gif", ".webp")
    return ("cdn.discordapp.com" in u) or ("media.discordapp.net" in u) or u.lower().split("?")[0].endswith(img_exts)
    
banner_group = app_commands.Group(name="banner", description="Event banners (Supporter perk)")
bot.tree.add_command(banner_group)

@banner_group.command(name="set", description="Set a banner image for an event (Supporter perk).")
@app_commands.describe(index="Event number from /listevents", url="Direct image URL (https://...)")
@app_commands.autocomplete(index=event_index_autocomplete)
@require_vote("/banner set")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def banner_set_cmd(interaction: discord.Interaction, index: int, url: str):
    guild = interaction.guild
    assert guild is not None

    g = get_guild_state(guild.id)
    guild_state = g
    ev = get_event_by_index(g, index)
    if not ev:
        await interaction.response.send_message("Invalid index.", ephemeral=True)
        return

    u = (url or "").strip()
    if not (u.startswith("https://") or u.startswith("http://")):
        await interaction.response.send_message("Banner URL must start with http:// or https://", ephemeral=True)
        return

    ev["banner_url"] = u
    save_state()
    
    guild_state = g
    ch_id = g.get("event_channel_id")
    if ch_id:
        ch = await get_text_channel(int(ch_id))
        if ch:
            await refresh_countdown_message(guild, guild_state)

    await interaction.response.send_message(f"✅ Banner set for event #{index}.", ephemeral=True)

@banner_group.command(name="clear", description="Remove a banner image from an event (Supporter perk).")
@app_commands.describe(index="Event number from /listevents")
@app_commands.autocomplete(index=event_index_autocomplete)
@require_vote("/banner clear")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def banner_clear_cmd(interaction: discord.Interaction, index: int):
    guild = interaction.guild
    assert guild is not None

    g = get_guild_state(guild.id)
    guild_state = g
    ev = get_event_by_index(g, index)
    if not ev:
        await interaction.response.send_message("Invalid index.", ephemeral=True)
        return

    # If nothing to clear, be friendly and exit
    if not ev.get("banner_url"):
        await interaction.response.send_message(
            f"🧼 Event #{index} (**{ev.get('name','Event')}**) doesn’t have a banner set.",
            ephemeral=True,
        )
        return

    ev["banner_url"] = None
    save_state()

    # Refresh pinned embed so the image disappears immediately
    ch_id = g.get("event_channel_id")
    if ch_id:
        ch = await get_text_channel(int(ch_id))
        if ch:
            await refresh_countdown_message(guild, guild_state)

    await interaction.response.send_message(
        f"✅ Banner removed for event #{index} (**{ev.get('name','Event')}**).",
        ephemeral=True,
    )


@bot.tree.command(name="resetmilestones", description="Restore default milestone days for an event.")
@app_commands.describe(index="The number shown in /listevents (1, 2, 3, ...)")
@app_commands.autocomplete(index=event_index_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def resetmilestones(interaction: discord.Interaction, index: int):
    guild = interaction.guild
    assert guild is not None
    g = get_guild_state(guild.id)

    ev = get_event_by_index(g, index)
    if not ev:
        await interaction.response.send_message("Invalid index. Use `/listevents`.", ephemeral=True)
        return

    defaults = g.get("default_milestones")
    if not isinstance(defaults, list) or not defaults:
        defaults = DEFAULT_MILESTONES
    ev["milestones"] = list(defaults)
    ev["announced_milestones"] = []
    save_state()

    await interaction.response.send_message(
        f"✅ Milestones reset for **{ev['name']}** to defaults: {', '.join(str(x) for x in defaults)}",
        ephemeral=True,
    )


@bot.tree.command(name="silence", description="Stop reminders for an event (keeps it listed).")
@app_commands.describe(index="The number shown in /listevents (1, 2, 3, ...)")
@app_commands.autocomplete(index=event_index_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def silence(interaction: discord.Interaction, index: int):
    guild = interaction.guild
    assert guild is not None
    g = get_guild_state(guild.id)

    ev = get_event_by_index(g, index)
    if not ev:
        await interaction.response.send_message("Invalid index. Use `/listevents`.", ephemeral=True)
        return

    ev["silenced"] = not bool(ev.get("silenced", False))
    save_state()

    state_word = "silenced 🔕" if ev["silenced"] else "unsilenced 🔔"
    await interaction.response.send_message(
        f"✅ **{ev['name']}** is now {state_word}.",
        ephemeral=True,
    )


@bot.tree.command(name="seteventowner", description="Assign an owner (they get milestone DMs).")
@app_commands.describe(
    index="The number shown in /listevents (1, 2, 3, ...)",
    user="User who should receive DMs for this event",
)
@app_commands.autocomplete(index=event_index_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def seteventowner(interaction: discord.Interaction, index: int, user: discord.User):
    guild = interaction.guild
    assert guild is not None
    g = get_guild_state(guild.id)

    ev = get_event_by_index(g, index)
    if not ev:
        await interaction.response.send_message("Invalid index. Use `/listevents`.", ephemeral=True)
        return

    ev["owner_user_id"] = int(user.id)

    # Cache a non-pinging display name for embeds/lists
    member = guild.get_member(user.id)
    ev["owner_name"] = member.display_name if member else user.name

    save_state()

    await interaction.response.send_message(
        f"✅ Set owner for **{ev['name']}** to {user.mention} (they'll receive milestone + repeat reminder DMs).",
        ephemeral=True,
    )


@bot.tree.command(name="cleareventowner", description="Remove the owner for an event.")
@app_commands.describe(index="The number shown in /listevents (1, 2, 3, ...)")
@app_commands.autocomplete(index=event_index_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def cleareventowner(interaction: discord.Interaction, index: int):
    guild = interaction.guild
    assert guild is not None
    g = get_guild_state(guild.id)

    ev = get_event_by_index(g, index)
    if not ev:
        await interaction.response.send_message("Invalid index. Use `/listevents`.", ephemeral=True)
        return

    ev["owner_user_id"] = None
    ev["owner_name"] = None
    save_state()

    await interaction.response.send_message(
        f"✅ Cleared owner for **{ev['name']}**.",
        ephemeral=True,
    )


@bot.tree.command(name="setmentionrole", description="Mention a role on milestone posts.")
@app_commands.describe(role="Role to mention when milestone reminders post")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def setmentionrole(interaction: discord.Interaction, role: discord.Role):
    # ✅ Prevent the special @everyone role (it causes "@@everyone" escaping)
    if role.is_default():  # @everyone role
        await interaction.response.send_message(
            "⚠️ You can’t set **@everyone** as the mention role.\n"
            "If you want @everyone pings, give Chromie the **Mention Everyone** permission instead.",
            ephemeral=True,
        )
        return

    guild = interaction.guild
    assert guild is not None
    g = get_guild_state(guild.id)

    g["mention_role_id"] = int(role.id)
    save_state()

    await interaction.response.send_message(
        f"✅ Milestone reminders will now mention {role.mention}.",
        ephemeral=True,
    )

@bot.tree.command(name="clearmentionrole", description="Stop role mentions on milestone posts.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def clearmentionrole(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None
    g = get_guild_state(guild.id)

    g["mention_role_id"] = None
    save_state()

    await interaction.response.send_message(
        "✅ Milestone role mentions have been cleared.",
        ephemeral=True,
    )


@bot.tree.command(name="setrepeat", description="Set a repeating reminder for an event (every X days).")
@app_commands.describe(
    index="The number shown in /listevents (1, 2, 3, ...)",
    every_days="Repeat interval in days (1 = daily, 7 = weekly, etc.)",
)
@app_commands.autocomplete(index=event_index_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def setrepeat(interaction: discord.Interaction, index: int, every_days: int):
    guild = interaction.guild
    assert guild is not None

    if every_days < 1 or every_days > 365:
        await interaction.response.send_message("Repeat interval must be between **1** and **365** days.", ephemeral=True)
        return

    g = get_guild_state(guild.id)
    sort_events(g)
    events = g.get("events", [])

    if not events:
        await interaction.response.send_message("There are no events yet. Add one with `/addevent` first.", ephemeral=True)
        return

    ev = get_event_by_index(g, index)
    if not ev:
        await interaction.response.send_message(f"Index must be between 1 and {len(events)}.", ephemeral=True)
        return

    today = _today_local_date().isoformat()
    ev["repeat_every_days"] = int(every_days)
    ev["repeat_anchor_date"] = today
    ev["announced_repeat_dates"] = []
    save_state()

    plural = "s" if every_days != 1 else ""
    await interaction.response.send_message(
        f"✅ Repeating reminders enabled for **{ev['name']}** — every **{every_days}** day{plural} (starting tomorrow). "
        f"Use `/clearrepeat index: {index}` to turn it off.",
        ephemeral=True,
    )


@bot.tree.command(name="clearrepeat", description="Turn off repeating reminders for an event.")
@app_commands.describe(index="The number shown in /listevents (1, 2, 3, ...)")
@app_commands.autocomplete(index=event_index_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def clearrepeat(interaction: discord.Interaction, index: int):
    guild = interaction.guild
    assert guild is not None

    g = get_guild_state(guild.id)
    sort_events(g)
    events = g.get("events", [])

    if not events:
        await interaction.response.send_message("There are no events to update.", ephemeral=True)
        return

    ev = get_event_by_index(g, index)
    if not ev:
        await interaction.response.send_message(f"Index must be between 1 and {len(events)}.", ephemeral=True)
        return

    ev["repeat_every_days"] = None
    ev["repeat_anchor_date"] = None
    ev["announced_repeat_dates"] = []
    save_state()

    await interaction.response.send_message(f"🧹 Repeating reminders disabled for **{ev['name']}**.", ephemeral=True)


@bot.tree.command(name="archivepast", description="Remove past events.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def archivepast(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None
    
    await interaction.response.defer(ephemeral=True)
    
    g = get_guild_state(guild.id)
    guild_state = g
    sort_events(g)

    now = datetime.now(DEFAULT_TZ)
    before = len(g.get("events", []))
    g["events"] = [ev for ev in g.get("events", []) if datetime.fromtimestamp(ev["timestamp"], tz=DEFAULT_TZ) > now]
    after = len(g["events"])
    removed = before - after

    save_state()
    
    guild_state = g
    ch_id = g.get("event_channel_id")
    if ch_id:
        ch = await get_text_channel(ch_id)
        if ch:
            await refresh_countdown_message(guild, guild_state)

    await interaction.edit_original_response(content=f"🧹 Archived **{removed}** past event(s).")


@bot.tree.command(name="resetchannel", description="Clear the configured event channel for this server.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def resetchannel(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None
    g = get_guild_state(guild.id)

    g["event_channel_id"] = None
    g["pinned_message_id"] = None
    save_state()

    await interaction.response.send_message(
        "✅ Event channel configuration cleared. Run `/seteventchannel` again to set it.",
        ephemeral=True,
    )


@bot.tree.command(name="healthcheck", description="Show config + permission diagnostics.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def healthcheck(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None
    g = get_guild_state(guild.id)

    channel_id = g.get("event_channel_id")
    mention_role_id = g.get("mention_role_id")
    num_events = len(g.get("events", []))

    lines = []
    lines.append("**ChronoBot Healthcheck**")
    lines.append(f"Server: **{guild.name}**")
    lines.append(f"Events stored: **{num_events}**")

    if channel_id:
        ch = await get_text_channel(channel_id)
        if ch:
            lines.append(f"Event channel: {ch.mention} ✅")
            bot_member = await get_bot_member(guild)
            if bot_member:
                perms = ch.permissions_for(bot_member)
                lines.append(f"• Can view channel: {'✅' if perms.view_channel else '❌'}")
                lines.append(f"• Can send messages: {'✅' if perms.send_messages else '❌'}")
                lines.append(f"• Can embed links: {'✅' if perms.embed_links else '❌'}")
                lines.append(f"• Can read history: {'✅' if perms.read_message_history else '❌'}")
                lines.append(f"• Can manage messages (pin/unpin): {'✅' if perms.manage_messages else '❌'}")
                lines.append(f"• Can mention @everyone: {'✅' if perms.mention_everyone else '❌'}")
            else:
                lines.append("• Bot member resolution: ❌ (couldn’t fetch bot member)")
        else:
            lines.append("Event channel: ❌ (configured channel not found / not accessible)")
    else:
        lines.append("Event channel: ❌ (not set)")

    if mention_role_id:
        role = guild.get_role(int(mention_role_id))
        lines.append(f"Mention role: {role.mention} ✅" if role else "Mention role: ❌ (role not found)")
    else:
        lines.append("Mention role: (none)")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="purgeevents", description="Delete all events for this server (requires confirm).")
@app_commands.describe(confirm="Type YES to confirm you want to delete all events.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def purgeevents(interaction: discord.Interaction, confirm: str):
    guild = interaction.guild
    assert guild is not None

    await interaction.response.defer(ephemeral=True)
    
    if (confirm or "").strip().upper() != "YES":
        await interaction.edit_original_response(content="Not confirmed. To purge, run `/purgeevents confirm: YES`.")
        return

    g = get_guild_state(guild.id)
    g["events"] = []
    g["pinned_message_id"] = None
    save_state()

    guild_state = g
    ch_id = g.get("event_channel_id")
    if ch_id:
        ch = await get_text_channel(ch_id)
        if ch:
            await refresh_countdown_message(guild, guild_state)

    await interaction.edit_original_response(content="🧨 All events have been deleted for this server.")


@bot.tree.command(name="update_countdown", description="Force-refresh the pinned countdown.")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.guild_only()
async def update_countdown_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    await interaction.response.defer(ephemeral=True)
    
    g = get_guild_state(guild.id)
    sort_events(g)

    channel_id = g.get("event_channel_id")
    if not channel_id:
        await interaction.edit_original_response(content=
            "No events channel set yet. Run `/seteventchannel` first."
        )
        return

    if interaction.channel_id != channel_id:
        await interaction.edit_original_response(content=
            "Please run this command in the configured events channel."
        )
        return

    channel = interaction.channel
    assert isinstance(channel, discord.TextChannel)

    pinned = await get_or_create_pinned_message(guild.id, channel, allow_create=True)
    if pinned is None:
        await interaction.edit_original_response(
            content="I couldn't create or access the pinned countdown message here. Check my permissions.",
        )
        return

    embed = build_embed_for_guild(g)
    try:
        await pinned.edit(embed=embed)
    except discord.Forbidden:
        missing = missing_channel_perms(channel, channel.guild)
        await notify_owner_missing_perms(
            channel.guild,
            channel,
            missing=missing,
            action="edit/update the pinned countdown message",
        )
        await interaction.edit_original_response(content=
            "I don't have permission to edit that pinned message here. "
            "I’ve messaged the server owner with a permissions fix guide.",
        )
        return
    except discord.HTTPException as e:
        await interaction.edit_original_response(content=
            f"Discord errored while updating the pinned message: {e}",
        )
        return

    await interaction.edit_original_response(content="⏱ Countdown updated.")


@bot.tree.command(name="resendsetup", description="Resend the onboarding/setup message.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def resendsetup(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    await interaction.response.defer(ephemeral=True)
    
    g = get_guild_state(guild.id)
    g["welcomed"] = False
    save_state()

    await send_onboarding_for_guild(guild)
    await interaction.edit_original_response(content=
        "📨 Setup instructions have been resent to the server owner (or a fallback channel)."
    )


# ---- THEME COMMAND ----

async def theme_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[str]]:
    cur = (current or "").lower().strip()
    out: List[app_commands.Choice[str]] = []
    for key in THEMES.keys():
        label = _THEME_LABELS.get(key, key.title())
        if not cur or cur in key or cur in label.lower():
            out.append(app_commands.Choice(name=label, value=key))
    return out[:25]


@bot.tree.command(name="theme", description="Set the countdown theme (supporter themes require /vote).")
@app_commands.describe(theme="Theme name (autocomplete will suggest options)")
@app_commands.autocomplete(theme=theme_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def theme_cmd(interaction: discord.Interaction, theme: str):
    guild = interaction.guild
    assert guild is not None

    await interaction.response.defer(ephemeral=True)

    theme_id = normalize_theme_key(theme)
    if theme_id not in THEMES:
        choices = ", ".join(sorted(THEMES.keys()))
        await interaction.edit_original_response(content=f"Unknown theme: `{theme}`. Available: {choices}")
        return

    # Classic is always allowed; supporter themes require an active /vote by the caller.
    if THEMES[theme_id].get("supporter_only"):
        voted = await topgg_has_voted(interaction.user.id, force=True)
        if not voted:
            await send_vote_required(interaction, feature_label=f"`{theme_id}` theme")
            return

    g = get_guild_state(guild.id)
    g["theme"] = theme_id
    save_state()

    # Refresh the pinned message (best-effort)
    try:
        await refresh_countdown_message(guild, g)
    except Exception:
        pass

    await interaction.edit_original_response(content=f"Theme set to **{_THEME_LABELS.get(theme_id, theme_id.title())}**.")


@bot.tree.command(name="chronohelp", description="Show Chromie help (paged).")
async def chronohelp(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=build_help_embed("quick"),
        view=HelpView(),
        ephemeral=True,
    )



# ==========================
# RUN
# ==========================

def main():
    if not TOKEN:
        raise RuntimeError(
            "No bot token found. Set the DISCORD_BOT_TOKEN environment variable "
            "or edit the TOKEN section near the top of the file."
        )
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
