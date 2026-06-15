import os
import json
import traceback
from pathlib import Path
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional, List, Tuple, Dict, Any, Set
import time
import asyncio
import discord
from discord.errors import NotFound, HTTPException
from discord.ext import commands, tasks
from discord import app_commands
from threading import Lock
from enum import IntEnum
from dataclasses import dataclass
import random
import re
import aiohttp
import difflib
import hashlib
from discord.errors import NotFound as DiscordNotFound, Forbidden as DiscordForbidden, HTTPException
# ==========================
# CONFIG
# ==========================

VERSION = "2026-01-11"

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
_topgg_mismatch_warned = False
    
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
        "• 5 events • premium themes (`/countdown`) • event banners (`/event`)"
    )

    # Use followup if already responded
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True, view=build_vote_view())
    else:
        await interaction.response.send_message(msg, ephemeral=True, view=build_vote_view())

def get_topgg_bot_id() -> str:
    bot_user = getattr(bot, "user", None)
    if bot_user and bot_user.id:
        runtime_id = str(bot_user.id)
        if TOPGG_BOT_ID and TOPGG_BOT_ID != runtime_id:
            global _topgg_mismatch_warned
            if not _topgg_mismatch_warned:
                _topgg_mismatch_warned = True
                print(
                    "⚠️ TOPGG_BOT_ID does not match the running bot user ID. "
                    f"Using runtime ID {runtime_id}."
                )
        return runtime_id

    return TOPGG_BOT_ID


def build_vote_view() -> discord.ui.View:
    view = discord.ui.View()
    bot_id = get_topgg_bot_id()
    url = f"https://top.gg/bot/{bot_id}/vote" if bot_id else "https://top.gg"
    view.add_item(discord.ui.Button(label="Vote on Top.gg", style=discord.ButtonStyle.link, url=url))
    return view
        
TOPGG_API_V1_BASE = "https://top.gg/api/v1"

TOPGG_API_BASE = "https://top.gg/api"
CF_MARKERS = ("cdn-cgi/challenge-platform", "__CF$cv$params", "cf-ray")

def _looks_like_cloudflare_html(text: str, content_type: str) -> bool:
    ct = (content_type or "").lower()
    if "text/html" not in ct:
        return False
    t = (text or "")
    return any(m in t for m in CF_MARKERS)
    
async def topgg_has_voted(user_id: int, *, force: bool = False) -> bool:
    now = time.monotonic()
    cached = _vote_cache.get(user_id)

    if (not force) and cached and (now - cached[0] <= VOTE_CACHE_TTL_SECONDS):
        return cached[1]

    bot_id = get_topgg_bot_id()
    if not TOPGG_TOKEN or not bot_id:
        voted = True if TOPGG_FAIL_OPEN else False
        _vote_cache[user_id] = (now, voted)
        return voted

    url = f"{TOPGG_API_BASE}/bots/{bot_id}/check"
    headers = {
        "Authorization": TOPGG_TOKEN.strip(),
        "Accept": "application/json",
        "User-Agent": "Chromie/TopggVoteCheck",
    }
    params = {"userId": str(user_id)}

    # 🔥 Keep under Discord’s interaction window.
    timeout = aiohttp.ClientTimeout(total=2.0)

    voted = True if TOPGG_FAIL_OPEN else False

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, params=params) as resp:
                ct = resp.headers.get("Content-Type", "")
                text = await resp.text()

                # Cloudflare challenge HTML -> treat as "not voted" (or fail open if you want)
                if _looks_like_cloudflare_html(text, ct):
                    voted = True if TOPGG_FAIL_OPEN else False
                elif resp.status == 200:
                    try:
                        data = json.loads(text)
                        voted = bool(int(data.get("voted", 0) or 0))
                    except Exception:
                        voted = True if TOPGG_FAIL_OPEN else False
                else:
                    voted = True if TOPGG_FAIL_OPEN else False

    except asyncio.TimeoutError:
        voted = True if TOPGG_FAIL_OPEN else False
    except Exception:
        voted = True if TOPGG_FAIL_OPEN else False

    _vote_cache[user_id] = (now, voted)
    return voted

async def send_vote_required(interaction: discord.Interaction, feature_label: str):
    content = (
        f"⭐ **Supporter feature:** {feature_label}\n\n"
        f"🗳️ **Option 1: Vote on Top.gg** (free, lasts 12 hours)\n"
        f"Vote takes 10 seconds, then try again!\n\n"
        f"💎 **Option 2: Chromie Pro** ($2.99/mo)\n"
        f"Get all Supporter features permanently + unlimited events, templates, recurring reminders, and more."
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
        # Pro users get all Supporter features without voting
        if interaction.guild_id:
            guild_state = get_guild_state(interaction.guild_id)
            if is_pro(guild_state):
                return True
        
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
# PRO GATING (from spec - must be before build_embed_for_guild)
# ==========================

def has_active_vote_guild(guild_state: Dict[str, Any]) -> bool:
    """Check if guild has an active vote (supporter status) - guild-level check."""
    vote_until_str = guild_state.get("supporter", {}).get("vote_until")
    if vote_until_str:
        try:
            vote_until = datetime.fromisoformat(vote_until_str)
            now = datetime.now(timezone.utc)
            if vote_until.tzinfo is None:
                now = now.replace(tzinfo=None)
            if now < vote_until:
                return True
        except:
            pass
    return False

def is_pro(guild_state: Dict[str, Any]) -> bool:
    """
    Check if guild has active Pro status.
    Returns True if:
    - pro_active is True AND pro_until is in future, OR
    - grace_until is in future (migration grace period), OR
    - migration_mode is True (soft rollout), OR
    - discord_subscription is True (Discord subscription synced)
    """
    pro_data = guild_state.get("pro", {})
    
    # Check if marked as Discord subscription (means it's synced and active)
    if pro_data.get("discord_subscription", False):
        return True
    
    if pro_data.get("pro_active", False):
        pro_until_str = pro_data.get("pro_until")
        if pro_until_str:
            try:
                # Handle both timezone-aware and naive datetimes
                pro_until = datetime.fromisoformat(pro_until_str)
                now = datetime.now(timezone.utc)
                
                # If pro_until is timezone-aware, make now timezone-aware too
                if pro_until.tzinfo is not None and now.tzinfo is None:
                    now = now.replace(tzinfo=pro_until.tzinfo)
                # If pro_until is naive, make sure now is naive too
                elif pro_until.tzinfo is None and now.tzinfo is not None:
                    pro_until = pro_until.replace(tzinfo=now.tzinfo)
                
                if now < pro_until:
                    return True
            except Exception as e:
                print(f"[PRO] Error parsing pro_until: {pro_until_str}, error: {e}")
                pass
    
    grace_until_str = pro_data.get("grace_until")
    if grace_until_str:
        try:
            grace_until = datetime.fromisoformat(grace_until_str)
            now = datetime.now(timezone.utc)
            
            # Handle timezone-aware dates
            if grace_until.tzinfo is not None and now.tzinfo is None:
                now = now.replace(tzinfo=grace_until.tzinfo)
            elif grace_until.tzinfo is None and now.tzinfo is not None:
                grace_until = grace_until.replace(tzinfo=now.tzinfo)
            
            if now < grace_until:
                return True
        except Exception as e:
            print(f"[PRO] Error parsing grace_until: {grace_until_str}, error: {e}")
            pass
    
    if pro_data.get("migration_mode", False):
        return True
    
    return False

def get_pro_status_text(guild_state: Dict[str, Any]) -> str:
    """Get formatted Pro status for display."""
    if is_pro(guild_state):
        pro_data = guild_state.get("pro", {})
        if pro_data.get("migration_mode", False):
            return "✅ Pro Active (Migration Mode)"
        if pro_data.get("grace_until"):
            return "✅ Pro Active (Grace Period)"
        return "✅ Pro Active"
    return "🔒 Pro Locked"


def require_pro(feature_name: str):
    """Decorator to require Pro subscription for a command."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild_id:
            return True  # Allow in DMs if they have linked server
        
        guild_state = get_guild_state(interaction.guild_id)
        if is_pro(guild_state):
            return True
        
        # Send Pro required message
        embed = discord.Embed(
            title="❌ Chromie Pro Required",
            description=f"**{feature_name}** is exclusive to Chromie Pro subscribers.",
            color=discord.Color.orange()
        )
        embed.add_field(
            name="💎 Chromie Pro ($2.99/month)",
            value=(
                "• ♾️ Unlimited events\n"
                "• 🔁 Recurring event reminders\n"
                "• 📋 Event templates (save/load)\n"
                "• 📑 Duplicate events\n"
                "• 📊 Weekly digest\n"
                "• 🎨 All Supporter features (permanent)\n\n"
                "Subscribe via **Discord Server Subscription**.\n"
                "Pro unlocks for the **entire server**!"
            ),
            inline=False
        )
        
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except:
            pass
        
        return False
    
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
    "Voting unlocks (free, 12 hours):\n"
    "• 5 events (vs 3)\n"
    "• premium themes — `/countdown` → Theme\n"
    "• event banners — `/event` → Banner"
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
# TIMEZONE & REMINDER TIME FUNCTIONS
# ==========================

def get_server_timezone(guild_id: int) -> str:
    """
    Get server's timezone setting.
    Returns the timezone string (e.g., 'US/Eastern', 'UTC')
    Defaults to 'UTC' if not set.
    """
    guild_state = get_guild_state(guild_id)
    return guild_state.get("timezone", "UTC")


def set_server_timezone(guild_id: int, timezone: str) -> None:
    """
    Set server's timezone setting.
    Args:
        guild_id: The Discord guild ID
        timezone: Timezone string (e.g., 'US/Eastern', 'Europe/London')
    """
    guild_state = get_guild_state(guild_id)
    guild_state["timezone"] = timezone
    save_state()


def get_event_reminder_time(guild_id: int, event_index: int) -> Optional[str]:
    """
    Get custom reminder time for an event.
    Returns the time string (e.g., '09:00') or None if not set.
    
    Args:
        guild_id: The Discord guild ID
        event_index: The event index (0-based)
    
    Returns:
        Time string in HH:MM format, or None if using event time
    """
    guild_state = get_guild_state(guild_id)
    events = guild_state.get("events", [])
    
    if event_index < len(events):
        return events[event_index].get("reminder_time", None)
    return None


def set_event_reminder_time(guild_id: int, event_index: int, reminder_time: Optional[str]) -> bool:
    """
    Set custom reminder time for an event.
    
    Args:
        guild_id: The Discord guild ID
        event_index: The event index (0-based)
        reminder_time: Time string in HH:MM format, or None to use event time
    
    Returns:
        True if successful, False if event not found
    """
    guild_state = get_guild_state(guild_id)
    events = guild_state.get("events", [])
    
    if event_index < len(events):
        events[event_index]["reminder_time"] = reminder_time
        save_state()
        return True
    return False


def should_send_reminder_based_on_time(ev: dict, now: datetime, event_dt: datetime) -> bool:
    """
    Determine if a reminder should be sent based on custom reminder_time.
    
    If a custom reminder_time is set for the event, only return True if current time
    has reached or passed that time on the event day. Otherwise, use the event's
    scheduled time-of-day (so reminders don't fire at midnight).
    
    Args:
        ev: The event dictionary
        now: Current datetime with timezone info
    
    Returns:
        True if reminder should be sent, False otherwise
    """
    reminder_time_str = ev.get("reminder_time")
    
    # Parse the custom reminder time (format: HH:MM)
    try:
        parts = reminder_time_str.split(":")
        if len(parts) != 2:
            return True  # Invalid format, fall back to sending
        
        reminder_hour = int(parts[0])
        reminder_minute = int(parts[1])
        
        # Validate the parsed values
        if not (0 <= reminder_hour < 24 and 0 <= reminder_minute < 60):
            return True  # Invalid time values, fall back to sending
        
        reminder_time = datetime.min.replace(hour=reminder_hour, minute=reminder_minute).time()
    except (ValueError, AttributeError):
        reminder_time = None

    if reminder_time is None:
        reminder_time = event_dt.timetz().replace(tzinfo=None)

    now_time = now.timetz().replace(tzinfo=None)
    return now_time >= reminder_time


def get_guild_timezone(guild_state: dict) -> ZoneInfo:
    tz_name = (guild_state.get("timezone") or "UTC").strip()
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return DEFAULT_TZ

# ==========================
# STATE INIT (must exist globally)
# ==========================

state = load_state()

# MIGRATION: per-channel data model. Idempotent + self-backing-up. We run this
# manually on Render before deploying the new code, but calling it here too is
# defensive self-healing for any un-migrated guild (e.g. one that joined on old
# code). Wrapped so a single bad guild can never take down all servers.
try:
    from migrate_per_channel import migrate_state as _migrate_to_per_channel
    _migrate_to_per_channel(state)
except Exception as _mig_err:
    print(f"[MIGRATION] per-channel migration error at startup: {type(_mig_err).__name__}: {_mig_err}")

# MIGRATION: Disable migration_mode for all guilds to enforce tier limits
migration_applied = False
for guild_id_str, g_state in state.get("guilds", {}).items():
    sort_events(g_state)
    
    # Ensure pro structure exists and disable migration_mode
    if "pro" not in g_state:
        g_state["pro"] = {
            "pro_active": False,
            "pro_until": None,
            "grace_until": None,
            "migration_mode": False
        }
        migration_applied = True
    elif g_state["pro"].get("migration_mode", False):
        # Disable migration_mode to enforce tier limits
        g_state["pro"]["migration_mode"] = False
        migration_applied = True
        print(f"[MIGRATION] Disabled migration_mode for guild {guild_id_str}")

if migration_applied:
    print("[MIGRATION] Tier limits now enforced for all guilds")
    save_state()
else:
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
            
            # NEW (supporter/pro tracking - from spec)
            "supporter": {
                "last_vote_at": None,
                "vote_until": None
            },
            "pro": {
                "pro_active": False,
                "pro_until": None,
                "grace_until": None,
                "migration_mode": False  # Disabled - tier limits now enforced
            },
            "auto_delete_milestones": True,
            "time_unit": "discord",

            # Per-channel countdowns scaffolding (see migrate_per_channel.py).
            # Legacy fields above stay for now; the command sweep moves event/
            # display data into per-channel buckets here.
            "channels": {},
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
        guilds[gid].setdefault("supporter", {"last_vote_at": None, "vote_until": None})
        guilds[gid].setdefault("pro", {"pro_active": False, "pro_until": None, "grace_until": None, "migration_mode": False})
        guilds[gid].setdefault("auto_delete_milestones", True)
        guilds[gid].setdefault("time_unit", "discord")
        guilds[gid].setdefault("channels", {})  # per-channel countdowns scaffolding
    return guilds[gid]


# ==========================
# PER-CHANNEL COUNTDOWNS — accessor layer
# ==========================
# Data model: each guild has a `channels` dict mapping a channel id (str) to a
# per-channel "bucket" holding that channel's own countdown (events, pinned
# message, theme, timezone, digest, etc.). Billing (pro/supporter), the shared
# template library, and `welcomed` stay at the guild level. See
# migrate_per_channel.py for the one-time guild->per-channel migration.
#
# Gating: 1 countdown channel is free (identical to the old single-countdown
# behavior); additional channels require ChronoBot Plus (is_pro). A channel that
# already exists is always editable, even if the guild later drops below Plus.

FREE_CHANNEL_LIMIT = 1  # free servers get exactly one countdown channel


def _default_channel_state() -> dict:
    """A fresh per-channel countdown bucket with sensible defaults."""
    return {
        "pinned_message_id": None,
        "mention_role_id": None,
        "events": [],
        "event_channel_set_by": None,
        "event_channel_set_at": None,
        "theme": DEFAULT_THEME_ID,
        "countdown_title_override": None,
        "countdown_description_override": None,
        "default_milestones": DEFAULT_MILESTONES.copy(),
        "digest": {"enabled": False, "channel_id": None, "last_sent_date": None},
        "auto_delete_milestones": True,
        "time_unit": "discord",
        "timezone": "UTC",
        "custom_theme": None,  # Pro build-your-own: {title, subtitle, footer, color(int), emoji}
    }


def get_channel_state(guild_id: int, channel_id: int) -> dict:
    """
    Return the per-channel countdown bucket for (guild_id, channel_id),
    creating it with defaults if it does not exist. Existing buckets are
    backfilled with any newly-added fields (so older saved states stay valid).
    """
    guild = get_guild_state(guild_id)
    channels = guild.setdefault("channels", {})
    cid = str(channel_id)
    if cid not in channels:
        channels[cid] = _default_channel_state()
    else:
        for key, default in _default_channel_state().items():
            channels[cid].setdefault(key, default)
    return channels[cid]


def iter_channel_states(guild_state: dict):
    """
    Yield (channel_id:int, channel_state) for every real countdown channel in a
    guild. Skips the non-numeric "unassigned" sentinel bucket the migration uses
    to preserve events that never had a channel set.
    """
    for cid, cs in guild_state.get("channels", {}).items():
        if isinstance(cid, str) and cid.isdigit():
            yield int(cid), cs


def count_countdown_channels(guild_state: dict) -> int:
    """Number of real (numeric-keyed) countdown channels in a guild."""
    return sum(1 for cid in guild_state.get("channels", {})
               if isinstance(cid, str) and cid.isdigit())


def can_add_countdown_channel(guild_state: dict, channel_id: int) -> bool:
    """
    Whether a countdown can be set up in `channel_id`. Always True if the channel
    is already a countdown channel; otherwise free servers are capped at
    FREE_CHANNEL_LIMIT and ChronoBot Plus (is_pro) is unlimited.
    """
    if str(channel_id) in guild_state.get("channels", {}):
        return True
    if is_pro(guild_state):
        return True
    return count_countdown_channels(guild_state) < FREE_CHANNEL_LIMIT


def resolve_event_channel(guild_state: dict, channel_id):
    """Pick which countdown channel a CRUD command (addevent/listevents/…) acts on.

    Returns (channel_id:int, channel_state:dict) or (None, None) if it can't be
    resolved unambiguously. Resolution order:
      1. If `channel_id` is itself a countdown channel, use it.
      2. Else if the guild has exactly ONE countdown channel, use that one. This
         keeps the single-channel experience working for free servers (and for
         DM control, where there is no "current channel"): the command can be run
         anywhere in the server.
      3. Else (0 channels, or 2+ on Plus) -> (None, None); the caller shows
         guidance based on count_countdown_channels().
    """
    channels = guild_state.get("channels", {})
    cid_str = str(channel_id)
    if cid_str.isdigit() and cid_str in channels:
        return int(cid_str), channels[cid_str]
    real = [(int(c), b) for c, b in channels.items() if c.isdigit()]
    if len(real) == 1:
        return real[0]
    return None, None


def no_channel_guidance(guild_state: dict, action_cmd: str) -> str:
    """User-facing message when resolve_event_channel() can't pick a channel."""
    if count_countdown_channels(guild_state) == 0:
        return (
            "No countdown channel is set up yet.\n"
            "Run `/seteventchannel` in the channel where you want the countdown."
        )
    return (
        "This server has multiple countdown channels.\n"
        f"Run `{action_cmd}` **inside** the specific countdown channel you want."
    )


def get_user_links() -> dict:
    return state.setdefault("user_links", {})


def _today_local_date(tz: ZoneInfo = DEFAULT_TZ) -> date:
    return datetime.now(tz).date()


def calendar_days_left(dt: datetime, now: Optional[datetime] = None) -> int:
    if now is None:
        now = datetime.now(dt.tzinfo or DEFAULT_TZ)
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

def format_time_unit(total_seconds: int, unit: str) -> str:
    """Format a positive duration in the requested unit for pinned embed display."""
    days = total_seconds // 86400
    if unit == "days":
        return f"{days} day{'s' if days != 1 else ''}"
    if unit == "weeks":
        weeks = days // 7
        return f"{weeks} week{'s' if weeks != 1 else ''}"
    if unit == "detailed":
        months = days // 30
        rem = days % 30
        weeks = rem // 7
        rem_days = rem % 7
        parts = []
        if months:
            parts.append(f"{months} month{'s' if months != 1 else ''}")
        if weeks:
            parts.append(f"{weeks} week{'s' if weeks != 1 else ''}")
        if rem_days:
            parts.append(f"{rem_days} day{'s' if rem_days != 1 else ''}")
        if not parts:
            return f"{days} day{'s' if days != 1 else ''}"
        if len(parts) == 1:
            return parts[0]
        return ", ".join(parts[:-1]) + f" and {parts[-1]}"
    return f"{days} day{'s' if days != 1 else ''}"


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
    tz = get_guild_timezone(guild_state)
    if now is None:
        now = datetime.now(tz)

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
            dt = datetime.fromtimestamp(ts, tz=tz)
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
    # Skip if server has auto-delete disabled (Supporter perk)
    if not guild_state.get("auto_delete_milestones", True):
        return

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


# ==========================
# DISCORD SUBSCRIPTION CHECKING
# ==========================

async def check_discord_entitlements(guild_id: int) -> bool:
    """
    Check if a guild has an active Discord subscription for Chromie Pro.
    Uses Discord's entitlements API to check for active subscriptions.
    """
    try:
        # Get the SKU ID for Chromie Pro from environment
        SKU_ID = os.getenv("CHROMIE_PRO_SKU_ID", "").strip()
        
        if not SKU_ID:
            print("[ENTITLEMENTS] SKU ID not configured")
            return False
        
        # Get the bot's application ID
        app_id = bot.application.id if bot.application else None
        if not app_id:
            print("[ENTITLEMENTS] Could not get bot application ID")
            return False
        
        # Fetch entitlements for the guild using the REST API
        # This checks if the guild has an active subscription
        try:
            # Use the bot's HTTP client to get entitlements
            # Need to provide application_id, guild_id, sku_ids, and exclude_ended
            entitlements = await bot.http.get_entitlements(
                application_id=app_id,
                guild_id=guild_id,
                sku_ids=[int(SKU_ID)],
                exclude_ended=True  # Only get active subscriptions
            )
            
            print(f"[ENTITLEMENTS] Entitlements response for guild {guild_id}: {entitlements}")
            
            # If there are any entitlements, the guild has Pro
            if entitlements and len(entitlements) > 0:
                print(f"[ENTITLEMENTS] Found {len(entitlements)} active entitlements for guild {guild_id}")
                return True
            
            print(f"[ENTITLEMENTS] No active entitlements found for guild {guild_id}")
            return False
        except Exception as inner_e:
            print(f"[ENTITLEMENTS] get_entitlements failed: {inner_e}")
            import traceback
            traceback.print_exc()
            return False
    except Exception as e:
        print(f"[ENTITLEMENTS] Error checking entitlements for guild {guild_id}: {e}")
        import traceback
        traceback.print_exc()
        return False


async def sync_discord_subscription(guild_id: int) -> bool:
    """
    Check Discord's entitlements API and sync Pro status if subscription is found.
    Updates the guild state in JSON if subscription is active.
    Returns True if Pro is now active.
    """
    has_subscription = await check_discord_entitlements(guild_id)
    
    if has_subscription:
        # Guild has active subscription - update the state
        guild_state = get_guild_state(guild_id)
        now = datetime.now(timezone.utc)
        
        # Set Pro as active for a year (subscriptions auto-renew)
        if "pro" not in guild_state:
            guild_state["pro"] = {}
        
        guild_state["pro"]["pro_active"] = True
        # Set pro_until to 1 year from now (subscriptions renew automatically)
        guild_state["pro"]["pro_until"] = (now + timedelta(days=365)).isoformat()
        guild_state["pro"]["discord_subscription"] = True  # Mark as Discord subscription
        
        save_state()
        print(f"[ENTITLEMENTS] Synced Pro status for guild {guild_id}")
        return True
    
    return False


class ChromieBot(commands.Bot):
    async def setup_hook(self):
        try:
            # Safe-launch hook: if CHROMIE_TEST_GUILD_ID is set (local/test runs),
            # sync ONLY to that guild — instant, and it can never touch the global
            # command set on the 600+ live servers. Unset (production) → global sync,
            # exactly the original behavior.
            test_guild_id = os.getenv("CHROMIE_TEST_GUILD_ID", "").strip()
            if test_guild_id:
                guild = discord.Object(id=int(test_guild_id))
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                print(f"Slash commands synced to TEST guild {test_guild_id} "
                      f"({len(synced)} cmds; global tree untouched). [{VERSION}]")
            else:
                await self.tree.sync()
                print(f"Slash commands synced (global, setup_hook). [{VERSION}]")
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

# Of the recommended perms, only these actually BLOCK the countdown from posting.
# manage_messages (pin) and read_message_history only degrade it — the countdown
# still posts and updates, it just can't be pinned / can't auto-clean old pins.
CRITICAL_CHANNEL_PERMS = ("view_channel", "send_messages", "embed_links")


def classify_missing_perms(missing) -> tuple[list[str], list[str]]:
    """Split missing perm codes into (blocking, degraded). Blocking perms stop the
    countdown from posting at all; degraded perms only affect pinning / cleanup."""
    blocking = [p for p in missing if p in CRITICAL_CHANNEL_PERMS]
    degraded = [p for p in missing if p not in CRITICAL_CHANNEL_PERMS]
    return blocking, degraded


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
        f"Hey {mention}! Thanks for inviting **ChronoBot** to **{guild.name}** 🕒✨\n"
        "I'm **Chromie** — your server's confident little timekeeper. I pin a clean countdown list and post reminders "
        "so nobody has to do the mental math (or the panic).\n\n"
        "**⚡ Quick start (30 seconds):**\n"
        "1) In the channel you want the countdown: `/seteventchannel`\n"
        "2) Add an event: `/addevent date: 04/12/2026 time: 09:00 name: Game Night 🎲`\n"
        "3) Fine-tune anytime with the hubs below.\n\n"
        "**🧭 Core commands:**\n"
        "• `/listevents` • `/nextevent` • `/remindall`\n"
        "• `/event` — manage events (edit, delete, milestones, owner, repeat, banner, silence)\n"
        "• `/countdown` — channel settings (theme, timezone, time format, mention role, digest)\n\n"
        "**🔔 Reminders & mentions:**\n"
        f"Milestone reminders post in your countdown channel ({milestone_str} by default). "
        "Each channel has its own timezone (set it in `/countdown`).\n"
        "Role pings, owner assignment, and per-event owner DMs all live in `/event` and `/countdown`.\n\n"
        "**🎨 Make it yours:**\n"
        "Pick from **19 themes** — sports, cute, spooky, birthday, wedding, game launch & more — and "
        "**preview before you apply** in `/countdown` → Theme.\n"
        "Your **first countdown channel is free**; Chromie Pro unlocks a separate countdown in *every* channel.\n\n"
        "**🛠️ Troubleshooting:**\n"
        "Run `/healthcheck` — it shows each countdown channel + whether I can view/send/embed/read history/pin.\n"
        "(Past events auto-remove after they pass so the list stays tidy.)\n\n"
        "**More help:** `/chronohelp` (check out Tiers & Pricing!)\n\n"
        f"FAQ: {FAQ_URL}\n"
        f"Support server: {SUPPORT_SERVER_URL}\n\n"
        "Alright — I'll be over here, politely bullying time into behaving. 💜"
    )

    # -----------------------------
    # Message 2: Tiers & Supporter features
    # -----------------------------
    supporter_message = (
        "**🎯 Your tier: Free (3 events max)**\n"
        "• 🆓 **Free:** 3 events, 1 countdown channel, all core features\n"
        "• ⭐ **Supporter:** 5 events, premium themes, banners (vote on Top.gg - free!)\n"
        "• 💎 **Pro:** Unlimited events, countdowns in every channel, build-your-own themes & more ($2.99/mo)\n"
        "Run `/vote` to unlock Supporter, or subscribe for Pro via our Store!\n\n"
        "**⭐ Supporter Tier (Vote to unlock - Free!)**\n"
        "Chromie is free. Voting on Top.gg helps it grow — and unlocks bonus features for 12 hours.\n\n"
        "**What you get:**\n"
        "• 5 events (vs 3 for Free)\n"
        "• 19 premium themes incl. seasonal drops — preview before you apply (`/countdown` → Theme)\n"
        "• Custom event banners (`/event` → Banner)\n\n"
        "**How to unlock:**\n"
        "Run `/vote` to get the link. Voting takes 10 seconds and helps Chromie grow!\n\n"
        "**💎 Chromie Pro ($2.99/month)**\n"
        "For power users who need unlimited events + advanced automation:\n"
        "• ♾️ Unlimited events (no cap)\n"
        "• 📌 A separate countdown in *every* channel (each its own events/theme/timezone)\n"
        "• 🎨 Build-your-own custom themes — your colors, emoji, title & footer (`/countdown` → Build-your-own)\n"
        "• 🔁 Recurring event reminders (`/event` → Repeat)\n"
        "• 📋 Event templates (`/template save`, `/template load`)\n"
        "• 📑 Duplicate events (`/event` → Duplicate)\n"
        "• 📊 Weekly digest summaries (`/countdown` → Weekly digest)\n"
        "• 🎨 All Supporter features (permanent)\n"
        "• 🏆 Priority support\n\n"
        "Subscribe via my Discord Store. Pro unlocks for your entire server!\n\n"
        "If anything seems stuck after voting, run `/vote` again (Top.gg can take a moment to reflect your vote)."
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

    # Fallback: post in a channel (base message only to avoid "promo spam" vibe)
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
    
    # Post server count to Top.gg
    bot_id = get_topgg_bot_id()
    if TOPGG_TOKEN and bot_id:
        try:
            server_count = len(bot.guilds)
            url = f"{TOPGG_API_BASE}/bots/{bot_id}/stats"
            headers = {"Authorization": TOPGG_TOKEN.strip()}
            payload = {"server_count": server_count}
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status == 200:
                        print(f"✅ Posted {server_count} servers to Top.gg")
                    else:
                        print(f"⚠️ Top.gg stats post failed: {resp.status}")
        except Exception as e:
            print(f"⚠️ Error posting to Top.gg: {e}")


@bot.event
async def on_guild_join(guild: discord.Guild):
    g_state = get_guild_state(guild.id)
    sort_events(g_state)
    save_state()
    await send_onboarding_for_guild(guild)
    
    # Post updated server count to Top.gg
    bot_id = get_topgg_bot_id()
    if TOPGG_TOKEN and bot_id:
        try:
            server_count = len(bot.guilds)
            url = f"{TOPGG_API_BASE}/bots/{bot_id}/stats"
            headers = {"Authorization": TOPGG_TOKEN.strip()}
            payload = {"server_count": server_count}
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status == 200:
                        print(f"✅ Updated Top.gg: now in {server_count} servers")
        except Exception as e:
            print(f"⚠️ Error updating Top.gg on guild join: {e}")


@bot.event
async def on_guild_remove(guild: discord.Guild):
    """Called when the bot leaves a guild (kicked, left, or guild deleted)"""
    # Post updated server count to Top.gg
    bot_id = get_topgg_bot_id()
    if TOPGG_TOKEN and bot_id:
        try:
            server_count = len(bot.guilds)
            url = f"{TOPGG_API_BASE}/bots/{bot_id}/stats"
            headers = {"Authorization": TOPGG_TOKEN.strip()}
            payload = {"server_count": server_count}
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status == 200:
                        print(f"✅ Updated Top.gg: now in {server_count} servers")
        except Exception as e:
            print(f"⚠️ Error updating Top.gg on guild remove: {e}")


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
            "2) Add an event: `/addevent`\n"
            "3) Chromie keeps the pinned countdown updated.\n\n"
            "**🎯 Event Limits by Tier:**\n"
            "🆓 Free: 3 events • ⭐ Supporter: 5 events • 💎 Pro: Unlimited\n"
            "Run `/vote` to unlock Supporter (free!), or subscribe to Pro ($2.99/mo) for unlimited events + advanced features.\n\n"
            "Use the dropdown to browse commands by category."
        ),
        "lines": [
            "`/seteventchannel` — turn this channel into a countdown channel",
            "`/addevent` — add an event",
            "`/event` — manage events (edit, milestones, owner, repeat, …)",
            "`/countdown` — channel settings (theme, timezone, format, …)",
            "`/vote` — check your tier + unlock Supporter",
        ],
    },
    "events": {
        "title": "Events",
        "desc": "Create, view, and manage events. Everything per countdown channel.",
        "lines": [
            "`/addevent` — add an event (fast)",
            "`/listevents` — list this channel's events",
            "`/nextevent` — show next upcoming event",
            "`/event` — open the event manager:",
            "   • edit name / date / time, delete",
            "   • milestones, reminder time, silence",
            "   • owner + owner-DM opt-in",
            "   • 💎 duplicate, 💎 repeat, banner",
        ],
    },
    "reminders": {
        "title": "Reminders",
        "desc": "Milestones, manual reminders, repeats, reminder times — all in `/event`.",
        "lines": [
            "`/remindall` — post a reminder now (defaults to next event)",
            "`/event` → Milestones — custom milestone days / reset",
            "`/event` → Reminder time — day-of reminder time / clear",
            "`/event` → Silence — pause an event's reminders",
            "`/event` → Repeat — 💎 recurring reminders (Pro)",
            "`/countdown` → Timezone — set this channel's timezone",
            "`/countdown` → Mention role — who gets pinged",
        ],
    },
    "customize": {
        "title": "Customization",
        "desc": "Make each channel's countdown match your vibe — all in `/countdown`.",
        "lines": [
            "`/countdown` → Theme — change the countdown theme",
            "`/countdown` → Time format — discord / days / weeks / detailed",
            "`/countdown` → Title / Description — 💎 custom embed text (Pro)",
            "`/countdown` → Weekly digest — 💎 weekly summary (Pro)",
            "`/event` → Banner — per-event banner image",
            "`/template save` / `/template load` — 💎 event templates (Pro)",
        ],
    },
    "owner": {
        "title": "Owner DMs",
        "desc": "Assign an owner to an event and opt them into reminder DMs (off by default).",
        "lines": [
            "`/event` → Owner — assign or clear the event owner",
            "`/event` → Owner DMs — toggle DM reminders for that owner",
        ],
    },
    "tiers": {
        "title": "Tiers & Pricing 💎",
        "desc": "Chromie has three tiers to fit your needs.",
        "lines": [
            "**🆓 Free Tier (Always free!)**",
            "• Up to 3 events",
            "• All core countdown features",
            "• Milestone reminders",
            "• Event management (add/edit/delete)",
            "• 14 premium themes",
            "",
            "**⭐ Supporter Tier (Vote to unlock - Free!)**",
            "• Up to 5 events",
            "• Everything in Free",
            "• Custom event banners (`/event` → Banner)",
            "• Advanced milestone customization",
            "• Unlock by voting on Top.gg (lasts 12 hours)",
            "• Run `/vote` to get the link!",
            "",
            "**💎 Chromie Pro ($2.99/month)**",
            "• Unlimited events",
            "• Everything in Supporter (permanent)",
            "• Recurring event reminders (`/event` → Repeat)",
            "• Event templates (`/template save` + `/template load`)",
            "• Duplicate events (`/event` → Duplicate)",
            "• Weekly digest summaries (`/countdown` → Weekly digest)",
            "• Multiple countdown boards",
            "• Priority support",
            "• Subscribe via Discord Server Subscription",
        ],
    },
    "supporter": {
        "title": "Supporter Tier ⭐",
        "desc": "Vote on Top.gg to unlock Supporter features for 12 hours (completely free!).",
        "lines": [
            "**What you get:**",
            "• 5 events (vs 3 for Free)",
            "• Custom themes (`/countdown` → Theme)",
            "• Event banners (`/event` → Banner)",
            "• Advanced milestone customization",
            "",
            "**How to unlock:**",
            "1) Run `/vote` to get the Top.gg link",
            "2) Click the link and vote (takes 10 seconds)",
            "3) Supporter unlocks for 12 hours",
            "4) Vote again after 12h to keep access!",
            "",
            "**Commands:**",
            "`/vote` — check status + get vote link",
            "`/vote_debug` — admin troubleshooting",
            "",
            "**Want more? Upgrade to Chromie Pro:**",
            "$2.99/month • Unlimited events • Recurring reminders",
            "Templates • Digest • Event duplication",
            "Subscribe via Discord Server Subscription",
        ],
    },
    "pro": {
        "title": "Chromie Pro 💎",
        "desc": "Premium features for power users. $2.99/month via Discord subscription.",
        "lines": [
            "**What's included:**",
            "• ♾️ Unlimited events (no cap!)",
            "• 🔁 Recurring event reminders (`/event` → Repeat)",
            "• 📋 Event templates (`/template save`, `/template load`)",
            "• 📑 Duplicate events (`/event` → Duplicate)",
            "• 📊 Weekly digest (`/countdown` → Weekly digest)",
            "• 🎨 All Supporter features (permanent, no voting needed)",
            "• 📌 Multiple countdown boards per server",
            "• 🏆 Priority support",
            "",
            "**How to subscribe:**",
            "Subscribe via your server's Discord Server Subscription.",
            "Pro unlocks for the ENTIRE server — all admins get Pro features.",
            "",
            "**Price:** $2.99/month per server",
            "Cancel anytime via Discord subscription settings.",
        ],
    },
    "maintenance": {
        "title": "Maintenance",
        "desc": "Config resets + cleanup + diagnostics.",
        "lines": [
            "`/healthcheck` — per-channel permissions/config",
            "`/update_countdown` — force-refresh this channel's pin",
            "`/resendsetup` — resend onboarding",
            "`/countdown` → Remove — stop a channel's countdown",
            "`/purgeevents confirm: YES` — delete this channel's events",
            "`/archivepast` — remove past events (all channels)",
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
    ("Tiers & Pricing", "tiers", "Free, Supporter, Pro"),
    ("Supporter", "supporter", "Vote perks"),
    ("Chromie Pro", "pro", "$2.99/mo features"),
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




# ---- THEMES (pool-based) - PREMIUM EDITION ----
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

THEMES: Dict[str, Dict[str, Any]] = {
    "classic": {
        "label": "Chrono Purple (Classic)",
        "supporter_only": False,
        "color": EMBED_COLOR,
        "pin_title_pool": ["⏳ **CHRONO COUNTDOWN** • Timeline Tracker", "💜 **PURPLE POWER** • Event Board", "🕒 **COUNTDOWN CENTRAL** • Timelines Sync", "✨ **SPARKLE TIMER** • Events Incoming", "⌛ **HOURGLASS MODE** • Schedule Locked", "🔔 **BELL ALERT** • Next Up", "📌 **PIN POINT** • Event Calendar"],
        "event_emoji_pool": ["🕒", "⏳", "⌛", "💜", "✨", "🔔", "📌", "⏰"],
        "milestone_emoji_pool": ["⏳", "🕒", "🔔", "✨", "💜", "⏰"],
        "milestone_templates": {
            "default": ["{emoji} ✨ **Countdown Check:** {event} in **{days} days** ({time_left}) • {date}", "{emoji} 📍 **Timeline Update:** {event} — **{days} days** on the horizon ({time_left})", "{emoji} ⏳ **Time Ticking:** {event} arrives in **{days} days** ({time_left}) • {date}", "{emoji} 🔔 **Heads Up:** {event} is **{days} days** away ({time_left}) • {date}", "{emoji} 💜 **Purple Vibes:** {event} in **{days} days**—mark your calendar ({time_left})", "{emoji} 📌 **Calendar Alert:** {event} — **{days} days** and counting ({time_left}) • {date}", "{emoji} ✨ **Sparkle Countdown:** {event} in **{days} days**—magic pending ({time_left})", "{emoji} 🕒 **Clock Says:** {event} in **{days} days** • Time flows ({time_left}) • {date}"],
            "one_day": ["{emoji} ⏰ **TOMORROW AWAITS:** {event} is tomorrow 🌙 ({time_left}) • {date}", "{emoji} 💜 **24 HOURS LEFT:** {event} — the countdown hits home ({time_left})", "{emoji} ✨ **ALMOST HERE:** {event} tomorrow • Get ready ({time_left}) • {date}", "{emoji} 📌 **FINAL STRETCH:** {event} arrives tomorrow ({time_left})", "{emoji} 🔔 **BELL RINGS TOMORROW:** {event} • One more sleep ({time_left})", "{emoji} ⏳ **LAST GRAIN OF SAND:** {event} tomorrow—the moment approaches ({time_left})"],
            "zero_day": ["{emoji} 💜 **IT'S TODAY:** {event} **IS LIVE NOW** 🎉 ({time_left}) • {date}", "{emoji} ✨ **MOMENT OF TRUTH:** {event} **STARTS TODAY**—Here we go! ({time_left})", "{emoji} 🕒 **HANDS AT ZERO:** {event} **RIGHT NOW** ({time_left}) • {date}", "{emoji} ⏰ **TIME'S UP (THE GOOD KIND):** {event} **TODAY** 💜 ({time_left})", "{emoji} 🔔 **BELL TOLLS:** {event} is **NOW**—The wait is over ({time_left})"],
        },
        "repeat_templates": ["{emoji} 🔁 **CYCLE COMPLETE:** {event} returns in **{time_left}** • {date}", "{emoji} 🔁 **LOOPING TIMELINE:** {event} in **{time_left}** ({date})", "{emoji} 🔁 **RECURRING MAGIC:** {event} repeats in **{time_left}** 💜 • {date}", "{emoji} 🔁 **THE WHEEL TURNS:** {event} again in **{time_left}** • {date}", "{emoji} 🔁 **NEXT CYCLE:** {event} in **{time_left}** ({date})"],
        "remindall_templates": ["{emoji} 📣 **REMINDER:** {event} in **{time_left}** • {date}", "{emoji} 🔔 **DON'T SLEEP:** {event} in **{time_left}** 💜 • {date}", "{emoji} ✨ **SPARKLE ALERT:** {event} in **{time_left}** ({date})", "{emoji} 📌 **MARK YOUR CALENDAR:** {event} in **{time_left}** • {date}", "{emoji} ⏳ **TIME MARCHES:** {event} in **{time_left}** • {date}"],
        "start_blast_templates": ["💜 ✨ **THE MOMENT IS HERE!** ✨ **{event} IS LIVE NOW—LET'S GO!**", "⏰ **TIME'S UP (THE GOOD KIND):** {event} **STARTS NOW** 🎉", "🕒 **HANDS AT ZERO:** {event} **BEGINS NOW**—Sparkle mode activated ✨", "🔔 **BELL TOLLS:** {event} **TODAY**—The countdown was worth it 💜", "✨ **DREAM TIME:** {event} **RIGHT NOW**—Shine bright 🌟"],
    },
    "football": {
        "label": "Football",
        "supporter_only": True,
        "color": discord.Color.from_rgb(20, 160, 90),
        "pin_title_pool": ["🏈 **PLAY CLOCK** • Kickoff Counter", "📺 **GAME ALERT** • Stadium Status", "🏟️ **NEXT UP** • Sunday Schedule", "📊 **THE SCOREBOARD** • Upcoming Matchups", "⏱️ **TICK TOCK** • Kickoff in T-Minus", "🎙️ **BROADCAST ALERT** • Next Game Incoming", "🔔 **PULL UP THE SCHEDULE** • Matchday Ahead"],
        "event_emoji_pool": ["🏈", "🏟️", "📣", "🧢", "🔥", "⏱️", "📺", "🎙️"],
        "milestone_emoji_pool": ["🏈", "📣", "⏱️", "🏟️", "🔥", "🎙️"],
        "milestone_templates": {
            "default": ["{emoji} 📺 **BROADCAST:** {event} kickoff in **{days} days** ({time_left}) • {date}", "{emoji} 🎙️ **FROM THE BOOTH:** {event} — **{days} days** on the clock ({time_left})", "{emoji} 📢 **PLAY-BY-PLAY:** Game incoming — {event} in **{days} days** ({time_left}) • {date}", "{emoji} 🏟️ **STADIUM COUNTDOWN:** {event} in **{days} days** ({time_left})", "{emoji} ⏱️ **CLOCK'S TICKING:** {event} scheduled **{days} days** out ({time_left}) • {date}", "{emoji} 🔥 **HYPE METER:** {event} is **{days} days** away—lockdown mode ({time_left})", "{emoji} 📊 **MATCHUP ALERT:** {event} — **{days} days** until we play ({time_left}) • {date}", "{emoji} 🧢 **TURF REPORT:** {event} in **{days} days** • Game plan ready ({time_left})"],
            "one_day": ["{emoji} 🏈 **TOMORROW: GAMEDAY LOCKED IN:** {event} kicks off ({time_left}) • {date}", "{emoji} 📺 **FINAL COUNTDOWN:** {event} **TOMORROW** • Showtime ({time_left})", "{emoji} 🔥 **PREGAME ENERGY:** {event} tomorrow • Get ready 🏟️ ({time_left}) • {date}", "{emoji} 🎙️ **ONE DAY TO KICKOFF:** {event} — tomorrow we fight ({time_left})", "{emoji} ⏱️ **24 HOURS TO GAME TIME:** {event} ({time_left}) • {date}", "{emoji} 📢 **LAST CALL:** {event} tomorrow—final prep ({time_left})", "{emoji} 🏟️ **STADIUM OPENS TOMORROW:** {event} 🎟️ ({time_left})"],
            "zero_day": ["{emoji} 🏈 **GAMEDAY IS LIVE:** {event} **RIGHT NOW** ({time_left}) • {date}", "{emoji} 📺 **ON THE AIR:** {event} **STARTS NOW** 🔴 ({time_left})", "{emoji} 🔥 **IT'S HAPPENING:** {event} **TODAY**—LET'S GOOOO! ({time_left}) • {date}", "{emoji} ⏱️ **NO TIMEOUTS LEFT:** {event} **TODAY**—Show time! ({time_left})", "{emoji} 🎙️ **LIVE FROM THE FIELD:** {event} **NOW** 🏟️ ({time_left})"],
        },
        "repeat_templates": ["{emoji} 🔁 **NEXT DRIVE:** {event} returns in **{time_left}** 🏈 • {date}", "{emoji} 🔁 **REMATCH SCHEDULED:** {event} again in **{time_left}** • {date}", "{emoji} 🔁 **SAME TIME NEXT WEEK:** {event} in **{time_left}** ({date})", "{emoji} 🔁 **PLAYOFFS INCOMING:** Next {event} in **{time_left}** 🏆 • {date}", "{emoji} 🔁 **BACK ON THE FIELD:** {event} cycles in **{time_left}** • {date}"],
        "remindall_templates": ["{emoji} 📣 **REMINDER:** {event} in **{time_left}** 🏈 • {date}", "{emoji} 📺 **DON'T MISS IT:** {event} in **{time_left}** • {date}", "{emoji} 🎙️ **HEADS UP:** {event} **{time_left}** away • {date}", "{emoji} ⏱️ **TICK TOCK:** {event} in **{time_left}** • Game time ({date})", "{emoji} 🏟️ **STADIUM ALERT:** {event} in **{time_left}** ({date})"],
        "start_blast_templates": ["🏈 🎙️ **AND THERE'S THE KICKOFF!** 🏈 **{event} IS LIVE RIGHT NOW!**", "📺 **BROADCAST:** {event} **STARTS NOW** 🔴 • TUNE IN 📺", "⏰ **GAME DAY LOCKED IN:** {event} **LIVE**—LET'S GOOOO 🔥🏟️", "🏟️ **FROM THE FIELD:** {event} **KICKS OFF**—Main character energy ⚡", "📢 **LIVE ALERT:** {event} **JUST STARTED** • Watch live 🏈🔥", "🧢 **TURF TIME:** {event} **LIVE NOW**—No delays allowed 🏈"],
    },
    "basketball": {
        "label": "Basketball",
        "supporter_only": True,
        "color": discord.Color.from_rgb(255, 130, 20),
        "pin_title_pool": ["🏀 **SHOT CLOCK** • Tip-Off Incoming", "⏱️ **COURT TIME** • Next Matchup", "🔥 **CLUTCH HOUR** • Game Board", "🏟️ **ARENA LOCKED** • Upcoming Games", "📣 **BUZZER BEATER** • Schedule Alert", "⛹️ **FAST BREAK** • Countdown Live", "🎯 **DUNK ALERT** • Action Coming"],
        "event_emoji_pool": ["🏀", "⛹️", "🔥", "⏱️", "📣", "🏟️", "🎯", "🏆"],
        "milestone_emoji_pool": ["🏀", "⏱️", "🔥", "📣", "🏟️"],
        "milestone_templates": {
            "default": ["{emoji} 🎯 **COURT ALERT:** {event} tips off in **{days} days** ({time_left}) • {date}", "{emoji} ⏱️ **SHOT CLOCK:** {event} — **{days} days** until game time ({time_left})", "{emoji} 🏟️ **ARENA COUNTDOWN:** {event} in **{days} days** • Warmups coming ({time_left}) • {date}", "{emoji} 📣 **BUZZER:** {event} next possession in **{days} days** ({time_left})", "{emoji} 🔥 **HEAT CHECK:** {event} incoming in **{days} days**—momentum building ({time_left})", "{emoji} ⛹️ **FAST BREAK:** {event} in **{days} days** • Get in position ({time_left}) • {date}", "{emoji} 🏀 **BALL MOVEMENT:** {event} scheduled **{days} days** ahead ({time_left})", "{emoji} 🎯 **NOTHING BUT NET:** {event} in **{days} days**—time to ball ({time_left}) • {date}"],
            "one_day": ["{emoji} 🏀 **TIP-OFF TOMORROW:** {event} — one more sleep 🌙 ({time_left}) • {date}", "{emoji} ⏱️ **SHOT CLOCK TOMORROW:** {event} • Lace up ({time_left})", "{emoji} 🔥 **CLUTCH TIME INCOMING:** {event} tomorrow—show them hoops ({time_left}) • {date}", "{emoji} 🏟️ **ARENA DOORS OPEN TOMORROW:** {event} 🎟️ ({time_left})", "{emoji} 📣 **FINAL BUZZER WARNING:** {event} tomorrow—be there ({time_left})", "{emoji} ⛹️ **FAST BREAK LOADING:** {event} tomorrow—speed drills ready ({time_left}) • {date}"],
            "zero_day": ["{emoji} 🏀 **TIP-OFF IS NOW:** {event} **LIVE** ({time_left}) • {date}", "{emoji} 🎯 **NOTHING BUT NET:** {event} **STARTS NOW**—Ball's in the air! 🔥", "{emoji} ⏱️ **SHOT CLOCK LIVE:** {event} **TODAY**—Game time! ({time_left}) • {date}", "{emoji} 🏟️ **ARENA PACKED:** {event} **RIGHT NOW** • Watch it unfold 🏀", "{emoji} 📣 **FINAL BUZZER RINGS:** {event} **NOW**—Showtime! ({time_left})"],
        },
        "repeat_templates": ["{emoji} 🔁 **NEXT GAME:** {event} returns in **{time_left}** 🏀 • {date}", "{emoji} 🔁 **REMATCH BOOKED:** {event} again in **{time_left}** • {date}", "{emoji} 🔁 **SEASON CONTINUES:** {event} in **{time_left}** 🏟️ • {date}", "{emoji} 🔁 **HOOPS CYCLE:** {event} repeats in **{time_left}** • {date}", "{emoji} 🔁 **BACK ON COURT:** {event} in **{time_left}** ({date})"],
        "remindall_templates": ["{emoji} 📣 **COURT REMINDER:** {event} in **{time_left}** 🏀 • {date}", "{emoji} 🏟️ **ARENA ALERT:** {event} in **{time_left}** • {date}", "{emoji} 🎯 **GAME INCOMING:** {event} in **{time_left}** 🔥 • {date}", "{emoji} ⏱️ **SHOT CLOCK TICKING:** {event} in **{time_left}** ({date})", "{emoji} ⛹️ **WARMUPS SOON:** {event} in **{time_left}** • {date}"],
        "start_blast_templates": ["🏀 ⛹️ **TIP-OFF!** ⛹️ **{event} IS LIVE—LET'S HOOP!**", "🎯 **NOTHING BUT NET:** {event} **STARTS NOW** 🔥 • Game time", "⏱️ **SHOT CLOCK LIVE:** {event} **BEGINS NOW**—Ball in the air! 🏀", "🏟️ **ARENA ERUPTS:** {event} **RIGHT NOW**—Showtime! 📣", "🔥 **CLUTCH MOMENT:** {event} **LIVE**—This is it! 🏆", "📣 **BUZZER SOUNDS:** {event} **NOW**—Get ready to hoop 🏀"],
    },
    "baseball": {
        "label": "Baseball",
        "supporter_only": True,
        "color": discord.Color.from_rgb(10, 50, 120),
        "pin_title_pool": ["⚾ **DIAMOND DUTY** • Game Day Tracker", "🧢 **DUGOUT ALERT** • First Pitch Coming", "🏟️ **BALLPARK BOARD** • Next Matchup", "🔥 **BOTTOM OF THE 9TH** • Clock Ticking", "📣 **PLAY BALL** • Schedule Active", "🧤 **GLOVE TIME** • Incoming Games", "⚾ **HOMERUN COUNTDOWN** • Events Loaded"],
        "event_emoji_pool": ["⚾", "🧢", "🏟️", "🔥", "🧤", "📣", "⚾", "🏆"],
        "milestone_emoji_pool": ["⚾", "🧢", "🏟️", "🔥", "🧤"],
        "milestone_templates": {
            "default": ["{emoji} ⚾ **BALLPARK ALERT:** {event} starts in **{days} days** ({time_left}) • {date}", "{emoji} 📣 **PLAY BALL COUNTDOWN:** {event} — **{days} days** on the diamond ({time_left})", "{emoji} 🧤 **GLOVE CHECK:** {event} in **{days} days** • Get ready to field ({time_left}) • {date}", "{emoji} 🧢 **DUGOUT TIMER:** {event} scheduled **{days} days** out ({time_left})", "{emoji} 🏟️ **STADIUM BOUND:** {event} in **{days} days**—grab your ticket ({time_left}) • {date}", "{emoji} 🔥 **PITCHER'S MOUND:** {event} incoming in **{days} days** ({time_left})", "{emoji} 🏆 **TROPHY TIME:** {event} — **{days} days** until the big game ({time_left}) • {date}", "{emoji} ⚾ **HOMERUN INCOMING:** {event} in **{days} days**—mark your calendar ({time_left})"],
            "one_day": ["{emoji} ⚾ **GAMEDAY TOMORROW:** {event} — one more night 🌙 ({time_left}) • {date}", "{emoji} 🧢 **DUGOUT READY:** {event} tomorrow—final warmup ({time_left})", "{emoji} 🏟️ **BALLPARK OPENS TOMORROW:** {event} • First pitch incoming ({time_left}) • {date}", "{emoji} 📣 **PLAY BALL TOMORROW:** {event} — get your gear ready ({time_left})", "{emoji} 🔥 **PITCHER PREPS:** {event} tomorrow • Game on ({time_left}) • {date}", "{emoji} 🧤 **GLOVE READY:** {event} tomorrow—the diamond awaits ({time_left})"],
            "zero_day": ["{emoji} ⚾ **FIRST PITCH IS NOW:** {event} **LIVE** ({time_left}) • {date}", "{emoji} 📣 **PLAY BALL:** {event} **STARTS NOW**—We're on the diamond! 🔥", "{emoji} 🧤 **GAME TIME:** {event} **TODAY**—Show them your glove! ({time_left})", "{emoji} 🏟️ **BALLPARK PACKED:** {event} **RIGHT NOW** • Let's go ⚾", "{emoji} 🧢 **DUGOUT SPRINGS TO LIFE:** {event} **NOW**—It's happening! ({time_left})"],
        },
        "repeat_templates": ["{emoji} 🔁 **NEXT GAME:** {event} returns in **{time_left}** ⚾ • {date}", "{emoji} 🔁 **REMATCH SCHEDULED:** {event} again in **{time_left}** • {date}", "{emoji} 🔁 **SEASON CONTINUES:** {event} in **{time_left}** 🏟️ • {date}", "{emoji} 🔁 **DIAMOND CYCLE:** {event} repeats in **{time_left}** • {date}", "{emoji} 🔁 **BACK ON THE DIAMOND:** {event} in **{time_left}** ({date})"],
        "remindall_templates": ["{emoji} 📣 **BALLPARK REMINDER:** {event} in **{time_left}** ⚾ • {date}", "{emoji} 🧢 **DUGOUT ALERT:** {event} in **{time_left}** • {date}", "{emoji} 🧤 **GLOVE UP:** {event} in **{time_left}** 🏟️ • {date}", "{emoji} 🏆 **TROPHY TIME:** {event} in **{time_left}** ({date})", "{emoji} 🔥 **HOT TIME:** {event} in **{time_left}** • {date}"],
        "start_blast_templates": ["⚾ 📣 **PLAY BALL!** 📣 **{event} STARTS NOW—WE'RE ON THE DIAMOND!**", "🧢 **FIRST PITCH:** {event} **LIVE NOW** ⚾ • Game on", "🏟️ **BALLPARK ERUPTS:** {event} **BEGINS NOW**—Let's go! 🔥", "📣 **NEXT BATTER READY:** {event} **STARTS NOW** • Show your stuff ⚾", "🧤 **GLOVE TIME:** {event} **RIGHT NOW**—Defense takes the field! 🏆", "🔥 **BOTTOM OF THE 9TH:** {event} **NOW**—Championship energy! ⚾"],
    },
    "raidnight": {
        "label": "Raid Night",
        "supporter_only": True,
        "color": discord.Color.from_rgb(180, 120, 255),
        "pin_title_pool": ["⚔️ **READY CHECK** • Pull Timer Active", "🛡️ **GUILD ALERT** • Next Raid Posted", "🎮 **OBJECTIVE HUD** • Boss Queue Status", "🏆 **LOOT TRACKER** • Upcoming Raid Night", "🎯 **DPS METER** • Encounter Locked", "💎 **TREASURE TIMER** • Guild Gathering Soon", "🪙 **GOLD RUSH** • Farming Time Incoming"],
        "event_emoji_pool": ["🎮", "🛡️", "⚔️", "🧩", "🗡️", "🪙", "🏆", "💎", "🎯"],
        "milestone_emoji_pool": ["🛡️", "⚔️", "🎮", "🔔", "🏆", "💎"],
        "milestone_templates": {
            "default": ["{emoji} 🎮 **QUEUE ALERT:** {event} in **{days} days** • Prepping ({time_left}) • {date}", "{emoji} 🛡️ **BUFF CHECK:** {event} — **{days} days** until lockdown ({time_left})", "{emoji} ⚔️ **RAID NOTICE:** {event} incoming in **{days} days**—stockpile consumables ({time_left}) • {date}", "{emoji} 🏆 **LOOT DROP:** {event} — **{days} days** until we farm ({time_left})", "{emoji} 💎 **TREASURE ALERT:** {event} in **{days} days** • Drops incoming ({time_left}) • {date}", "{emoji} 🎯 **OBJECTIVE LOCKED:** {event} in **{days} days**—DPS check incoming ({time_left})", "{emoji} 🪙 **GOLD INCOMING:** {event} — **{days} days** until payday ({time_left}) • {date}", "{emoji} 🧩 **PUZZLE PIECE:** {event} coming in **{days} days** • Prepare your role ({time_left})"],
            "one_day": ["{emoji} ⚔️ **RAID TOMORROW—READY CHECK!** {event} ({time_left}) • {date}", "{emoji} 🛡️ **CONSUMABLES LOCKED:** {event} tomorrow—buffed up ({time_left})", "{emoji} 🎮 **ONE DAY TO BOSS:** {event} tomorrow—final prep ({time_left}) • {date}", "{emoji} 🏆 **LOOT TOMORROW:** {event} — lock in your gear ({time_left})", "{emoji} 💎 **TREASURE AWAITS TOMORROW:** {event} 💎 ({time_left}) • {date}", "{emoji} 🎯 **DPS CHECK TOMORROW:** {event} • Get your numbers ready ({time_left})"],
            "zero_day": ["{emoji} ⚔️ **IT'S RAID DAY:** {event} **LIVE NOW** 🔴 ({time_left}) • {date}", "{emoji} 🛡️ **READY CHECK:** {event} **STARTS NOW**—Party up! ({time_left})", "{emoji} 🎮 **BOSS IS UP:** {event} **BEGINS NOW**—Loot time 🏆 ({time_left})", "{emoji} 🎯 **PULL TIMER AT ZERO:** {event} **NOW**—No deaths! ({time_left}) • {date}", "{emoji} 💎 **LOOT DOOR OPENS:** {event} **TODAY**—Grind time 💎 ({time_left})"],
        },
        "repeat_templates": ["{emoji} 🔁 **RESET COMPLETE:** {event} returns in **{time_left}** 🔄 • {date}", "{emoji} 🔁 **NEXT RUN:** {event} in **{time_left}** • Guild gathers again • {date}", "{emoji} 🔁 **FARM RESUMES:** {event} cycles in **{time_left}** 💎 ({date})", "{emoji} 🔁 **RAID RESET POSTED:** {event} in **{time_left}** • Fresh drops incoming", "{emoji} 🔁 **GUILD GATHERS AGAIN:** {event} in **{time_left}** • Grind time ({date})"],
        "remindall_templates": ["{emoji} 📣 **RAID REMINDER:** {event} in **{time_left}** • Don't AFK 🚨 • {date}", "{emoji} 🎮 **GUILD ALERT:** {event} in **{time_left}** • Get buffed ({date})", "{emoji} ⚔️ **COMBAT INCOMING:** {event} in **{time_left}** • Locked in 🔒", "{emoji} 🏆 **LOOT CHECK:** {event} in **{time_left}** 💎 • {date}", "{emoji} 🎯 **DPS TIMER:** {event} in **{time_left}** • No wipes ({date})"],
        "start_blast_templates": ["⚔️ 🛡️ **READY CHECK COMPLETE!** 🛡️ **{event} IS LIVE—LET'S RAID!**", "🎮 **QUEUE POPPED:** {event} **STARTS NOW** 🔴 • Everyone inside", "🏆 **BOSS DOWN:** {event} **BEGINS NOW**—Farm loot 💎", "💎 **TREASURE DOOR OPENS:** {event} **LIVE NOW** • Get that gold ⚔️", "🎯 **PULL TIMER READY:** {event} **STARTS NOW**—No deaths! 🛡️", "🪙 **GOLD RUSH:** {event} **LIVE**—Let the farming begin 💰"],
    },
    "dnd": {
        "label": "D&D Campaign Night",
        "supporter_only": True,
        "color": discord.Color.from_rgb(170, 130, 80),
        "pin_title_pool": ["🐉 **QUEST LOG** • The Party Gathers", "🎲 **SESSION TIMER** • Next Campaign Chapter", "📜 **THE PROPHECY** • Adventure Awaits", "🕯️ **TAVERN BELL** • When We Return", "🗺️ **THE EXPEDITION** • Brave New Worlds", "🔮 **FATE'S COUNTDOWN** • Destiny Incoming", "⚔️ **THE FELLOWSHIP** • Heroes Rise Again"],
        "event_emoji_pool": ["🎲", "🐉", "📜", "🕯️", "🗺️", "🗡️", "🛡️", "🔮", "⚔️"],
        "milestone_emoji_pool": ["🎲", "🐉", "📜", "🕯️", "🗡️", "🔮"],
        "milestone_templates": {
            "default": ["{emoji} 📖 **DM's Notes:** {event} resumes in **{days} days** ({time_left}) • {date}", "{emoji} 🔮 **The Prophecy:** {event} arrives in **{days} days** ({time_left}) • {date}", "{emoji} 🗺️ **Quest Board:** {event} posted—**{days} days** until departure ({time_left})", "{emoji} 🕯️ **Tavern Gathering:** {event} — party meets in **{days} days** ({time_left}) • {date}", "{emoji} ⚔️ **Heroes Called:** {event} begins in **{days} days**—spells ready ({time_left})", "{emoji} 🎲 **The Dice Await:** {event} in **{days} days** • Rolling begins ({time_left}) • {date}", "{emoji} 🐉 **Dragon's Countdown:** {event} — beast awakens in **{days} days** ({time_left})", "{emoji} 📜 **Ancient Text:** {event} deciphered in **{days} days**—destiny calls ({time_left}) • {date}"],
            "one_day": ["{emoji} 🎲 **TOMORROW: ADVENTURE AWAITS** {event} rolls ({time_left}) • {date}", "{emoji} 🕯️ **TAVERN OPENS TOMORROW:** {event} — gather courage ({time_left})", "{emoji} 📖 **DM'S TABLE READY:** {event} begins tomorrow—spells locked ({time_left}) • {date}", "{emoji} ⚔️ **ONE REST SHORT:** {event} tomorrow • Long rest tonight ({time_left})", "{emoji} 🔮 **FATE'S DOOR OPENS TOMORROW:** {event} ({time_left}) • {date}", "{emoji} 🗺️ **DAWN BREAKS ON YOUR JOURNEY:** {event} tomorrow—map ready ({time_left})"],
            "zero_day": ["{emoji} 🎲 **ROLL INITIATIVE—{event} IS TODAY** ({time_left}) • {date}", "{emoji} 🕯️ **THE TAVERN OPENS:** {event} **NOW**—adventure time ({time_left})", "{emoji} 📖 **THE CHAPTER BEGINS:** {event} **TODAY**—party assembles ({time_left}) • {date}", "{emoji} 🐉 **THE DRAGON AWAKENS:** {event} **LIVE NOW** 🔥 ({time_left})", "{emoji} ⚔️ **SWORDS DRAWN:** {event} **TODAY**—hero's hour ({time_left})"],
        },
        "repeat_templates": ["{emoji} 🔁 **THE STORY LOOPS:** {event} returns in **{time_left}** 📖 • {date}", "{emoji} 🔁 **NEXT CHAPTER POSTED:** {event} in **{time_left}** • {date}", "{emoji} 🔁 **RECURRING QUEST:** {event} cycles in **{time_left}** • Adventure never ends", "{emoji} 🔁 **THE CYCLE CONTINUES:** {event} in **{time_left}** 🔮 • {date}", "{emoji} 🔁 **SESSION RESCHEDULED:** {event} next run in **{time_left}** • {date}"],
        "remindall_templates": ["{emoji} 📢 **HERALD'S CALL:** {event} in **{time_left}** • {date}", "{emoji} 🎲 **SESSION REMINDER:** {event} in **{time_left}** • Bring your dice 🎲 • {date}", "{emoji} 🔔 **THE BELL RINGS:** {event} in **{time_left}** ({date})", "{emoji} 📖 **DON'T FORGET SPELLBOOK:** {event} in **{time_left}** • {date}", "{emoji} ⚔️ **SHARPEN YOUR SWORDS:** {event} in **{time_left}** • Adventure awaits ({date})"],
        "start_blast_templates": ["🎲 🐉 **ROLL INITIATIVE!** 🐉 **{event} BEGINS NOW—ADVENTURE IS LIVE!**", "📖 **FROM THE TALE KEEPER:** {event} **STARTS NOW** 📜 • The story unfolds", "⚔️ **SWORDS DRAWN!** {event} **BEGINS NOW**—HEROES RISE! 🛡️🔥", "🕯️ **THE TAVERN DOORS OPEN:** {event} **LIVE NOW** • Gather the party 🍺", "🔮 **FATE REVEALS ITSELF:** {event} **NOW LIVE**—Destiny awaits ✨", "🗺️ **EXPEDITION LAUNCHES:** {event} **STARTS NOW** • Onward to glory 🗺️🌟"],
    },
    "romance": {
        "label": "Date Night Countdown",
        "supporter_only": True,
        "color": discord.Color.from_rgb(245, 50, 100),
        "pin_title_pool": ["💕 **HEART SYNC** • When We Meet Again", "🌹 **LOVE'S CALENDAR** • Moments Pending", "💌 **SOULMATE TIMER** • The Wait is Worth It", "✨ **STARLIT COUNTDOWN** • Timeless Together", "💑 **TWO HEARTS** • One Beautiful Moment", "🌸 **BLOOM SEASON** • Love Arrives Soon", "🦋 **BUTTERFLY WINGS** • Magic Incoming"],
        "event_emoji_pool": ["💕", "🌹", "💌", "✨", "💑", "🌸", "💖", "🦋"],
        "milestone_emoji_pool": ["💕", "🌹", "💌", "✨", "🌸"],
        "milestone_templates": {
            "default": ["{emoji} 💌 **Love's Letter:** {event} in **{days} days**—butterflies dancing ({time_left}) • {date}", "{emoji} 🌹 **Rose Counter:** {event} arrives in **{days} days** • Magic pending ({time_left})", "{emoji} ✨ **Starlit Countdown:** {event} — **{days} days** until our moment ({time_left}) • {date}", "{emoji} 💕 **Heartbeat Sync:** {event} in **{days} days** • Love moves differently ({time_left})", "{emoji} 🌸 **Garden of Moments:** {event} blooms in **{days} days** ({time_left}) • {date}", "{emoji} 💑 **Two Hearts:** {event} — **{days} days** and counting 💕 ({time_left})", "{emoji} 🦋 **Butterfly Wings:** {event} flutters in **{days} days** • The wait sweetens it ({time_left}) • {date}", "{emoji} 💖 **Passion Pending:** {event} in **{days} days** • Forever awaits ({time_left})"],
            "one_day": ["{emoji} 💕 **TOMORROW IS OUR DAY:** {event} — one sleep left 🌙 ({time_left}) • {date}", "{emoji} 🌹 **ROSE PETALS READY:** {event} tomorrow—butterflies activated ({time_left})", "{emoji} ✨ **STARLIGHT TOMORROW NIGHT:** {event} — the moment we waited for ({time_left}) • {date}", "{emoji} 💌 **LOVE NOTE PENDING:** {event} tomorrow 💌 ({time_left})", "{emoji} 🌸 **BLOOM TIME TOMORROW:** {event} — beauty incoming ({time_left}) • {date}", "{emoji} 🦋 **WINGS SPREAD:** {event} tomorrow—flight ready ({time_left})"],
            "zero_day": ["{emoji} 💕 **IT'S TODAY—OUR MOMENT IS HERE:** {event} 💕 ({time_left}) • {date}", "{emoji} 🌹 **THE DAY HAS COME:** {event} **RIGHT NOW**—Forever starts ({time_left})", "{emoji} ✨ **STARLIGHT FALLS:** {event} **BEGINS NOW**—Magic everywhere ✨ ({time_left}) • {date}", "{emoji} 💌 **LOVE DELIVERED:** {event} **TODAY**—This is it 💌 ({time_left})", "{emoji} 🌸 **BLOOM BLOOMS:** {event} **NOW**—You made it 🌸 ({time_left})"],
        },
        "repeat_templates": ["{emoji} 🔁 **LOVE CYCLES:** {event} returns in **{time_left}** • Another together 💕 • {date}", "{emoji} 🔁 **HEARTBEAT REPEATS:** {event} in **{time_left}** 💕 • {date}", "{emoji} 🔁 **FOREVER CONTINUES:** {event} again in **{time_left}** • {date}", "{emoji} 🔁 **THE CYCLE OF LOVE:** {event} in **{time_left}** 🌹 ({date})", "{emoji} 🔁 **NEXT CHAPTER:** {event} in **{time_left}** • Love grows ({date})"],
        "remindall_templates": ["{emoji} 💌 **HEART REMINDER:** {event} in **{time_left}** 💕 • {date}", "{emoji} 🌹 **LOVE ALERT:** {event} in **{time_left}** • Moments matter • {date}", "{emoji} ✨ **STARLIGHT PENDING:** {event} in **{time_left}** • Almost there", "{emoji} 🦋 **FLUTTER TIME SOON:** {event} in **{time_left}** 🦋 • {date}", "{emoji} 🌸 **BLOOM ALERT:** {event} in **{time_left}** • Love incoming ({date})"],
        "start_blast_templates": ["💕 💕 **IT'S TIME!** 💕 **{event} IS HERE—YOUR MOMENT IS NOW!**", "🌹 **THE ROSES BLOOM:** {event} **STARTS NOW** 🌹 • Forever awaits", "✨ **STARLIGHT FALLS:** {event} **BEGINS NOW**—Magic in every moment ✨", "💌 **LOVE DELIVERED:** {event} **LIVE NOW** 💌 • This is it", "🌸 **BLOOM TIME:** {event} **NOW**—Beauty never lasted this long 🌸", "💑 **TWO HEARTS BEAT AS ONE:** {event} **STARTS NOW** 💕"],
    },
    "vacation": {
        "label": "Vacation Countdown Board",
        "supporter_only": True,
        "color": discord.Color.from_rgb(20, 210, 200),
        "pin_title_pool": ["🌴 **GETAWAY MODE** • Beach Awaits", "✈️ **DEPARTURE TIMER** • Adventure Countdown", "🏖️ **TROPICAL VIBES** • Sun & Sand Incoming", "🧳 **ESCAPE PLAN** • Journey Loading", "🌊 **WAVES CALLING** • Paradise Pending", "☀️ **VITAMIN SEA** • Freedom Countdown", "🗺️ **TRAVEL BOARD** • Wanderlust Loading"],
        "event_emoji_pool": ["🌴", "✈️", "🏖️", "🧳", "🌊", "☀️", "🌺", "🍹"],
        "milestone_emoji_pool": ["🌴", "✈️", "🏖️", "🌊", "☀️"],
        "milestone_templates": {
            "default": ["{emoji} ✈️ **DEPARTURE IN:** {event} — **{days} days** until takeoff ({time_left}) • {date}", "{emoji} 🏖️ **SAND TIMER:** {event} in **{days} days** • Bags half-packed ({time_left}) • {date}", "{emoji} 🌊 **WAVE COUNTDOWN:** {event} — **{days} days** to paradise ({time_left})", "{emoji} 🧳 **LUGGAGE ALERT:** {event} in **{days} days** • Start gathering ({time_left}) • {date}", "{emoji} 🌴 **PALM TREE TIMER:** {event} arrives in **{days} days** ({time_left})", "{emoji} ☀️ **SUNSCREEN COUNTDOWN:** {event} in **{days} days** • Vitamin sea ({time_left})", "{emoji} 🍹 **TROPICAL TIMER:** {event} — **{days} days** until cocktails ({time_left}) • {date}", "{emoji} 🗺️ **ADVENTURE MAP:** {event} in **{days} days** • Wander time ({time_left})"],
            "one_day": ["{emoji} ✈️ **GATES OPEN TOMORROW:** {event} — packing panic 🧳 ({time_left}) • {date}", "{emoji} 🏖️ **ONE SLEEP UNTIL PARADISE:** {event} tomorrow 🌴 ({time_left})", "{emoji} 🌊 **LAST NIGHT HOME:** {event} departs tomorrow—adventure calls ({time_left}) • {date}", "{emoji} ☀️ **TAN LINES TOMORROW:** {event} — final prep hour ({time_left})", "{emoji} 🧳 **FINAL BOARDING CALL:** {event} tomorrow—you've got this ({time_left}) • {date}", "{emoji} 🌺 **FLOWER CROWN READY:** {event} tomorrow—tropical energy ({time_left})"],
            "zero_day": ["{emoji} ✈️ **WHEELS UP—IT'S HAPPENING:** {event} **TODAY**—Off to adventure! 🌍 ({time_left}) • {date}", "{emoji} 🏖️ **BEACH TIME BEGINS:** {event} **RIGHT NOW** 🌊 • Paradise unlocked ({time_left})", "{emoji} 🌴 **WELCOME TO PARADISE:** {event} **STARTS NOW**—Soak it ☀️ ({time_left}) • {date}", "{emoji} 🧳 **ESCAPE MODE ACTIVATED:** {event} **LIVE NOW**—Freedom tastes like vacation 🍹", "{emoji} 🌊 **WAVES CRASH:** {event} **TODAY**—Vitamin sea delivered 💙 ({time_left})"],
        },
        "repeat_templates": ["{emoji} 🔁 **WANDERLUST CYCLES:** {event} returns in **{time_left}** • Another adventure ({date})", "{emoji} 🔁 **ESCAPE PLAN REPEATS:** {event} in **{time_left}** • Paradise always calls 🏖️ • {date}", "{emoji} 🔁 **GETAWAY TIME AGAIN:** {event} in **{time_left}** 🏖️ • {date}", "{emoji} 🔁 **ADVENTURE CIRCLE:** {event} cycles in **{time_left}** ✈️ • {date}", "{emoji} 🔁 **VACATION RELOAD:** {event} in **{time_left}** • Escape awaits ({date})"],
        "remindall_templates": ["{emoji} ✈️ **VACATION ALERT:** {event} in **{time_left}** • Start packing 🧳 • {date}", "{emoji} 🏖️ **PARADISE PENDING:** {event} in **{time_left}** • Sunscreen ready? 🌊 • {date}", "{emoji} 🌴 **ESCAPE REMINDER:** {event} in **{time_left}** • Freedom incoming • {date}", "{emoji} 🌊 **GETAWAY TIME:** {event} in **{time_left}** ☀️ • {date}", "{emoji} ☀️ **TAN TIME SOON:** {event} in **{time_left}** • Escape awaits ({date})"],
        "start_blast_templates": ["✈️ 🌴 **DEPARTURE GATE OPENS!** 🌴 **{event} STARTS NOW—LET'S GET OUT OF HERE!**", "🏖️ **PARADISE UNLOCKED:** {event} **BEGINS NOW** 🌊 • Welcome to vacation", "☀️ **VITAMIN SEA DELIVERED:** {event} **LIVE NOW** 🍹 • Escape activated", "🧳 **ADVENTURE AWAITS:** {event} **STARTS NOW**—The world is yours ✈️", "🌊 **WAVE TIME:** {event} **TODAY**—Dive in 🏊 • Paradise is calling", "🌺 **ISLAND LIVING:** {event} **NOW**—Freedom time 🏝️ • Enjoy it"],
    },
    "hype": {
        "label": "Hype Mode",
        "supporter_only": True,
        "color": discord.Color.from_rgb(255, 70, 150),
        "pin_title_pool": ["🚀 **HYPE METER** • Countdown Chaos", "🔥 **MAIN CHARACTER ENERGY** • Big Moment", "⚡ **INCOMING** • Excitement Board", "💥 **LET'S GO** • Action Loaded", "🎉 **MOMENTUM BUILDING** • Peak Energy", "🌟 **IT'S HAPPENING** • History Today", "💎 **TREASURE TIME** • Main Event"],
        "event_emoji_pool": ["🚀", "🔥", "⚡", "💥", "🎉", "🌟", "💎", "✨"],
        "milestone_emoji_pool": ["🚀", "🔥", "⚡", "💥", "🎉"],
        "milestone_templates": {
            "default": ["{emoji} 🚀 **HYPE ALERT:** {event} in **{days} days** • Momentum building ({time_left}) • {date}", "{emoji} 🔥 **MAIN CHARACTER MODE:** {event} — **{days} days** until YOU shine ({time_left})", "{emoji} ⚡ **INCOMING:** {event} in **{days} days** • Don't blink ({time_left}) • {date}", "{emoji} 💥 **CHAOS LOADING:** {event} in **{days} days**—energy rising ({time_left})", "{emoji} 🎉 **COUNTDOWN HEAT:** {event} — **{days} days** of anticipation ({time_left}) • {date}", "{emoji} 🌟 **HISTORY INCOMING:** {event} in **{days} days** • Destiny ({time_left})", "{emoji} 💎 **TREASURE TRACKER:** {event} in **{days} days**—main event energy ({time_left}) • {date}", "{emoji} ✨ **PLOT TWIST:** {event} in **{days} days** • Story's building ({time_left})"],
            "one_day": ["{emoji} 🚀 **LAUNCH TOMORROW:** {event} — strap in! 🌙 ({time_left}) • {date}", "{emoji} 🔥 **MAIN CHARACTER TOMORROW:** {event} • It's YOUR moment ({time_left})", "{emoji} ⚡ **24 HOURS TO CHAOS:** {event} tomorrow—energy is PEAK ({time_left}) • {date}", "{emoji} 💥 **EXPLOSION PENDING:** {event} tomorrow—get ready ({time_left})", "{emoji} 🎉 **PARTY MODE TOMORROW:** {event} — the hype is REAL ({time_left}) • {date}", "{emoji} 🌟 **SHINE TIME:** {event} tomorrow—put on your crown ({time_left})"],
            "zero_day": ["{emoji} 🚀 **LIFTOFF NOW:** {event} **LIVE** • Main character energy activated! 💥 ({time_left}) • {date}", "{emoji} 🔥 **YOU'RE UP:** {event} **STARTS NOW**—SHOW THEM WHAT'S UP! ({time_left})", "{emoji} ⚡ **IT'S CHAOS O'CLOCK:** {event} **TODAY**—Peak energy unlocked! ({time_left}) • {date}", "{emoji} 💥 **EXPLOSION:** {event} **NOW**—THE MOMENT IS HERE! ({time_left})", "{emoji} 🎉 **CELEBRATION BEGINS:** {event} **LIVE**—HISTORY IN THE MAKING! ({time_left})"],
        },
        "repeat_templates": ["{emoji} 🔁 **HYPE CYCLES:** {event} returns in **{time_left}** • Let's go again! 🚀 • {date}", "{emoji} 🔁 **CHAOS RELOADS:** {event} in **{time_left}** • Round 2 energy 🔥 • {date}", "{emoji} 🔁 **MOMENTUM RESTARTS:** {event} cycles in **{time_left}** ⚡ • {date}", "{emoji} 🔁 **ENERGY RETURNS:** {event} in **{time_left}** • Still hyped! 💥 • {date}"],
        "remindall_templates": ["{emoji} 📢 **HYPE REMINDER:** {event} in **{time_left}** • Get pumped 🔥 • {date}", "{emoji} 🌟 **DON'T SLEEP:** {event} in **{time_left}** • It's coming! ⚡ • {date}", "{emoji} 💥 **CHAOS COUNTDOWN:** {event} in **{time_left}** • Are you ready? ({date})", "{emoji} 🚀 **LAUNCH ALERT:** {event} in **{time_left}** • Get ready! 🚀 • {date}", "{emoji} 🎉 **PARTY INCOMING:** {event} in **{time_left}** • Main event soon ({date})"],
        "start_blast_templates": ["🚀 🔥 **LIFTOFF!** 🔥 **{event} IS LIVE—MAIN CHARACTER ENERGY ACTIVATED!**", "⚡ **IT'S CHAOS O'CLOCK:** {event} **STARTS NOW** 💥 • Show them!", "💥 **EXPLOSION:** {event} **BEGINS NOW**—THE MOMENT IS HERE! 🌟", "🎉 **CELEBRATION MODE:** {event} **LIVE NOW** • HISTORY! 📸", "🌟 **SHINE TIME:** {event} **RIGHT NOW**—YOU'RE UP! 🚀", "💎 **TREASURE DROP:** {event} **NOW**—MAIN EVENT ENERGY! 🔥"],
    },
    "minimal": {
        "label": "Minimalist",
        "supporter_only": True,
        "color": discord.Color.from_rgb(170, 180, 200),
        "pin_title_pool": ["▫️ **CLEAN** • Simple Countdown", "⏱️ **FOCUSED** • Event Board", "—— **QUIET TIME** • Calendar Sync", "• **CLARITY** • Schedule Locked", "▪️ **MINIMAL** • Timelines Here", "⬜ **NEAT** • Events Listed", "・ **SIMPLE** • Upcoming"],
        "event_emoji_pool": ["▫️", "⏱️", "•", "—", "⬜", "▪️", "・"],
        "milestone_emoji_pool": ["▫️", "⏱️", "•", "⬜", "▪️"],
        "milestone_templates": {
            "default": ["{emoji} {event} — {days} days ({time_left}) • {date}", "{emoji} {event} in {days} days — {time_left} • {date}", "{emoji} {event} scheduled {days} days out — {time_left}", "{emoji} {days} days until {event} — {time_left} • {date}", "{emoji} {event} arrives {time_left} ({days} days) • {date}", "{emoji} {event} — {days}d {time_left} • {date}", "{emoji} upcoming: {event} ({days} days, {time_left}) • {date}", "{emoji} {event} in {days}d — {time_left} • {date}"],
            "one_day": ["{emoji} {event} — tomorrow", "{emoji} {event} is tomorrow — {time_left} • {date}", "{emoji} tomorrow: {event} ({time_left})", "{emoji} one day — {event} ({time_left}) • {date}", "{emoji} {event} — 1 day left ({time_left})"],
            "zero_day": ["{emoji} {event} — today ({time_left}) • {date}", "{emoji} today: {event} — {time_left}", "{emoji} {event} is now — {time_left} • {date}", "{emoji} happening now: {event} ({time_left})", "{emoji} {event} — right now ({time_left}) • {date}"],
        },
        "repeat_templates": ["{emoji} {event} repeats in {time_left} • {date}", "{emoji} next: {event} in {time_left} ({date})", "{emoji} {event} — recurring {time_left}", "{emoji} {event} again in {time_left} • {date}"],
        "remindall_templates": ["{emoji} reminder: {event} in {time_left} • {date}", "{emoji} {event} — {time_left} away ({date})", "{emoji} upcoming: {event} in {time_left}", "{emoji} {event} in {time_left} • {date}"],
        "start_blast_templates": ["{emoji} {event} — now", "{emoji} {event} starting now", "{emoji} now: {event}", "{emoji} {event} live", "{emoji} {event} begins"],
    },
    "school": {
        "label": "School Mode",
        "supporter_only": True,
        "color": discord.Color.from_rgb(50, 120, 255),
        "pin_title_pool": ["📚 **STUDY SCHEDULE** • Deadline Tracker", "📝 **SYLLABUS MODE** • Assignment Due", "✅ **PREP CHECKLIST** • Exam Board", "🧠 **FOCUS TIME** • Tasks Locked", "⏳ **DEADLINE ENERGY** • Calendar Alert", "📋 **TASK LIST** • Due Dates", "🎓 **GRADE TRACKER** • Deadlines Here"],
        "event_emoji_pool": ["📚", "📝", "✅", "🧠", "⏳", "📋", "🎓", "💪"],
        "milestone_emoji_pool": ["📚", "📝", "✅", "🧠", "⏳"],
        "milestone_templates": {
            "default": ["{emoji} 📚 **DEADLINE ALERT:** {event} due in **{days} days** • Start prep ({time_left}) • {date}", "{emoji} 📝 **SYLLABUS CHECK:** {event} — **{days} days** until due ({time_left})", "{emoji} ✅ **TASK BOARD:** {event} in **{days} days** • Small steps ({time_left}) • {date}", "{emoji} 🧠 **FOCUS TIME:** {event} due **{days} days** out • Break it down ({time_left})", "{emoji} 💪 **YOU GOT THIS:** {event} in **{days} days** — study strong ({time_left}) • {date}", "{emoji} ⏳ **CRUNCH WARNING:** {event} due **{days} days** • Plan early ({time_left})", "{emoji} 🎓 **GRADE PENDING:** {event} in **{days} days** • Quality work ({time_left}) • {date}", "{emoji} 📋 **DUE SOON:** {event} — **{days} days** left • Get ahead ({time_left})"],
            "one_day": ["{emoji} 📚 **TOMORROW'S DUE:** {event} — crunch time! 🌙 ({time_left}) • {date}", "{emoji} 📝 **LAST MINUTE:** {event} due tomorrow—final push ({time_left})", "{emoji} ✅ **FINAL CHECK:** {event} tomorrow—lock it in ({time_left}) • {date}", "{emoji} 🧠 **BRAIN TIME:** {event} due tomorrow—you can do it ({time_left})", "{emoji} 💪 **FINISH STRONG:** {event} tomorrow • One more sprint ({time_left}) • {date}"],
            "zero_day": ["{emoji} 📚 **IT'S DUE TODAY:** {event} **DUE NOW**—Submit it! ({time_left}) • {date}", "{emoji} 📝 **DEADLINE IS NOW:** {event} **TODAY**—Final submission ({time_left})", "{emoji} ✅ **COMPLETE IT:** {event} **DUE NOW**—You finished! ({time_left}) • {date}", "{emoji} 🎓 **GRADE TIME:** {event} **TODAY**—Work delivered! ({time_left})", "{emoji} 💪 **YOU DID IT:** {event} **NOW**—Deadline conquered! ({time_left})"],
        },
        "repeat_templates": ["{emoji} 🔁 **CYCLE REPEATS:** {event} again in **{time_left}** 📚 • {date}", "{emoji} 🔁 **NEXT ASSIGNMENT:** {event} in **{time_left}** • Keep studying • {date}", "{emoji} 🔁 **HOMEWORK LOOP:** {event} in **{time_left}** • Stay focused ({date})", "{emoji} 🔁 **NEXT DEADLINE:** {event} in **{time_left}** 📝 • {date}"],
        "remindall_templates": ["{emoji} 📢 **DEADLINE REMINDER:** {event} in **{time_left}** • Start now 📚 • {date}", "{emoji} 🧠 **STUDY ALERT:** {event} in **{time_left}** • Plan ahead ({date})", "{emoji} ✅ **TASK REMINDER:** {event} in **{time_left}** • Break it down ({date})", "{emoji} 💪 **YOU CAN DO IT:** {event} in **{time_left}** • Start early ({date})", "{emoji} ⏳ **CRUNCH WARNING:** {event} in **{time_left}** • Deadline approaching ({date})"],
        "start_blast_templates": ["📚 ✅ **DEADLINE IS NOW!** ✅ **{event} DUE—SUBMIT IT!**", "📝 **FINAL CHECK:** {event} **DUE NOW** 🎓 • Submit your work", "🧠 **BRAIN TIME:** {event} **STARTS NOW**—Let's finish! 💪", "⏳ **TIME'S UP:** {event} **DUE NOW**—You got this! 📚", "💪 **FINISH STRONG:** {event} **NOW**—One last push! ✅", "🎓 **GRADE TIME:** {event} **STARTS NOW** • Work delivered! 📝"],
    },
    "spooky": {
        "label": "Spooky Season",
        "supporter_only": True,
        "season_months": [9, 10],  # only applyable in Sep/Oct (previewable year-round)
        "color": discord.Color.from_rgb(255, 135, 25),
        "pin_title_pool": ["🎃 **SPOOKY COUNTDOWN** • The Clock Creaks", "🕯️ **WITCHING HOUR** • Spirits Rising", "🕸️ **COBWEB CALENDAR** • Something Awaits", "👻 **HAUNTED SCHEDULE** • Time Watches", "🦇 **MIDNIGHT MODE** • Darkness Incoming", "🔮 **CURSED TIMER** • Fate Unknown", "💀 **SKELETON KEY** • Secrets Unlocking"],
        "event_emoji_pool": ["🎃", "👻", "🕷️", "🦇", "💀", "🔮", "🕯️", "🕸️"],
        "milestone_emoji_pool": ["🎃", "👻", "🕷️", "🦇", "🔮"],
        "milestone_templates": {
            "default": ["{emoji} 🎃 **THE CLOCK CREAKS:** {event} in **{days} days** • Something stirs ({time_left}) • {date}", "{emoji} 👻 **SPIRITS RISE:** {event} — **{days} days** until the veil thins ({time_left})", "{emoji} 🕯️ **CANDLELIGHT FLICKERING:** {event} in **{days} days** • Shadows dance ({time_left}) • {date}", "{emoji} 🦇 **WINGS BEATING:** {event} due **{days} days** out • Darkness gathers ({time_left})", "{emoji} 🔮 **CRYSTAL GLOWS:** {event} in **{days} days** — destiny approaches ({time_left}) • {date}", "{emoji} 🕸️ **WEBS FORMING:** {event} — **{days} days** until they catch you ({time_left})", "{emoji} 💀 **BONES RATTLE:** {event} in **{days} days** • The dead whisper ({time_left}) • {date}", "{emoji} 🎃 **JACK-O'-LANTERN GLOWS:** {event} in **{days} days** • Mark your calendar ({time_left})"],
            "one_day": ["{emoji} 🎃 **THE VEIL OPENS TOMORROW:** {event} — one night left 🌙 ({time_left}) • {date}", "{emoji} 👻 **MIDNIGHT APPROACHES:** {event} tomorrow—spirits waken ({time_left})", "{emoji} 🕯️ **FINAL FLICKER:** {event} tomorrow—the countdown ends ({time_left}) • {date}", "{emoji} 🦇 **WINGS BEAT LOUDER:** {event} tomorrow—destiny nears ({time_left})", "{emoji} 🔮 **CRYSTAL BLAZES:** {event} tomorrow—secrets unlock ({time_left}) • {date}"],
            "zero_day": ["{emoji} 🎃 **THE MOMENT ARRIVES:** {event} **RIGHT NOW**—The spell is cast! 🕯️ ({time_left}) • {date}", "{emoji} 👻 **THE VEIL TEARS:** {event} **NOW**—Spirits walk ({time_left})", "{emoji} 🦇 **WINGS SPREAD WIDE:** {event} **TODAY**—Darkness falls! ({time_left}) • {date}", "{emoji} 💀 **THE DEAD RISE:** {event} **NOW**—The curse awakens! ({time_left})", "{emoji} 🔮 **MAGIC UNLEASHED:** {event} **LIVE**—Ancient power stirs ({time_left})"],
        },
        "repeat_templates": ["{emoji} 🔁 **THE CYCLE TURNS:** {event} returns in **{time_left}** 🎃 • {date}", "{emoji} 🔁 **SPIRITS RETURN:** {event} again in **{time_left}** • The haunting continues ({date})", "{emoji} 🔁 **SPOOKY SEASON REPEATS:** {event} in **{time_left}** 👻 • {date}", "{emoji} 🔁 **DARKNESS CYCLES:** {event} in **{time_left}** 🦇 • ({date})"],
        "remindall_templates": ["{emoji} 👻 **SPIRITS STIR:** {event} in **{time_left}** • The haunting begins ({date})", "{emoji} 🎃 **PUMPKIN PATCH ALERT:** {event} in **{time_left}** • Get ready ({date})", "{emoji} 🕯️ **CANDLES LIGHT:** {event} in **{time_left}** • Darkness comes ({date})", "{emoji} 🦇 **WINGS BEAT:** {event} in **{time_left}** • Something stirs ({date})", "{emoji} 💀 **BONES RATTLE:** {event} in **{time_left}** • Beware ({date})"],
        "start_blast_templates": ["🎃 👻 **THE VEIL TEARS!** 👻 **{event} IS HERE—THE HAUNTING BEGINS!**", "👻 **SPIRITS WALK:** {event} **STARTS NOW** 🕯️ • The curse awakens", "🦇 **WINGS SPREAD:** {event} **BEGINS NOW**—Darkness falls! 🌑", "💀 **THE DEAD RISE:** {event} **LIVE NOW** • Beware! ☠️", "🔮 **MAGIC UNLEASHED:** {event} **NOW**—Ancient power stirs 🕯️", "🎃 **JACK-O'-LANTERN GLOWS:** {event} **STARTS NOW** • Happy Spooky Season! 👻"],
    },
    "girly": {
        "label": "Cute Aesthetic",
        "supporter_only": True,
        "color": discord.Color.from_rgb(255, 110, 180),
        "pin_title_pool": ["🎀 **PRETTY PLANS** • Adorable Countdown", "💖 **SPARKLE MODE** • Cute Vibes", "✨ **MAGICAL MOMENT** • Dreams Loading", "🌸 **BLOOM SEASON** • Soft Energy", "🫧 **BUBBLE TEA TIMER** • Cozy Countdown", "🦋 **BUTTERFLY WINGS** • Beauty Pending", "👑 **CROWN MOMENT** • You're That Girl"],
        "event_emoji_pool": ["🎀", "💖", "✨", "🌸", "🫧", "🦋", "👑", "🌺"],
        "milestone_emoji_pool": ["🎀", "💖", "✨", "🌸", "🦋"],
        "milestone_templates": {
            "default": ["{emoji} 🎀 **PRETTY COUNTDOWN:** {event} in **{days} days** • Sparkle mode ({time_left}) • {date}", "{emoji} 💖 **SOFT VIBES:** {event} — **{days} days** of cuteness ({time_left})", "{emoji} ✨ **MAGICAL TIMER:** {event} in **{days} days** • Dreams manifesting ({time_left}) • {date}", "{emoji} 🌸 **BLOOM COUNTDOWN:** {event} due **{days} days** out • Beauty pending ({time_left})", "{emoji} 🫧 **BUBBLE TEA BREAK:** {event} in **{days} days** — self-care incoming ({time_left}) • {date}", "{emoji} 🦋 **WING FLUTTER:** {event} — **{days} days** of transformation ({time_left})", "{emoji} 👑 **THAT GIRL ENERGY:** {event} in **{days} days** • Crown ready ({time_left}) • {date}", "{emoji} 🌺 **TROPICAL CUTE:** {event} in **{days} days** • Paradise vibes ({time_left})"],
            "one_day": ["{emoji} 🎀 **PRETTY DAY TOMORROW:** {event} — one sleep left 🌙 ({time_left}) • {date}", "{emoji} 💖 **SOFT TOMORROW:** {event} • Sparkles ready ({time_left})", "{emoji} ✨ **MAGIC TOMORROW:** {event} — the moment we dreamed ({time_left}) • {date}", "{emoji} 🌸 **BLOOM TIME:** {event} tomorrow—beauty unlocked ({time_left})", "{emoji} 👑 **CROWN MOMENT TOMORROW:** {event} • That girl energy ({time_left}) • {date}"],
            "zero_day": ["{emoji} 🎀 **PRETTY DAY IS HERE:** {event} **RIGHT NOW**—Sparkle time! 💖 ({time_left}) • {date}", "{emoji} 💖 **SOFT MOMENT:** {event} **NOW**—You're that girl! ({time_left})", "{emoji} ✨ **MAGICAL NOW:** {event} **STARTS NOW**—Dreams come true! ({time_left}) • {date}", "{emoji} 🌸 **BLOOM BLOOMS:** {event} **TODAY**—Beauty unleashed! ({time_left})", "{emoji} 👑 **CROWN MOMENT:** {event} **NOW**—That girl energy activated! ({time_left})"],
        },
        "repeat_templates": ["{emoji} 🔁 **CUTENESS CYCLES:** {event} returns in **{time_left}** 🎀 • {date}", "{emoji} 🔁 **SPARKLE REPEATS:** {event} again in **{time_left}** • {date}", "{emoji} 🔁 **PRETTY LOOP:** {event} in **{time_left}** 💖 • {date}", "{emoji} 🔁 **BEAUTY CYCLE:** {event} cycles in **{time_left}** ✨ • ({date})"],
        "remindall_templates": ["{emoji} 🎀 **CUTE REMINDER:** {event} in **{time_left}** • Sparkle on! 💖 • {date}", "{emoji} ✨ **MAGIC ALERT:** {event} in **{time_left}** • Dreams pending ({date})", "{emoji} 👑 **THAT GIRL CHECK:** {event} in **{time_left}** • You've got this ({date})", "{emoji} 🌸 **BLOOM ALERT:** {event} in **{time_left}** • Beauty incoming ({date})", "{emoji} 🦋 **BUTTERFLY WINGS:** {event} in **{time_left}** • Transformation pending ({date})"],
        "start_blast_templates": ["🎀 💖 **PRETTY MOMENT IS HERE!** 💖 **{event} IS LIVE—SPARKLE TIME!**", "✨ **MAGICAL NOW:** {event} **STARTS NOW** 🌟 • Dreams come true", "👑 **THAT GIRL ENERGY:** {event} **BEGINS NOW**—Crown activated! 💎", "🌸 **BLOOM TIME:** {event} **RIGHT NOW** • Beauty unleashed 🌺", "💖 **SOFT MOMENT:** {event} **LIVE NOW** • You're that girl 🎀", "🦋 **TRANSFORMATION:** {event} **NOW**—Glow up activated! ✨"],
    },
    "workplace": {
        "label": "Workplace Ops",
        "supporter_only": True,
        "color": discord.Color.from_rgb(90, 100, 120),
        "pin_title_pool": ["📌 **OPERATIONS BOARD** • Deliverable Tracker", "📋 **ACTION ITEMS** • Project Timeline", "✅ **TASK MANAGEMENT** • Deadline Alert", "🗓️ **CALENDAR LOCK** • Schedule Active", "📊 **METRICS INCOMING** • Stakeholder Update", "💼 **EXECUTIVE MODE** • Key Dates", "⏱️ **ON SCHEDULE** • Meetings Locked"],
        "event_emoji_pool": ["📌", "📋", "✅", "🗓️", "📊", "💼", "⏱️", "📈"],
        "milestone_emoji_pool": ["📌", "📋", "✅", "🗓️", "📊"],
        "milestone_templates": {
            "default": ["{emoji} 📌 **DELIVERABLE ALERT:** {event} in **{days} days** • Lockdown mode ({time_left}) • {date}", "{emoji} 📋 **ACTION ITEM:** {event} — **{days} days** on the critical path ({time_left})", "{emoji} ✅ **TASK BOARD:** {event} in **{days} days** • Execution phase ({time_left}) • {date}", "{emoji} 🗓️ **CALENDAR SYNC:** {event} due **{days} days** out • Team alignment ({time_left})", "{emoji} 📊 **METRICS UPDATE:** {event} in **{days} days** — stakeholder value ({time_left}) • {date}", "{emoji} 💼 **EXECUTIVE NOTE:** {event} — **{days} days** until key milestone ({time_left})", "{emoji} ⏱️ **ON THE CLOCK:** {event} in **{days} days** • Budget time wisely ({time_left}) • {date}", "{emoji} 📈 **GROWTH TRACKER:** {event} in **{days} days** — momentum building ({time_left})"],
            "one_day": ["{emoji} 📌 **DELIVERY TOMORROW:** {event} — final sprint 🌙 ({time_left}) • {date}", "{emoji} 📋 **ACTION ITEMS FINAL:** {event} tomorrow—lockdown phase ({time_left})", "{emoji} ✅ **EXECUTION DAY:** {event} tomorrow—execution ready ({time_left}) • {date}", "{emoji} 🗓️ **CALENDAR LOCKED:** {event} tomorrow—all hands ({time_left})", "{emoji} 📊 **STAKEHOLDER DAY:** {event} tomorrow—update incoming ({time_left}) • {date}"],
            "zero_day": ["{emoji} 📌 **DELIVERY DAY:** {event} **RIGHT NOW**—Execution time! ({time_left}) • {date}", "{emoji} 📋 **ACTION COMPLETE:** {event} **NOW**—Deliverable locked! ({time_left})", "{emoji} ✅ **EXECUTION LIVE:** {event} **STARTS NOW**—Team mobilized! ({time_left}) • {date}", "{emoji} 🗓️ **CALENDAR ACTIVE:** {event} **TODAY**—Stakeholder time! ({time_left})", "{emoji} 📊 **GO-LIVE:** {event} **NOW**—Results delivered! ({time_left})"],
        },
        "repeat_templates": ["{emoji} 🔁 **CYCLE COMPLETE:** {event} next in **{time_left}** 📌 • {date}", "{emoji} 🔁 **RECURRING TASK:** {event} in **{time_left}** • Team alignment • {date}", "{emoji} 🔁 **NEXT SPRINT:** {event} cycles in **{time_left}** 📊 • {date}", "{emoji} 🔁 **OPERATIONAL LOOP:** {event} in **{time_left}** • Efficiency locked ({date})"],
        "remindall_templates": ["{emoji} 📢 **TASK REMINDER:** {event} in **{time_left}** • Deliverable pending ({date})", "{emoji} 📋 **ACTION ALERT:** {event} in **{time_left}** • Stakeholder update ({date})", "{emoji} ✅ **EXECUTION CHECK:** {event} in **{time_left}** • Team ready ({date})", "{emoji} 🗓️ **CALENDAR REMINDER:** {event} in **{time_left}** • Meetings lock ({date})", "{emoji} 💼 **EXECUTIVE ALERT:** {event} in **{time_left}** • Key date ({date})"],
        "start_blast_templates": ["📌 ✅ **DELIVERY TIME!** ✅ **{event} IS LIVE—EXECUTION MODE!**", "📋 **ACTION ACTIVATED:** {event} **STARTS NOW** 🚀 • Team mobilized", "🗓️ **CALENDAR LIVE:** {event} **BEGINS NOW**—Stakeholder time! 📊", "💼 **EXECUTIVE GO:** {event} **RIGHT NOW** • Deliverable locked 📌", "⏱️ **CLOCK RUNNING:** {event} **NOW**—Execution engaged! ✅", "📈 **GO-LIVE ACTIVATED:** {event} **STARTS NOW** • Results incoming! 🚀"],
    },
    "celebration": {
        "label": "Celebration Mode",
        "supporter_only": True,
        "color": discord.Color.from_rgb(255, 220, 80),
        "pin_title_pool": ["🎉 **CELEBRATION BOARD** • Big Moment", "🎊 **PARTY INCOMING** • Confetti Loaded", "🥳 **GOOD TIMES** • Milestone Tracker", "🍾 **TOAST TIME** • Special Moment", "✨ **SPARKLE ENERGY** • Main Event", "🎈 **BALLOON DROP** • Excitement Rising", "🏆 **VICTORY COUNTDOWN** • Triumph Pending"],
        "event_emoji_pool": ["🎉", "🎊", "🥳", "🍾", "✨", "🎈", "🏆", "🌟"],
        "milestone_emoji_pool": ["🎉", "🎊", "🥳", "✨", "🎈"],
        "milestone_templates": {
            "default": ["{emoji} 🎉 **PARTY TIME:** {event} in **{days} days** • Hype building ({time_left}) • {date}", "{emoji} 🎊 **CELEBRATION ALERT:** {event} — **{days} days** until we celebrate ({time_left})", "{emoji} 🥳 **GOOD VIBES:** {event} in **{days} days** • Confetti loading ({time_left}) • {date}", "{emoji} 🍾 **TOAST PENDING:** {event} due **{days} days** out • Special moment ({time_left})", "{emoji} 🏆 **VICTORY INCOMING:** {event} in **{days} days** — triumph awaits ({time_left}) • {date}", "{emoji} 🎈 **BALLOON COUNTDOWN:** {event} — **{days} days** until the drop ({time_left})", "{emoji} ✨ **SPARKLE TIME:** {event} in **{days} days** • Main event energy ({time_left}) • {date}", "{emoji} 🌟 **MAIN CHARACTER MOMENT:** {event} in **{days} days** • Your time to shine ({time_left})"],
            "one_day": ["{emoji} 🎉 **PARTY TOMORROW:** {event} — get ready! 🌙 ({time_left}) • {date}", "{emoji} 🎊 **CELEBRATION TOMORROW:** {event} • Confetti ready ({time_left})", "{emoji} 🥳 **GOOD TIMES COMING:** {event} tomorrow—hype is PEAK ({time_left}) • {date}", "{emoji} 🍾 **TOAST TIME:** {event} tomorrow—special moment ({time_left})", "{emoji} 🏆 **VICTORY DAY:** {event} tomorrow—triumph time ({time_left}) • {date}"],
            "zero_day": ["{emoji} 🎉 **IT'S PARTY TIME:** {event} **RIGHT NOW**—Celebration! 🎊 ({time_left}) • {date}", "{emoji} 🎊 **CONFETTI DROP:** {event} **NOW**—We're celebrating! ({time_left})", "{emoji} 🥳 **GOOD TIMES:** {event} **STARTS NOW**—Let's gooo! ({time_left}) • {date}", "{emoji} 🍾 **TOAST RAISED:** {event} **TODAY**—Special moment unlocked! ({time_left})", "{emoji} 🏆 **VICTORY ACHIEVED:** {event} **NOW**—You made it! ({time_left})"],
        },
        "repeat_templates": ["{emoji} 🔁 **CELEBRATION CYCLES:** {event} returns in **{time_left}** 🎉 • {date}", "{emoji} 🔁 **PARTY REPEATS:** {event} again in **{time_left}** • {date}", "{emoji} 🔁 **MILESTONE LOOP:** {event} in **{time_left}** 🏆 • {date}", "{emoji} 🔁 **GOOD TIMES CIRCLE:** {event} cycles in **{time_left}** 🎊 • ({date})"],
        "remindall_templates": ["{emoji} 🎉 **PARTY REMINDER:** {event} in **{time_left}** • Get ready! ({date})", "{emoji} 🏆 **VICTORY ALERT:** {event} in **{time_left}** • Main moment ({date})", "{emoji} ✨ **SPARKLE CHECK:** {event} in **{time_left}** • Shine bright ({date})", "{emoji} 🎊 **CONFETTI ALERT:** {event} in **{time_left}** • Celebration incoming ({date})", "{emoji} 🥳 **GOOD TIMES:** {event} in **{time_left}** • Get hyped ({date})"],
        "start_blast_templates": ["🎉 🎊 **PARTY TIME!** 🎊 **{event} IS HERE—LET'S CELEBRATE!**", "🥳 **CONFETTI DROP:** {event} **STARTS NOW** 🎉 • Good times", "🍾 **TOAST RAISED:** {event} **BEGINS NOW**—Cheers! 🌟", "🏆 **VICTORY:** {event} **RIGHT NOW** • You made it! 🎊", "✨ **SPARKLE MOMENT:** {event} **LIVE NOW** • Main event! 🎈", "🎈 **BALLOON DROP:** {event} **NOW**—Let's celebrate! 🎉"],
    },
    "birthday": {
        "label": "Birthday & Baby",
        "supporter_only": True,
        "color": discord.Color.from_rgb(255, 105, 180),
        "pin_title_pool": ["🎂 **BIRTHDAY COUNTDOWN** • Cake Incoming", "🎈 **PARTY PREP** • Big Day Tracker", "🥳 **ANOTHER TRIP AROUND THE SUN** • Loading", "🍰 **CAKE O'CLOCK** • Celebration Board", "🎁 **GIFT QUEUE** • Special Day Ahead", "👶 **NEW ARRIVAL** • Baby Watch Active", "✨ **MAKE A WISH** • Countdown Central"],
        "event_emoji_pool": ["🎂", "🎈", "🥳", "🍰", "🎁", "👶", "✨", "🎉"],
        "milestone_emoji_pool": ["🎂", "🎈", "🥳", "🍰", "🎁", "✨"],
        "milestone_templates": {
            "default": ["{emoji} 🎂 **CAKE COUNTDOWN:** {event} in **{days} days** ({time_left}) • {date}", "{emoji} 🎈 **PARTY PREP:** {event} — **{days} days** to the big day ({time_left})", "{emoji} 🎁 **GIFT QUEUE:** {event} in **{days} days** • Start shopping ({time_left}) • {date}", "{emoji} 🥳 **HYPE RISING:** {event} — **{days} days** until we celebrate ({time_left})", "{emoji} 🍰 **SWEET TIMING:** {event} in **{days} days** • Cake on standby ({time_left}) • {date}", "{emoji} 👶 **BABY WATCH:** {event} — **{days} days** until the arrival ({time_left})", "{emoji} ✨ **MAKE A WISH:** {event} in **{days} days** • Candles loading ({time_left}) • {date}", "{emoji} 🎉 **SPECIAL DAY:** {event} — **{days} days** and counting ({time_left})"],
            "one_day": ["{emoji} 🎂 **BIRTHDAY TOMORROW:** {event} — one more sleep 🌙 ({time_left}) • {date}", "{emoji} 🎈 **PARTY TOMORROW:** {event} • Blow up the balloons ({time_left})", "{emoji} 🎁 **GIFTS READY:** {event} tomorrow — wrap it up 🎀 ({time_left}) • {date}", "{emoji} 🍰 **CAKE INCOMING:** {event} tomorrow • Chill the bubbly ({time_left})", "{emoji} 👶 **ALMOST HERE:** {event} tomorrow — bags packed? ({time_left}) • {date}", "{emoji} ✨ **WISH LOADING:** {event} tomorrow • Get ready to celebrate ({time_left})"],
            "zero_day": ["{emoji} 🎂 **HAPPY DAY:** {event} **IS TODAY** 🎉 ({time_left}) • {date}", "{emoji} 🎈 **PARTY TIME:** {event} **STARTS NOW** — confetti! ({time_left})", "{emoji} 🥳 **MAKE A WISH:** {event} **TODAY** — candles lit! ({time_left}) • {date}", "{emoji} 🍰 **CAKE CUT:** {event} **RIGHT NOW** — let them eat cake 🎂 ({time_left})", "{emoji} 👶 **THE DAY IS HERE:** {event} **NOW** — welcome! ({time_left})"],
        },
        "repeat_templates": ["{emoji} 🔁 **ANOTHER YEAR:** {event} comes around in **{time_left}** 🎂 • {date}", "{emoji} 🔁 **CELEBRATION LOOP:** {event} again in **{time_left}** • {date}", "{emoji} 🔁 **CAKE CYCLE:** {event} in **{time_left}** 🍰 • {date}", "{emoji} 🔁 **PARTY REPEATS:** {event} in **{time_left}** 🎈 ({date})", "{emoji} 🔁 **NEXT BIG DAY:** {event} in **{time_left}** • {date}"],
        "remindall_templates": ["{emoji} 🎂 **BIRTHDAY REMINDER:** {event} in **{time_left}** • {date}", "{emoji} 🎁 **GIFT ALERT:** {event} in **{time_left}** — got something? ({date})", "{emoji} 🎈 **PARTY CHECK:** {event} in **{time_left}** 🥳 • {date}", "{emoji} 🍰 **CAKE WATCH:** {event} in **{time_left}** • {date}", "{emoji} ✨ **WISH SOON:** {event} in **{time_left}** ({date})"],
        "start_blast_templates": ["🎂 🎉 **HAPPY DAY!** 🎉 **{event} IS HERE—LET'S CELEBRATE!**", "🎈 **PARTY TIME:** {event} **STARTS NOW** 🥳 • Balloons up!", "🍰 **CAKE O'CLOCK:** {event} **RIGHT NOW** — make a wish ✨", "🎁 **GIFTS UNLOCKED:** {event} **TODAY** — the big day! 🎂", "👶 **WELCOME:** {event} **NOW** — the wait is over! 💕", "✨ **MAKE A WISH:** {event} **LIVE** — candles lit! 🎂"],
    },
    "wedding": {
        "label": "Wedding",
        "supporter_only": True,
        "color": discord.Color.from_rgb(214, 174, 148),
        "pin_title_pool": ["💍 **THE BIG DAY** • Countdown to I Do", "💐 **WEDDING WATCH** • Save the Date", "🤍 **HAPPILY EVER AFTER** • Loading", "🥂 **TOAST PENDING** • Celebration Board", "💒 **AISLE TIME** • Ceremony Tracker", "💖 **TWO BECOME ONE** • Countdown Central", "🔔 **WEDDING BELLS** • Big Day Ahead"],
        "event_emoji_pool": ["💍", "💐", "🤍", "🥂", "💒", "💖", "🔔", "✨"],
        "milestone_emoji_pool": ["💍", "💐", "🤍", "🥂", "💒", "✨"],
        "milestone_templates": {
            "default": ["{emoji} 💍 **COUNTDOWN TO I DO:** {event} in **{days} days** ({time_left}) • {date}", "{emoji} 💐 **SAVE THE DATE:** {event} — **{days} days** to go ({time_left})", "{emoji} 💒 **AISLE PREP:** {event} in **{days} days** • RSVPs rolling in ({time_left}) • {date}", "{emoji} 🥂 **TOAST PENDING:** {event} — **{days} days** until cheers ({time_left})", "{emoji} 🤍 **HAPPILY EVER AFTER:** {event} in **{days} days** ({time_left}) • {date}", "{emoji} 💖 **LOVE IS IN THE AIR:** {event} — **{days} days** away ({time_left})", "{emoji} 🔔 **WEDDING BELLS:** {event} in **{days} days** • Almost there ({time_left}) • {date}", "{emoji} ✨ **THE BIG DAY:** {event} — **{days} days** and counting ({time_left})"],
            "one_day": ["{emoji} 💍 **TOMORROW: I DO:** {event} — final touches 🌙 ({time_left}) • {date}", "{emoji} 💐 **BIG DAY TOMORROW:** {event} • Press the suit, steam the dress ({time_left})", "{emoji} 💒 **AISLE READY:** {event} tomorrow — rehearsal done ({time_left}) • {date}", "{emoji} 🥂 **TOAST TOMORROW:** {event} • Chill the champagne ({time_left})", "{emoji} 🤍 **ONE MORE SLEEP:** {event} tomorrow — happily ever after loading ({time_left}) • {date}", "{emoji} 🔔 **BELLS TOMORROW:** {event} • Get ready to celebrate ({time_left})"],
            "zero_day": ["{emoji} 💍 **TODAY YOU SAY I DO:** {event} **IS HERE** ({time_left}) • {date}", "{emoji} 💒 **WALK THE AISLE:** {event} **STARTS NOW** 🤍 ({time_left})", "{emoji} 🥂 **RAISE A GLASS:** {event} **TODAY** — cheers to forever! ({time_left}) • {date}", "{emoji} 💐 **HAPPILY EVER AFTER:** {event} **RIGHT NOW** 💖 ({time_left})", "{emoji} 🔔 **WEDDING BELLS RING:** {event} **NOW** — congrats! ({time_left})"],
        },
        "repeat_templates": ["{emoji} 🔁 **ANNIVERSARY LOOP:** {event} returns in **{time_left}** 💍 • {date}", "{emoji} 🔁 **LOVE ON REPEAT:** {event} again in **{time_left}** • {date}", "{emoji} 🔁 **FOREVER CYCLE:** {event} in **{time_left}** 🤍 • {date}", "{emoji} 🔁 **TOAST AGAIN:** {event} in **{time_left}** 🥂 ({date})", "{emoji} 🔁 **NEXT CELEBRATION:** {event} in **{time_left}** • {date}"],
        "remindall_templates": ["{emoji} 💍 **WEDDING REMINDER:** {event} in **{time_left}** • {date}", "{emoji} 💐 **SAVE THE DATE:** {event} in **{time_left}** ({date})", "{emoji} 🥂 **TOAST SOON:** {event} in **{time_left}** 🤍 • {date}", "{emoji} 💒 **CEREMONY ALERT:** {event} in **{time_left}** • {date}", "{emoji} 🔔 **BELLS SOON:** {event} in **{time_left}** ({date})"],
        "start_blast_templates": ["💍 🤍 **IT'S TIME!** 🤍 **{event} IS HERE—HAPPILY EVER AFTER!**", "💒 **WALK THE AISLE:** {event} **STARTS NOW** 💐 • Here comes the moment", "🥂 **RAISE A GLASS:** {event} **RIGHT NOW** — to forever! 🤍", "🔔 **WEDDING BELLS:** {event} **TODAY** — congratulations! 💖", "💐 **LOVE WINS:** {event} **LIVE NOW** — the big day! 💍", "✨ **FOREVER STARTS:** {event} **NOW** — cheers! 🥂"],
    },
    "gamelaunch": {
        "label": "Game Launch",
        "supporter_only": True,
        "pro_only": True,  # Pro-exclusive premium theme (a vote won't unlock it)
        "color": discord.Color.from_rgb(0, 209, 178),
        "pin_title_pool": ["🚀 **LAUNCH SEQUENCE** • Release Timer Active", "🎮 **DROP INCOMING** • Day-One Countdown", "🕹️ **PRESS START SOON** • Release Board", "💾 **PRELOAD READY** • Servers Spinning Up", "🛰️ **GO FOR LAUNCH** • Countdown Central", "🔓 **UNLOCK TIMER** • Early Access Ahead", "🎯 **DAY ONE** • Release Tracker"],
        "event_emoji_pool": ["🚀", "🎮", "🕹️", "💾", "🛰️", "🔓", "🎯", "⚡"],
        "milestone_emoji_pool": ["🚀", "🎮", "🕹️", "💾", "⚡", "🔓"],
        "milestone_templates": {
            "default": ["{emoji} 🚀 **LAUNCH WINDOW:** {event} drops in **{days} days** ({time_left}) • {date}", "{emoji} 🎮 **DAY-ONE COUNTDOWN:** {event} — **{days} days** to release ({time_left})", "{emoji} 💾 **PRELOAD ALERT:** {event} in **{days} days** • Clear some storage ({time_left}) • {date}", "{emoji} 🔓 **UNLOCK TIMER:** {event} — **{days} days** until go-live ({time_left})", "{emoji} 🛰️ **GO FOR LAUNCH:** {event} in **{days} days** ({time_left}) • {date}", "{emoji} ⚡ **HYPE METER:** {event} — **{days} days** away, charging up ({time_left})", "{emoji} 🕹️ **PRESS START SOON:** {event} in **{days} days** • Controllers ready ({time_left}) • {date}", "{emoji} 🎯 **RELEASE INCOMING:** {event} — **{days} days** and counting ({time_left})"],
            "one_day": ["{emoji} 🚀 **T-MINUS 1 DAY:** {event} launches tomorrow 🌙 ({time_left}) • {date}", "{emoji} 💾 **PRELOAD NOW:** {event} drops tomorrow • Download tonight ({time_left})", "{emoji} 🎮 **DAY-ONE TOMORROW:** {event} • Clear your schedule ({time_left}) • {date}", "{emoji} 🔓 **UNLOCK TOMORROW:** {event} — servers warming up ({time_left})", "{emoji} ⚡ **FINAL CHARGE:** {event} tomorrow • Hype at max ({time_left}) • {date}", "{emoji} 🕹️ **ONE MORE SLEEP:** {event} tomorrow — press start soon ({time_left})"],
            "zero_day": ["{emoji} 🚀 **LIFTOFF:** {event} **IS LIVE NOW** ({time_left}) • {date}", "{emoji} 🎮 **PRESS START:** {event} **DROPPED** — go go go! ({time_left})", "{emoji} 🔓 **SERVERS UNLOCKED:** {event} **LIVE TODAY** ⚡ ({time_left}) • {date}", "{emoji} 💾 **IT'S OUT:** {event} **RIGHT NOW** — download + play! ({time_left})", "{emoji} 🛰️ **WE HAVE LAUNCH:** {event} **NOW** — see you online! ({time_left})"],
        },
        "repeat_templates": ["{emoji} 🔁 **NEXT DROP:** {event} returns in **{time_left}** 🚀 • {date}", "{emoji} 🔁 **PATCH CYCLE:** {event} again in **{time_left}** • {date}", "{emoji} 🔁 **SEASON RESET:** {event} in **{time_left}** 🎮 • {date}", "{emoji} 🔁 **RELOAD:** {event} in **{time_left}** ⚡ ({date})", "{emoji} 🔁 **NEXT UPDATE:** {event} in **{time_left}** • {date}"],
        "remindall_templates": ["{emoji} 🚀 **LAUNCH REMINDER:** {event} in **{time_left}** • {date}", "{emoji} 🎮 **DON'T MISS THE DROP:** {event} in **{time_left}** ({date})", "{emoji} 💾 **PRELOAD SOON:** {event} in **{time_left}** ⚡ • {date}", "{emoji} 🔓 **UNLOCK SOON:** {event} in **{time_left}** • {date}", "{emoji} 🕹️ **GET READY:** {event} in **{time_left}** ({date})"],
        "start_blast_templates": ["🚀 ⚡ **LIFTOFF!** ⚡ **{event} IS LIVE—GO PLAY!**", "🎮 **PRESS START:** {event} **DROPPED NOW** 🕹️ • Servers are up", "🔓 **UNLOCKED:** {event} **LIVE NOW** — download + dive in! 💾", "🛰️ **WE HAVE LAUNCH:** {event} **RIGHT NOW** — see you online! ⚡", "💾 **IT'S OUT:** {event} **TODAY** — day-one squad assemble! 🎮", "🎯 **DAY ONE:** {event} **NOW** — let's go! 🚀"],
    },
    "exam": {
        "label": "Exam Season",
        "supporter_only": True,
        "color": discord.Color.from_rgb(245, 158, 66),
        "pin_title_pool": ["📚 **EXAM COUNTDOWN** • Crunch Timer Active", "✏️ **STUDY MODE** • Deadline Tracker", "⏰ **FINALS WATCH** • Countdown Central", "📝 **TEST DAY AHEAD** • Revision Board", "🧠 **BRAIN PREP** • Study Timer", "📖 **CRAM SCHEDULE** • Big Test Loading", "🎓 **THE HOME STRETCH** • Exam Tracker"],
        "event_emoji_pool": ["📚", "✏️", "📝", "⏰", "🧠", "📖", "🎓", "☕"],
        "milestone_emoji_pool": ["📚", "✏️", "📝", "🧠", "⏰", "📖"],
        "milestone_templates": {
            "default": ["{emoji} 📚 **STUDY COUNTDOWN:** {event} in **{days} days** ({time_left}) • {date}", "{emoji} ✏️ **REVISION TIMER:** {event} — **{days} days** to go ({time_left})", "{emoji} 🧠 **BRAIN PREP:** {event} in **{days} days** • Flashcards ready ({time_left}) • {date}", "{emoji} 📝 **TEST AHEAD:** {event} — **{days} days** until the big one ({time_left})", "{emoji} 📖 **CRAM PLAN:** {event} in **{days} days** • Pace yourself ({time_left}) • {date}", "{emoji} ⏰ **DEADLINE WATCH:** {event} — **{days} days** left ({time_left})", "{emoji} 🎓 **HOME STRETCH:** {event} in **{days} days** • You've got this ({time_left}) • {date}", "{emoji} ☕ **STUDY FUEL:** {event} — **{days} days** out, keep going ({time_left})"],
            "one_day": ["{emoji} 📚 **EXAM TOMORROW:** {event} — final review 🌙 ({time_left}) • {date}", "{emoji} ✏️ **TEST DAY TOMORROW:** {event} • Sharpen the pencils ({time_left})", "{emoji} 🧠 **LAST REVISION:** {event} tomorrow — rest your brain too ({time_left}) • {date}", "{emoji} ⏰ **DEADLINE TOMORROW:** {event} • Submit on time ({time_left})", "{emoji} 📖 **ONE MORE SLEEP:** {event} tomorrow — you're ready ({time_left}) • {date}", "{emoji} ☕ **FINAL PUSH:** {event} tomorrow • Early night recommended ({time_left})"],
            "zero_day": ["{emoji} 📚 **EXAM DAY:** {event} **IS TODAY** — go ace it! ({time_left}) • {date}", "{emoji} ✏️ **TEST TIME:** {event} **NOW** — you studied for this! ({time_left})", "{emoji} 🧠 **BRAIN ONLINE:** {event} **TODAY** — deep breaths 🎓 ({time_left}) • {date}", "{emoji} ⏰ **DEADLINE IS NOW:** {event} **TODAY** — submit it! ({time_left})", "{emoji} 🎓 **GO GET IT:** {event} **RIGHT NOW** — you've got this! ({time_left})"],
        },
        "repeat_templates": ["{emoji} 🔁 **NEXT EXAM:** {event} comes around in **{time_left}** 📚 • {date}", "{emoji} 🔁 **STUDY CYCLE:** {event} again in **{time_left}** • {date}", "{emoji} 🔁 **DEADLINE LOOP:** {event} in **{time_left}** ⏰ • {date}", "{emoji} 🔁 **NEXT TEST:** {event} in **{time_left}** ✏️ ({date})", "{emoji} 🔁 **REVISION REPEATS:** {event} in **{time_left}** • {date}"],
        "remindall_templates": ["{emoji} 📚 **STUDY REMINDER:** {event} in **{time_left}** • {date}", "{emoji} ✏️ **TEST ALERT:** {event} in **{time_left}** ({date})", "{emoji} ⏰ **DEADLINE SOON:** {event} in **{time_left}** • {date}", "{emoji} 🧠 **REVISE NOW:** {event} in **{time_left}** 📖 • {date}", "{emoji} 🎓 **HEADS UP:** {event} in **{time_left}** ({date})"],
        "start_blast_templates": ["📚 🎓 **GO TIME!** 🎓 **{event} IS HERE—YOU'VE GOT THIS!**", "✏️ **TEST TIME:** {event} **STARTS NOW** — show what you know!", "🧠 **BRAIN ONLINE:** {event} **TODAY** — deep breath, you studied 📖", "⏰ **DEADLINE NOW:** {event} **RIGHT NOW** — submit it! ✅", "🎓 **HOME STRETCH:** {event} **LIVE** — go ace it! 📚", "☕ **FINAL PUSH:** {event} **NOW** — one last sprint! ✏️"],
    },
    "family": {
        "label": "Family & Kids",
        "supporter_only": True,
        "color": discord.Color.from_rgb(255, 145, 77),
        "pin_title_pool": ["🧸 **FAMILY COUNTDOWN** • Big Days Ahead", "🗓️ **KIDS' CALENDAR** • What's Coming Up", "🎒 **SCHEDULE CENTRAL** • Family Edition", "⭐ **UPCOMING ADVENTURES** • Mark the Day", "🚸 **THE FAMILY PLANNER** • Events Loaded", "🎈 **LITTLE MILESTONES** • Countdown Board", "📌 **DON'T-FORGET BOARD** • Family Events"],
        "event_emoji_pool": ["🧸", "🎒", "⭐", "🎈", "🚸", "🎨", "⚽", "🗓️"],
        "milestone_emoji_pool": ["🧸", "🎒", "⭐", "🎈", "🎨", "🗓️"],
        "milestone_templates": {
            "default": ["{emoji} 🗓️ **COMING UP:** {event} in **{days} days** ({time_left}) • {date}", "{emoji} 🎒 **MARK THE CALENDAR:** {event} — **{days} days** to go ({time_left})", "{emoji} ⭐ **BIG DAY INCOMING:** {event} in **{days} days** • Get ready ({time_left}) • {date}", "{emoji} 🎈 **FAMILY HEADS-UP:** {event} — **{days} days** away ({time_left})", "{emoji} 🚸 **DON'T FORGET:** {event} in **{days} days** ({time_left}) • {date}", "{emoji} 🎨 **SAVE THE DATE:** {event} — **{days} days** until the fun ({time_left})", "{emoji} 🧸 **COUNTDOWN ON:** {event} in **{days} days** • So close! ({time_left}) • {date}", "{emoji} ⚽ **ON THE SCHEDULE:** {event} — **{days} days** and counting ({time_left})"],
            "one_day": ["{emoji} 🗓️ **TOMORROW:** {event} — lay everything out tonight 🌙 ({time_left}) • {date}", "{emoji} 🎒 **BIG DAY TOMORROW:** {event} • Bags packed? ({time_left})", "{emoji} ⭐ **ALMOST HERE:** {event} tomorrow — early night! ({time_left}) • {date}", "{emoji} 🎈 **ONE MORE SLEEP:** {event} tomorrow ({time_left})", "{emoji} 🚸 **READY FOR TOMORROW?** {event} • Snacks + shoes set ({time_left}) • {date}", "{emoji} 🎨 **TOMORROW'S THE DAY:** {event} — can't wait! ({time_left})"],
            "zero_day": ["{emoji} ⭐ **TODAY'S THE DAY:** {event} is here! 🎉 ({time_left}) • {date}", "{emoji} 🎈 **IT'S HAPPENING:** {event} **TODAY** — have fun! ({time_left})", "{emoji} 🗓️ **GO TIME:** {event} **NOW** ({time_left}) • {date}", "{emoji} 🎒 **OUT THE DOOR:** {event} **TODAY** — you've got this! ({time_left})", "{emoji} 🧸 **THE BIG DAY:** {event} **RIGHT NOW** 🎉 ({time_left})"],
        },
        "repeat_templates": ["{emoji} 🔁 **EVERY TIME:** {event} comes around in **{time_left}** 🗓️ • {date}", "{emoji} 🔁 **REGULAR THING:** {event} again in **{time_left}** • {date}", "{emoji} 🔁 **BACK ON THE SCHEDULE:** {event} in **{time_left}** ({date})", "{emoji} 🔁 **NEXT ONE:** {event} in **{time_left}** ⭐ • {date}", "{emoji} 🔁 **ROUND AGAIN:** {event} in **{time_left}** • {date}"],
        "remindall_templates": ["{emoji} 📣 **FAMILY REMINDER:** {event} in **{time_left}** • {date}", "{emoji} 🗓️ **HEADS UP:** {event} in **{time_left}** ({date})", "{emoji} 🎒 **DON'T FORGET:** {event} in **{time_left}** • {date}", "{emoji} ⭐ **COMING SOON:** {event} in **{time_left}** 🎈 • {date}", "{emoji} 🚸 **MARK IT:** {event} in **{time_left}** ({date})"],
        "start_blast_templates": ["🧸 ⭐ **TODAY'S THE DAY!** ⭐ **{event} IS HERE — HAVE FUN!**", "🎈 **IT'S HAPPENING:** {event} **STARTS NOW** 🎉", "🗓️ **GO TIME:** {event} **TODAY** — out the door! 🎒", "⭐ **BIG DAY:** {event} **RIGHT NOW** — enjoy every minute! 🎨", "🎉 **HOORAY:** {event} **NOW** — make memories! 🧸", "🚸 **OFF WE GO:** {event} **STARTS NOW** — have a blast! ⚽"],
    },
}

FOOTER_POOLS: Dict[str, List[str]] = {
    "classic": ["💜 ChronoBot • Time is fake, reminders are real.", "⏳ Countdown Central • One timeline to rule them all.", "✨ Sparkle Energy • Keeping your chaos on schedule.", "🕒 Chrono Purple • /chronohelp for all commands", "📌 Supporter Feature • Unlock more with /vote", "🔔 Timer Ticking • Your timeline, perfected."],
    "football": ["🏈 Game Day • No timeouts on time.", "📺 Broadcast Mode • Play-by-play countdown.", "⏱️ Play Clock • Every second counts.", "🏟️ Stadium Mode • Schedule locked, kickoff incoming.", "🎙️ From the Booth • Supporter Feature", "🔥 Hype Meter • Main event energy."],
    "basketball": ["🏀 Tip-Off • Shot clock is running.", "⏱️ Court Time • Next game locked.", "🔥 Clutch Hour • Don't miss it.", "🏟️ Arena Lights • Championship energy.", "⛹️ Fast Break • Supporter Feature", "🎯 Dunk Alert • Bring the heat."],
    "baseball": ["⚾ Diamond Time • First pitch incoming.", "🧢 Dugout Alert • Game day locked.", "🏟️ Ballpark Board • No rain delays.", "📣 Play Ball • Schedule active.", "🔥 Bottom of the 9th • Supporter Feature", "🧤 Glove Time • Ready to field."],
    "raidnight": ["🎮 Raid Night • Ready check in progress.", "🛡️ Party Finder • Boss queue loaded.", "⚔️ Pull Timer • Gold incoming.", "🏆 Loot Council • Timers > excuses.", "🧩 Objective HUD • /chronohelp for commands", "💎 Loot Tracker • Supporter Feature"],
    "dnd": ["🎲 Campaign Night • Roll initiative (on time).", "🐉 Dragon's Lair • Respect the schedule.", "📜 The Next Chapter • /chronohelp for commands", "🕯️ Tavern Board • Session locked.", "🗺️ Quest Log • Adventure awaits.", "⚔️ Fellowship Timer • Supporter Feature"],
    "girly": ["🎀 Cute Aesthetic • Sparkle mode activated.", "💖 Soft Schedule • Your calendar, made cute.", "✨ Pretty Timing • /chronohelp for commands", "🌸 Sweet Reminder • Future-you says thanks.", "🫧 Bubble Tea Break • Supporter Feature", "👑 That Girl Energy • You've got this."],
    "workplace": ["📌 Operations Board • Clear dates, clean execution.", "📋 Action Items • /chronohelp for commands", "✅ Task Management • Planning > firefighting.", "🗓️ Calendar Sync • Keep the machine humming.", "📊 Metrics Incoming • Supporter Feature", "⏱️ On Schedule • Meetings locked."],
    "celebration": ["🎉 Celebration Mode • Confetti pending.", "🎊 Party Incoming • Don't forget the good stuff.", "🥳 Good Times • /chronohelp for commands", "🍾 Toast Time • The countdown is the fun part.", "✨ Sparkle Energy • Supporter Feature", "🏆 Victory Countdown • Make it legendary."],
    "romance": ["💞 Date Night • Soft plans, strong intentions.", "🌹 Love's Calendar • /chronohelp for commands", "💌 Love Notes • Keep the magic alive.", "✨ Starlit Moments • Timing is everything.", "💕 Heart Sync • Supporter Feature", "🦋 Butterfly Bloom • Love grows here."],
    "vacation": ["🧳 Vacation Mode • Out of office (mentally).", "✈️ Departure Board • /chronohelp for commands", "🌴 Getaway Mode • Countdown to freedom.", "🌊 Wave Rider • Future-you is packing.", "☀️ Vitamin Sea • Supporter Feature", "🏖️ Beach Brain • The trip starts when you plan."],
    "hype": ["🚀 Hype Mode • Main character scheduling.", "🔥 Big Energy • /chronohelp for commands", "⚡ Incoming • Don't blink, it's soon.", "💥 Countdown Heat • We love drama.", "🌟 Momentum Building • Supporter Feature", "✨ Peak Vibes • Future you is screaming."],
    "minimal": ["• Minimal • /chronohelp", "⏱️ Simple timers. Clean schedule.", "▫️ Less clutter. More clarity.", "— Planning > panic.", "· Supporter Feature", "• ChronoBot • Quietly keeping time."],
    "school": ["📚 School Mode • Study now, celebrate later.", "📝 Syllabus Mode • /chronohelp for commands", "✅ Prep Checklist • Due dates don't negotiate.", "🧠 Focus Time • Small steps, big grades.", "⏳ Deadline Energy • Supporter Feature", "💪 You Got This • Start early, finish calm."],
    "spooky": ["🎃 Spooky Season • The clock creaks closer.", "🕯️ Witching Hour • /chronohelp for commands", "👻 Haunted Schedule • Time is watching.", "🦇 Midnight Mode • The countdown stirs.", "🕷️ Web Weaver • Supporter Feature", "💀 Skeleton Key • Secrets unlock."],
}

for theme_id, pool in FOOTER_POOLS.items():
    if theme_id in THEMES:
        THEMES[theme_id]["footer_pool"] = pool

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
    "birthday": "Birthday & Baby 🎂",
    "wedding": "Wedding 💍",
    "gamelaunch": "Game Launch 🚀",
    "exam": "Exam Season 📚",
    "family": "Family & Kids 🧸",
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
    "birthday": {
        "title": "🎂 Birthday Countdown Board",
        "subtitle": "Cakes, gifts, and big-day vibes incoming.",
        "footer": "Updated every minute • Don’t forget the candles",
        "color": discord.Color.from_rgb(255, 105, 180),
        "emoji": "🎈",
    },
    "wedding": {
        "title": "💍 Countdown to I Do",
        "subtitle": "Save the date — happily ever after loading.",
        "footer": "Updated every minute • Something timed, something blue",
        "color": discord.Color.from_rgb(214, 174, 148),
        "emoji": "💐",
    },
    "gamelaunch": {
        "title": "🚀 Launch Countdown",
        "subtitle": "Day-one drops and release timers.",
        "footer": "Updated every minute • Preload before you’re late",
        "color": discord.Color.from_rgb(0, 209, 178),
        "emoji": "🎮",
    },
    "exam": {
        "title": "📚 Exam Season Countdown",
        "subtitle": "Deadlines, finals, and focus sessions.",
        "footer": "Updated every minute • Start early, stress less",
        "color": discord.Color.from_rgb(245, 158, 66),
        "emoji": "✏️",
    },
    "family": {
        "title": "🧸 Family Countdown Board",
        "subtitle": "Upcoming days the whole family's waiting for.",
        "footer": "Updated every minute • Never miss a big day",
        "color": discord.Color.from_rgb(255, 145, 77),
        "emoji": "⭐",
    },
}

def get_theme_layout(guild_state: dict, theme_id: Optional[str] = None) -> dict:
    tid = (theme_id or guild_state.get("theme") or "classic")
    tid = str(tid).lower()

    if tid == "custom":
        # Pro build-your-own: read the per-channel custom_theme bucket; classic is
        # the base for anything the user didn't set.
        layout = THEME_LAYOUTS["classic"].copy()
        ct = guild_state.get("custom_theme") if isinstance(guild_state, dict) else None
        if isinstance(ct, dict):
            if ct.get("title"):
                layout["title"] = str(ct["title"])[:256]
            if ct.get("subtitle") is not None:
                layout["subtitle"] = str(ct["subtitle"])
            if ct.get("footer") is not None:
                layout["footer"] = str(ct["footer"])
            if ct.get("emoji"):
                layout["emoji"] = str(ct["emoji"])
            if isinstance(ct.get("color"), int):
                try:
                    layout["color"] = discord.Color(ct["color"])
                except Exception:
                    pass
    else:
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

def build_embed_for_channel(channel_state: dict, guild_state: dict) -> discord.Embed:
    """Render the pinned countdown embed for ONE channel's countdown.

    Per-channel data (events, theme, timezone, title/description overrides,
    time_unit) comes from `channel_state`; the server-level Supporter/Pro status
    line comes from `guild_state` (the two are the same dict only for a guild
    that has not been migrated to the per-channel model).
    """
    layout = get_theme_layout(channel_state) or {}

    # Harden events
    events = channel_state.get("events", [])
    if not isinstance(events, list):
        events = []
    events = list(events)  # shallow copy of list

    tz = get_guild_timezone(channel_state)

    now = datetime.now(tz)

    # Sort by timestamp so "next upcoming" logic is true
    def _ts(ev):
        try:
            return float(ev.get("timestamp", 0))
        except Exception:
            return 0.0
    events.sort(key=_ts)

    override_title = (channel_state.get("countdown_title_override") or "").strip()
    embed_title = override_title[:256] if override_title else (layout.get("title") or "Event Countdown")[:256]

    embed = discord.Embed(
        title=embed_title,
        color=layout.get("color", discord.Color.from_rgb(140, 82, 255)),  # safe default
    )

    emoji = layout.get("emoji", "🕒")
    time_unit = channel_state.get("time_unit", "discord")

    # Theme-provided subtitle/heading (fallback)
    theme_subtitle = layout.get("subtitle", layout.get("description", "📅 Upcoming events:"))

    # NEW: Supporter custom intro shown above the list
    custom_intro = (channel_state.get("countdown_description_override") or "").strip()

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
            dt = datetime.fromtimestamp(float(ev["timestamp"]), tz=tz)
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
        
        # Use dynamic Discord timestamps (from spec) - client-side updates!
        unix_ts = int(dt.timestamp())

        when_str = dt.strftime("%A, %d %B %Y at %H:%M %Z")

        if time_unit == "discord":
            countdown_str = f"<t:{unix_ts}:R>"
        else:
            countdown_str = format_time_unit(int(delta.total_seconds()), time_unit)

        lines = [
            f"{emoji} **{name}**",
            f"**When:** {when_str}",
            f"**Countdown:** {countdown_str}",
        ]

        # Only show owner if explicitly set via /seteventowner
        # Don't show creator (they're just the one who added the event)
        owner_id = ev.get("owner_user_id")
        owner_name = ev.get("owner_name")

        # allow int-like strings too
        if isinstance(owner_id, str) and owner_id.isdigit():
            owner_id = int(owner_id)

        # Only display if owner was explicitly set (has a valid owner_id or owner_name)
        if isinstance(owner_id, int) and owner_id > 0:
            lines.append(f"👤 Hosted by <@{owner_id}>")
        elif isinstance(owner_name, str) and owner_name.strip() and owner_id:
            # Only show name if there's also an ID (to ensure it was set via /seteventowner)
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
        footer = f"{footer} • Times update automatically"
        embed.set_footer(text=footer[:2048])
    else:
        embed.set_footer(text="Times update automatically • Use /chronohelp for commands")

    if banner_url:
        embed.set_image(url=banner_url)

    return embed




async def rebuild_pinned_message_for_channel(
    channel: discord.TextChannel, channel_state: dict, guild_state: dict
):
    """Rebuild the pinned countdown for ONE channel bucket (unpin old, send + pin new)."""
    sort_events(channel_state)

    old_id = channel_state.get("pinned_message_id")
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

    embed = build_embed_for_channel(channel_state, guild_state)

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
        # but this keeps rebuild from ever crashing.
        pass

    channel_state["pinned_message_id"] = msg.id
    save_state()
    return msg



async def get_or_create_pinned_message_for_channel(
    channel: discord.TextChannel,
    state_bucket: dict,
    guild_state: dict,
    *,
    allow_create: bool = False,
):
    """Per-channel pinned-message resolver. `state_bucket` holds this channel's
    events + pinned_message_id; `guild_state` is used for the embed status line."""
    sort_events(state_bucket)
    pinned_id = state_bucket.get("pinned_message_id")

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
            state_bucket["pinned_message_id"] = None
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
            pins = [message async for message in channel.pins()]
            bot_pins = [m for m in pins if m.author and m.author.id == bot_member.id]
            if bot_pins:
                m = max(bot_pins, key=lambda x: x.created_at)
                state_bucket["pinned_message_id"] = m.id
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

    embed = build_embed_for_channel(state_bucket, guild_state)
    try:
        msg = await channel.send(embed=embed)
        await ensure_countdown_pinned(channel.guild, channel, msg, perms=perms)

        # Cleanup old bot pins only if we can read history AND manage pins
        if perms.manage_messages and perms.read_message_history:
            try:
                async for m in channel.pins():
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

    state_bucket["pinned_message_id"] = msg.id
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
    # Owner DMs are opt-in per event (default OFF). Set via the /event hub.
    if not ev.get("dm_opt_in"):
        return
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

async def refresh_countdown_message_for_channel(
    guild: discord.Guild, channel: discord.TextChannel, channel_state: dict, guild_state: dict
) -> None:
    """Edit the pinned countdown for ONE channel in place (no repost/repin).

    Use this for settings tweaks (title/description/theme/etc.) where rebuilding
    would needlessly delete + resend the pin. Falls back to creating the pin if
    one doesn't exist yet."""
    pinned = await get_or_create_pinned_message_for_channel(
        channel, channel_state, guild_state, allow_create=True
    )
    if pinned is None:
        return

    try:
        await pinned.edit(embed=build_embed_for_channel(channel_state, guild_state))
    except discord.NotFound:
        if channel_state.get("pinned_message_id") == pinned.id:
            channel_state["pinned_message_id"] = None
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
    _cid, cs = resolve_event_channel(g, interaction.channel_id)
    if cs is None:
        return []
    tz = get_guild_timezone(cs)
    sort_events(cs)

    now = datetime.now(tz)
    cur = (current or "").strip().lower()
    grace = timedelta(seconds=EVENT_START_GRACE_SECONDS)

    choices: List[app_commands.Choice[int]] = []

    for idx, ev in enumerate(cs.get("events", []), start=1):
        ts = ev.get("timestamp")
        if not isinstance(ts, (int, float)):
            continue

        try:
            dt = datetime.fromtimestamp(ts, tz=tz)
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
    for gid_str, guild_state in list(state.get("guilds", {}).items()):
        for cid, channel_state in iter_channel_states(guild_state):
            try:
                tz = get_guild_timezone(channel_state)
                now = datetime.now(tz)

                # Send once each Monday any time after 9:00 AM local (per-channel timezone).
                if now.weekday() != 0:  # Monday = 0
                    continue
                if now.hour < 9:
                    continue

                today_str = now.date().isoformat()
                now_ts = int(now.timestamp())
                cutoff_ts = now_ts + (7 * 86400)

                d = channel_state.get("digest")
                if not isinstance(d, dict) or not d.get("enabled"):
                    continue
                if d.get("last_sent_date") == today_str:
                    continue

                # Default digest target is the countdown channel itself.
                ch_id = d.get("channel_id") or cid
                channel = await get_text_channel(int(ch_id))
                if channel is None:
                    continue

                sort_events(channel_state)

                upcoming = []
                for ev in channel_state.get("events", []):
                    ts = ev.get("timestamp")
                    if isinstance(ts, int) and now_ts < ts <= cutoff_ts:
                        dt = datetime.fromtimestamp(ts, tz=tz)
                        desc, _, _ = compute_time_left(now, dt)
                        upcoming.append(
                            f"• **{ev.get('name', 'Event')}** — {dt.strftime('%m/%d %I:%M %p')} ({desc})"
                        )

                text = "📬 **Weekly Digest (Next 7 days)**\n"
                text += "\n".join(upcoming[:15]) if upcoming else "No events in the next 7 days."

                await channel.send(text, allowed_mentions=discord.AllowedMentions.none())

                d["last_sent_date"] = today_str
                channel_state["digest"] = d
                save_state()

            except Exception as e:
                print(f"[Digest] guild {gid_str} / channel {cid} failed: {type(e).__name__}: {e}")
                continue


@weekly_digest_loop.before_loop
async def before_weekly_digest_loop():
    await bot.wait_until_ready()

async def _run_countdown_cycle(guild_id, guild_state, channel, bot_member, server_state):
    # NOTE: `guild_state` here is the per-channel bucket (named this way so the
    # long loop body below needs no changes); `server_state` is the guild dict,
    # used only for the server-level Supporter/Pro status line in the embed.
    if True:
        if True:
            sort_events(guild_state)

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
            tz = get_guild_timezone(guild_state)
            today = datetime.now(tz).date()
            now = datetime.now(tz)
            now_dt = now
            for ev in list(guild_state.get("events", [])):
                if ev.get("silenced", False):
                    continue

                ts = ev.get("timestamp")
                if not isinstance(ts, (int, float)):
                    continue

                try:
                    dt = datetime.fromtimestamp(ts, tz=tz)
                except Exception:
                    continue
                # ----------------------------
                # ✅ Reminder tracking defaults + auto-delete 24h after sending
                # ----------------------------
                ev.setdefault("reminder_messages", [])      # [{channel_id, message_id, sent_at}, ...]

                # Harden types (older saved states)
                if not isinstance(ev.get("reminder_messages"), list):
                    ev["reminder_messages"] = []
                    mark_dirty()

                # Auto-delete reminder messages 24 hours after they were sent
                # (skip if server has auto-delete disabled — Supporter perk)
                msgs = ev.get("reminder_messages", []) or []
                if msgs and guild_state.get("auto_delete_milestones", True):
                    current_time = time.time()
                    remaining_msgs = []
                    had_forbidden = False
                    state_changed = False

                    for item in msgs:
                        try:
                            ch_id = int(item.get("channel_id") or channel.id)
                            msg_id = int(item.get("message_id") or 0)
                        except Exception:
                            continue

                        if msg_id <= 0:
                            continue

                        # Check if message is older than 24 hours
                        sent_at = item.get("sent_at")
                        if sent_at is None:
                            # Legacy message without sent_at - use old behavior (delete after event + 24h)
                            if now_dt >= (dt + timedelta(seconds=MILESTONE_CLEANUP_AFTER_EVENT_SECONDS)):
                                sent_at = 0  # Force deletion
                            else:
                                remaining_msgs.append(item)
                                continue

                        message_age = current_time - sent_at
                        if message_age < MILESTONE_CLEANUP_AFTER_EVENT_SECONDS:
                            # Message is less than 24h old, keep it
                            remaining_msgs.append(item)
                            continue

                        # Message is 24h+ old, delete it
                        if had_forbidden:
                            # Skip further deletes if we already hit a permission error
                            continue

                        ch = await get_text_channel(ch_id)
                        if ch is None:
                            state_changed = True
                            continue

                        try:
                            await ch.get_partial_message(msg_id).delete()
                            state_changed = True
                        except discord.Forbidden:
                            had_forbidden = True
                            state_changed = True
                            # Notify once then stop trying
                            try:
                                missing = missing_channel_perms(ch, ch.guild)
                                await notify_owner_missing_perms(
                                    ch.guild,
                                    ch,
                                    missing=missing,
                                    action="delete old reminder messages (needs Manage Messages)",
                                )
                            except Exception:
                                pass
                        except (discord.NotFound, discord.HTTPException):
                            state_changed = True
                            pass  # already gone or transient

                    # Update the list to only keep messages we didn't delete
                    if state_changed or len(remaining_msgs) != len(msgs):
                        ev["reminder_messages"] = remaining_msgs
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

                                # track for auto-delete 24h after sending
                                ev.setdefault("reminder_messages", []).append(
                                    {"channel_id": channel.id, "message_id": m.id, "sent_at": time.time()}
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

                days_left = calendar_days_left(dt, now=now)
                if days_left < 0:
                    continue

                milestone_sent_today = False

                milestones = ev.get("milestones", DEFAULT_MILESTONES)
                announced = ev.get("announced_milestones", [])
                if not isinstance(announced, list):
                    announced = []
                    ev["announced_milestones"] = announced
                    mark_dirty()  # you changed the event dict

                if days_left in milestones and days_left not in announced and should_send_reminder_based_on_time(ev, now, dt):
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

                        # ✅ track for auto-delete 24h after sending
                        ev.setdefault("reminder_messages", []).append(
                            {"channel_id": channel.id, "message_id": m.id, "sent_at": time.time()}
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

                        if today.isoformat() not in sent_dates and not milestone_sent_today and should_send_reminder_based_on_time(ev, now, dt):
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

                                # ✅ track for cleanup 24h after send time
                                ev.setdefault("reminder_messages", []).append(
                                    {"channel_id": channel.id, "message_id": m.id, "sent_at": time.time()}
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
                now=datetime.now(get_guild_timezone(guild_state)) - timedelta(seconds=MILESTONE_CLEANUP_AFTER_EVENT_SECONDS),
            )
            if removed:
                mark_dirty()
                # (No immediate flush needed; no public post happened.)

            # ---- Update pinned embed once at end (reflects changes) ----
            try:
                pinned = await get_or_create_pinned_message_for_channel(channel, guild_state, server_state, allow_create=True)
            except Exception:
                print(f"[Guild {guild_id}] get_or_create_pinned_message failed:\n{traceback.format_exc()}")
                pinned = None

            if pinned is not None:
                try:
                    embed = build_embed_for_channel(guild_state, server_state)
                except Exception:
                    print(f"[Guild {guild_id}] build_embed_for_channel failed:\n{traceback.format_exc()}")
                    embed = None

                if embed is not None:
                    try:
                        await pinned.edit(embed=embed)
                    except discord.NotFound:
                        if guild_state.get("pinned_message_id") == pinned.id:
                            guild_state["pinned_message_id"] = None
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

            # ✅ Final flush: saves prune/anchor fixes/etc once per channel cycle
            flush_if_dirty()


@tasks.loop(seconds=UPDATE_INTERVAL_SECONDS)
async def update_countdowns():
    """Per-channel countdown engine: for each guild, refresh every countdown
    channel's milestones, start blasts, repeats, pruning, and pinned embed."""
    guilds = state.get("guilds", {})
    for gid_str, guild_state in list(guilds.items()):
        try:
            guild_id_int = int(gid_str)
        except (TypeError, ValueError):
            continue
        for cid, channel_state in iter_channel_states(guild_state):
            try:
                channel = await get_text_channel(cid)
                if channel is None:
                    continue
                bot_member = await get_bot_member(channel.guild)
                if bot_member is None:
                    continue
                await _run_countdown_cycle(
                    guild_id_int, channel_state, channel, bot_member, guild_state
                )
            except Exception as e:
                print(f"[Guild {gid_str} / channel {cid}] update_countdowns crashed: {type(e).__name__}: {e}")
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
    await interaction.response.defer(ephemeral=True, thinking=True)

    voted = await topgg_has_voted(interaction.user.id, force=True)
    status = "✅ You currently have supporter access." if voted else "❌ You don’t have an active vote yet."

    await interaction.followup.send(
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

        tz = get_guild_timezone(guild_state)
        dt = datetime.fromtimestamp(ts, tz=tz)
        now = datetime.now(tz)
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


@bot.tree.command(name="seteventchannel", description="Turn this channel into a countdown channel.")
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
    new_channel = interaction.channel
    channels = guild_state.setdefault("channels", {})

    # If this channel is already a countdown channel, no-op (don't spam).
    if str(new_channel.id) in channels:
        await interaction.edit_original_response(
            content="✅ This channel is already a countdown channel."
        )
        return

    # Gate: 1 countdown channel is free; additional channels need Chromie Pro.
    if not can_add_countdown_channel(guild_state, new_channel.id):
        embed = discord.Embed(
            title="💎 Multiple countdown channels are a Chromie Pro feature",
            description=(
                "This server already has its free countdown channel. "
                "With **Chromie Pro ($2.99/month)** you can run an independent "
                "countdown in *every* channel — each with its own events, theme, "
                "timezone and digest.\n\n"
                "Subscribe via **Discord Server Subscription**, then run "
                "`/seteventchannel` here again. Use `/pro_status` to check status."
            ),
            color=discord.Color.orange(),
        )
        await interaction.edit_original_response(content=None, embed=embed)
        return

    # First countdown channel for this guild? Adopt any events the migration
    # parked under the "unassigned" sentinel (events that existed before a
    # channel was ever set) so nothing is lost.
    if "unassigned" in channels:
        channels[str(new_channel.id)] = channels.pop("unassigned")

    cs = get_channel_state(guild.id, new_channel.id)
    cs["pinned_message_id"] = None  # force a fresh pin for this channel

    # audit fields (optional)
    cs["event_channel_set_by"] = int(interaction.user.id)
    cs["event_channel_set_at"] = int(time.time())

    sort_events(cs)
    save_state()

    # Permissions check. Split into perms that BLOCK the countdown (it can't post)
    # vs perms that merely DEGRADE it (posts fine, just can't pin / auto-clean), so
    # we don't cry wolf when the countdown actually works.
    missing: list[str] = []
    blocking: list[str] = []
    degraded: list[str] = []
    if hasattr(new_channel, "permissions_for"):
        missing = missing_channel_perms(new_channel, guild)
        blocking, degraded = classify_missing_perms(missing)
        # Only DM the owner the "quick fix" guide when the countdown is actually
        # broken — a missing pin perm doesn't warrant an owner DM.
        if blocking:
            await notify_owner_missing_perms(
                guild,
                new_channel,
                missing=blocking,
                action="post the countdown (it can't show until this is fixed)",
            )

    # 🔔 Notify owner + optionally post an audit note in the channel.
    await notify_event_channel_changed(
        guild,
        actor=interaction.user,
        old_channel_id=None,
        new_channel=new_channel,
    )

    # Build the pinned countdown immediately so the channel shows something.
    await rebuild_pinned_message_for_channel(new_channel, cs, guild_state)

    def _perm_names(codes):
        return ", ".join(f"**{PERM_LABELS.get(p, p)}**" for p in codes)

    if blocking:
        extra = (
            f"\n\n⚠️ I’m missing {_perm_names(blocking)} here, so the countdown can’t post yet. "
            "I’ve messaged the server owner with a quick fix guide — grant those and it’ll appear."
        )
    elif degraded:
        extra = (
            f"\n\nℹ️ The countdown is posted. Heads-up: I’m missing {_perm_names(degraded)} here — "
            "not required, but granting it lets me keep the countdown pinned and tidy."
        )
    else:
        extra = ""

    await interaction.edit_original_response(
        content="✅ This channel is now a countdown channel.\nUse `/addevent` here to add events." + extra
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

# ==========================
# /countdown HUB — per-channel countdown settings (Select + Modal/Buttons)
# ==========================
# Replaces the old /countdown title|description group, /digest, /theme,
# /timeformat, /timezone_set|view, /setmentionrole|clearmentionrole and
# /resetchannel. Everything edits the bucket for the countdown channel the
# command is run in (resolved via resolve_event_channel).

_TIME_UNIT_LABELS = {
    "discord": "Discord native (e.g. 'in 3 months')",
    "days": "Days (e.g. '90 days')",
    "weeks": "Weeks (e.g. '12 weeks')",
    "detailed": "Detailed (e.g. '12 weeks and 6 days')",
}


def build_countdown_settings_embed(cs: dict, guild_state: dict, channel_id: int) -> discord.Embed:
    """Render the current settings for one countdown channel."""
    theme_id = normalize_theme_key(cs.get("theme"))
    theme_label = "Build-Your-Own 🎨" if theme_id == "custom" else _THEME_LABELS.get(theme_id, theme_id.title())
    tz = cs.get("timezone") or "UTC"
    unit = cs.get("time_unit", "discord")
    role_id = cs.get("mention_role_id")
    role_txt = f"<@&{int(role_id)}>" if role_id else "—"
    title_ov = cs.get("countdown_title_override") or "— (theme default)"
    desc_ov = cs.get("countdown_description_override")
    if desc_ov:
        desc_txt = (desc_ov[:80] + "…") if len(desc_ov) > 80 else desc_ov
    else:
        desc_txt = "—"
    digest = cs.get("digest") or {}
    digest_txt = "✅ On" if digest.get("enabled") else "🔕 Off"
    autodelete_txt = "✅ After 24h" if cs.get("auto_delete_milestones", True) else "🔕 Keep forever"
    pro = is_pro(guild_state)
    lock = "" if pro else " 🔒"

    e = discord.Embed(
        title="⚙️ Countdown Settings",
        description=f"Editing the countdown in <#{channel_id}>.\nPick a setting below to change it.",
        color=EMBED_COLOR,
    )
    e.add_field(name="🎨 Theme", value=theme_label, inline=True)
    e.add_field(name="🌍 Timezone", value=f"`{tz}`", inline=True)
    e.add_field(name="⏱️ Time format", value=_TIME_UNIT_LABELS.get(unit, unit), inline=True)
    e.add_field(name="🔔 Mention role", value=role_txt, inline=True)
    e.add_field(name="🧹 Auto-delete posts", value=autodelete_txt, inline=True)
    e.add_field(name=f"📝 Title{lock}", value=title_ov, inline=True)
    e.add_field(name=f"💬 Description{lock}", value=desc_txt, inline=True)
    e.add_field(name=f"📊 Weekly digest{lock}", value=digest_txt, inline=True)
    if not pro:
        e.set_footer(text="🔒 = Chromie Pro ($2.99/mo). Free servers get 1 countdown channel.")
    return e


_MONTH_ABBR = ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def theme_in_season(theme_id: str, now: Optional[datetime] = None) -> bool:
    """True if a theme has no season window, or the current month is in it.
    `season_months` is a list of month numbers (1-12); absent = always available."""
    months = THEMES.get(theme_id, {}).get("season_months")
    if not months:
        return True
    if now is None:
        now = datetime.now(timezone.utc)
    return now.month in months


def theme_is_pro_only(theme_id: str) -> bool:
    """True if a theme requires Chromie Pro specifically (a Top.gg vote won't unlock it)."""
    return bool(THEMES.get(theme_id, {}).get("pro_only"))


def _season_label(theme_id: str) -> str:
    months = THEMES.get(theme_id, {}).get("season_months") or []
    return "/".join(_MONTH_ABBR[m] for m in months if 1 <= m <= 12)


def theme_picker_description(theme_id: str, guild_state: dict, now: Optional[datetime] = None) -> Optional[str]:
    """Short label shown under a theme in the /countdown Theme picker (or None)."""
    if not theme_in_season(theme_id, now):
        return f"🗓️ {_season_label(theme_id)} only"
    pro = is_pro(guild_state)
    if theme_is_pro_only(theme_id) and not pro:
        return "💎 Pro only"
    if THEMES.get(theme_id, {}).get("supporter_only") and not (pro or has_active_vote_guild(guild_state)):
        return "🔒 Supporter / Pro"
    return None


def build_theme_preview_embed(theme_id: str, guild_state: dict) -> discord.Embed:
    """Render a sample countdown in `theme_id` so admins can preview before applying."""
    now = datetime.now(timezone.utc)
    sample_cs = {
        "theme": theme_id,
        "timezone": "UTC",
        "time_unit": "discord",
        "events": [
            {"name": "Sample Event", "timestamp": int((now + timedelta(days=3, hours=5)).timestamp())},
            {"name": "Another Milestone", "timestamp": int((now + timedelta(days=24)).timestamp())},
        ],
    }
    return build_embed_for_channel(sample_cs, guild_state)


async def _countdown_refresh_pin(guild_id: int, channel_id: int) -> None:
    """Best-effort: re-render the pinned countdown after a settings change."""
    g = get_guild_state(guild_id)
    cs = get_channel_state(guild_id, channel_id)
    guild = bot.get_guild(guild_id)
    ch = await get_text_channel(channel_id)
    if guild and ch:
        try:
            await refresh_countdown_message_for_channel(guild, ch, cs, g)
        except Exception:
            pass


def _countdown_hub_embed_with_note(guild_id: int, channel_id: int, note: str) -> discord.Embed:
    g = get_guild_state(guild_id)
    cs = get_channel_state(guild_id, channel_id)
    embed = build_countdown_settings_embed(cs, g, channel_id)
    embed.description = f"{note}\n\n{embed.description}"
    return embed


async def _countdown_apply(interaction: discord.Interaction, guild_id: int, channel_id: int, *, confirm: str):
    """Component path: refresh pin + re-render the hub in place (edit_message)."""
    await _countdown_refresh_pin(guild_id, channel_id)
    embed = _countdown_hub_embed_with_note(guild_id, channel_id, confirm)
    await interaction.response.edit_message(embed=embed, view=CountdownHubView(guild_id, channel_id))


async def _countdown_apply_via_parent(parent: discord.Interaction, guild_id: int, channel_id: int, *, confirm: str):
    """Modal path: refresh pin + re-render the hub on the parent component message."""
    await _countdown_refresh_pin(guild_id, channel_id)
    embed = _countdown_hub_embed_with_note(guild_id, channel_id, confirm)
    try:
        await parent.edit_original_response(embed=embed, view=CountdownHubView(guild_id, channel_id))
    except Exception:
        pass


class CountdownBackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Back", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        g = get_guild_state(view.guild_id)
        cs = get_channel_state(view.guild_id, view.channel_id)
        await interaction.response.edit_message(
            embed=build_countdown_settings_embed(cs, g, view.channel_id),
            view=CountdownHubView(view.guild_id, view.channel_id),
        )


class CountdownSubView(discord.ui.View):
    """Generic wrapper: one sub-control (a Select) plus a Back button."""
    def __init__(self, guild_id: int, channel_id: int, item: discord.ui.Item):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.add_item(item)
        self.add_item(CountdownBackButton())


class CountdownThemeSelect(discord.ui.Select):
    def __init__(self, guild_state: dict):
        options = []
        for tid, label in _THEME_LABELS.items():
            options.append(discord.SelectOption(
                label=label[:100],
                value=tid,
                description=theme_picker_description(tid, guild_state),
            ))
        super().__init__(placeholder="Pick a theme…", min_values=1, max_values=1, options=options[:25])

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        gid, cid = view.guild_id, view.channel_id
        g = get_guild_state(gid)
        tid = normalize_theme_key(self.values[0])
        if tid not in THEMES:
            await interaction.response.send_message("Unknown theme.", ephemeral=True)
            return
        # Preview first (free, even for locked themes — it's also a nice upsell).
        # The supporter/Pro gate is enforced on Apply, not here.
        await interaction.response.edit_message(
            content=f"🔍 **Preview — {_THEME_LABELS.get(tid, tid)}** (not applied yet)",
            embed=build_theme_preview_embed(tid, g),
            view=CountdownThemePreviewView(gid, cid, tid),
        )


class CountdownThemeApplyButton(discord.ui.Button):
    def __init__(self, label: str, emoji: str):
        super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.success, row=0)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        gid, cid, tid = view.guild_id, view.channel_id, view.theme_id
        g = get_guild_state(gid)
        label = _THEME_LABELS.get(tid, tid)
        # Seasonal themes can be previewed year-round but only applied in season.
        if not theme_in_season(tid):
            await interaction.response.send_message(
                f"🗓️ **{label}** is a seasonal theme — it returns in **{_season_label(tid)}**. "
                "You can preview it anytime!",
                ephemeral=True,
            )
            return
        # Pro-only themes: a Top.gg vote does NOT unlock these.
        if theme_is_pro_only(tid) and not is_pro(g):
            await interaction.response.send_message(
                f"💎 **{label}** is a Chromie Pro–exclusive theme ($2.99/mo). Voting doesn't unlock "
                "this one — subscribe via Discord Server Subscription.",
                ephemeral=True,
            )
            return
        # Supporter themes: Pro or an active vote from the caller.
        if THEMES.get(tid, {}).get("supporter_only") and not is_pro(g):
            if not await topgg_has_voted(interaction.user.id, force=True):
                await send_vote_required(interaction, feature_label=f"`{label}` theme")
                return
        cs = get_channel_state(gid, cid)
        cs["theme"] = tid
        save_state()
        await _countdown_refresh_pin(gid, cid)
        embed = _countdown_hub_embed_with_note(gid, cid, f"✅ Theme set to **{label}**.")
        await interaction.response.edit_message(content=None, embed=embed, view=CountdownHubView(gid, cid))


class CountdownThemePreviewBackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Back to themes", emoji="⬅️", style=discord.ButtonStyle.secondary, row=0)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        gid, cid = view.guild_id, view.channel_id
        g = get_guild_state(gid)
        await interaction.response.edit_message(
            content=None,
            embed=discord.Embed(
                title="🎨 Pick a theme",
                description="Pick one to preview it, then Apply. Supporter themes need an active `/vote` or Chromie Pro.",
                color=EMBED_COLOR,
            ),
            view=CountdownSubView(gid, cid, CountdownThemeSelect(g)),
        )


class CountdownThemePreviewView(discord.ui.View):
    def __init__(self, guild_id: int, channel_id: int, theme_id: str):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.theme_id = theme_id
        g = get_guild_state(guild_id)
        label, emoji = self._apply_button_state(theme_id, g)
        self.add_item(CountdownThemeApplyButton(label, emoji))
        self.add_item(CountdownThemePreviewBackButton())

    @staticmethod
    def _apply_button_state(theme_id: str, guild_state: dict) -> tuple[str, str]:
        pro = is_pro(guild_state)
        if not theme_in_season(theme_id):
            return "Apply (out of season)", "🗓️"
        if theme_is_pro_only(theme_id) and not pro:
            return "Apply (Pro only)", "💎"
        if THEMES.get(theme_id, {}).get("supporter_only") and not (pro or has_active_vote_guild(guild_state)):
            return "Apply (Supporter/Pro)", "🔒"
        return "Apply this theme", "✅"


class CountdownTimeFormatSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=lbl[:100], value=val) for val, lbl in _TIME_UNIT_LABELS.items()]
        super().__init__(placeholder="Pick a time format…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        gid, cid = view.guild_id, view.channel_id
        cs = get_channel_state(gid, cid)
        cs["time_unit"] = self.values[0]
        save_state()
        await _countdown_apply(interaction, gid, cid, confirm=f"✅ Time format set to **{_TIME_UNIT_LABELS[self.values[0]]}**.")


class CountdownRoleSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(placeholder="Pick a role to mention…", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        gid, cid = view.guild_id, view.channel_id
        role = self.values[0]
        if role.is_default():
            await interaction.response.send_message(
                "⚠️ You can't use **@everyone** as the mention role.\n"
                "Give Chromie the **Mention Everyone** permission instead.",
                ephemeral=True,
            )
            return
        cs = get_channel_state(gid, cid)
        cs["mention_role_id"] = int(role.id)
        save_state()
        await _countdown_apply(interaction, gid, cid, confirm=f"✅ Milestone reminders will mention {role.mention}.")


class CountdownRoleClearButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Clear role", emoji="🚫", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        gid, cid = view.guild_id, view.channel_id
        cs = get_channel_state(gid, cid)
        cs["mention_role_id"] = None
        save_state()
        await _countdown_apply(interaction, gid, cid, confirm="✅ Mention role cleared.")


class CountdownRoleView(discord.ui.View):
    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.add_item(CountdownRoleSelect())
        self.add_item(CountdownRoleClearButton())
        self.add_item(CountdownBackButton())


# Curated, friendly-labelled timezones for the picker (value = canonical IANA
# name). Covers the common cases so people don't have to know the exact spelling;
# the "Other" option still opens the modal for anything not listed.
COMMON_TIMEZONES = [
    ("UTC", "UTC"),
    ("US Eastern — New York", "America/New_York"),
    ("US Central — Chicago", "America/Chicago"),
    ("US Mountain — Denver", "America/Denver"),
    ("US Arizona (no DST)", "America/Phoenix"),
    ("US Pacific — Los Angeles", "America/Los_Angeles"),
    ("US Alaska — Anchorage", "America/Anchorage"),
    ("US Hawaii — Honolulu", "Pacific/Honolulu"),
    ("Canada Eastern — Toronto", "America/Toronto"),
    ("Mexico City", "America/Mexico_City"),
    ("Brazil — São Paulo", "America/Sao_Paulo"),
    ("UK — London", "Europe/London"),
    ("Central Europe — Paris", "Europe/Paris"),
    ("Central Europe — Berlin", "Europe/Berlin"),
    ("Eastern Europe — Athens", "Europe/Athens"),
    ("Moscow", "Europe/Moscow"),
    ("India — Kolkata", "Asia/Kolkata"),
    ("UAE — Dubai", "Asia/Dubai"),
    ("Singapore", "Asia/Singapore"),
    ("China — Shanghai", "Asia/Shanghai"),
    ("Japan — Tokyo", "Asia/Tokyo"),
    ("Australia Eastern — Sydney", "Australia/Sydney"),
    ("New Zealand — Auckland", "Pacific/Auckland"),
]


class CountdownTimezoneSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=lbl[:100], value=iana) for lbl, iana in COMMON_TIMEZONES]
        options.append(discord.SelectOption(
            label="Other — type any timezone…", value="__other__", emoji="⌨️",
            description="Enter a custom IANA zone (e.g. Europe/Lisbon)"))
        super().__init__(placeholder="Pick your timezone…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        gid, cid = view.guild_id, view.channel_id
        choice = self.values[0]
        if choice == "__other__":
            await interaction.response.send_modal(CountdownTimezoneModal(gid, cid, interaction))
            return
        cs = get_channel_state(gid, cid)
        cs["timezone"] = choice
        save_state()
        await _countdown_apply(interaction, gid, cid, confirm=f"✅ Timezone set to `{choice}`.")


class CountdownTimezoneModal(discord.ui.Modal):
    def __init__(self, guild_id: int, channel_id: int, parent: discord.Interaction):
        super().__init__(title="Set channel timezone")
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.parent = parent
        cs = get_channel_state(guild_id, channel_id)
        self.tz_input = discord.ui.TextInput(
            label="Timezone",
            placeholder="e.g. US/Eastern, Europe/London, UTC",
            required=True,
            max_length=64,
            default=cs.get("timezone") or "UTC",
        )
        self.add_item(self.tz_input)

    async def on_submit(self, interaction: discord.Interaction):
        import pytz
        raw = str(self.tz_input.value).strip()
        try:
            pytz.timezone(raw)
        except Exception:
            await interaction.response.send_message(
                f"❌ `{raw}` isn't a valid timezone. Try e.g. `US/Eastern`, `Europe/London`, `UTC`.",
                ephemeral=True,
            )
            return
        cs = get_channel_state(self.guild_id, self.channel_id)
        cs["timezone"] = raw
        save_state()
        await interaction.response.send_message(f"✅ Timezone set to `{raw}`.", ephemeral=True)
        await _countdown_apply_via_parent(self.parent, self.guild_id, self.channel_id, confirm=f"✅ Timezone set to `{raw}`.")


class CountdownTextModal(discord.ui.Modal):
    """Edit a free-text override (title or description). Blank clears it."""
    def __init__(self, guild_id: int, channel_id: int, parent: discord.Interaction, *,
                 field: str, modal_title: str, label: str, max_len: int, style: discord.TextStyle):
        super().__init__(title=modal_title)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.parent = parent
        self.field = field
        self.max_len = max_len
        cs = get_channel_state(guild_id, channel_id)
        self.text_input = discord.ui.TextInput(
            label=label,
            required=False,
            max_length=max_len,
            style=style,
            default=cs.get(field) or None,
            placeholder="Leave blank to clear / use the theme default",
        )
        self.add_item(self.text_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.text_input.value or "").strip()
        cs = get_channel_state(self.guild_id, self.channel_id)
        cs[self.field] = raw[: self.max_len] if raw else None
        save_state()
        confirm = "✅ Cleared." if not raw else "✅ Updated."
        await interaction.response.send_message(confirm, ephemeral=True)
        await _countdown_apply_via_parent(self.parent, self.guild_id, self.channel_id, confirm=confirm)


class CountdownBuildModal(discord.ui.Modal):
    """Pro build-your-own theme: one modal sets the whole custom embed look."""
    def __init__(self, gid: int, cid: int, parent: discord.Interaction):
        super().__init__(title="Build your own theme")
        self.gid, self.cid, self.parent = gid, cid, parent
        ct = get_channel_state(gid, cid).get("custom_theme") or {}
        color_default = f"#{ct['color']:06X}" if isinstance(ct.get("color"), int) else None
        self.title_in = discord.ui.TextInput(
            label="Embed title", required=False, max_length=256,
            default=ct.get("title") or None, placeholder="🎯 Our Countdown")
        self.subtitle_in = discord.ui.TextInput(
            label="Subtitle / intro line", required=False, max_length=200,
            style=discord.TextStyle.paragraph, default=ct.get("subtitle") or None,
            placeholder="What this countdown is about")
        self.footer_in = discord.ui.TextInput(
            label="Footer", required=False, max_length=200,
            default=ct.get("footer") or None, placeholder="Updated every minute")
        self.color_in = discord.ui.TextInput(
            label="Color (hex, e.g. #8C52FF)", required=False, max_length=7,
            default=color_default, placeholder="#8C52FF")
        self.emoji_in = discord.ui.TextInput(
            label="Event emoji", required=False, max_length=12,
            default=ct.get("emoji") or None, placeholder="🎯")
        for item in (self.title_in, self.subtitle_in, self.footer_in, self.color_in, self.emoji_in):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        raw_color = str(self.color_in.value or "").strip().lstrip("#")
        color_int = None
        if raw_color:
            try:
                color_int = int(raw_color, 16)
            except ValueError:
                color_int = -1
            if not (0 <= color_int <= 0xFFFFFF):
                await interaction.response.send_message(
                    "❌ Color must be a 6-digit hex code like `#8C52FF`.", ephemeral=True)
                return
        cs = get_channel_state(self.gid, self.cid)
        cs["custom_theme"] = {
            "title": (str(self.title_in.value).strip() or None),
            "subtitle": (str(self.subtitle_in.value).strip() or None),
            "footer": (str(self.footer_in.value).strip() or None),
            "color": color_int,
            "emoji": (str(self.emoji_in.value).strip() or None),
        }
        cs["theme"] = "custom"
        save_state()
        await interaction.response.send_message("✅ Custom theme saved and applied.", ephemeral=True)
        await _countdown_apply_via_parent(self.parent, self.gid, self.cid, confirm="✅ Custom theme applied.")


class CountdownDigestToggle(discord.ui.Button):
    def __init__(self, enable: bool):
        self.enable = enable
        super().__init__(
            label="Enable" if enable else "Disable",
            emoji="📊" if enable else "🔕",
            style=discord.ButtonStyle.success if enable else discord.ButtonStyle.secondary,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        gid, cid = view.guild_id, view.channel_id
        g = get_guild_state(gid)
        if not is_pro(g):
            await interaction.response.send_message(
                "💎 The **weekly digest** is a Chromie Pro feature ($2.99/mo). Subscribe via Discord Server Subscription.",
                ephemeral=True,
            )
            return
        cs = get_channel_state(gid, cid)
        d = cs.setdefault("digest", {"enabled": False, "channel_id": None, "last_sent_date": None})
        d["enabled"] = self.enable
        d["channel_id"] = int(cid)
        save_state()
        await _countdown_apply(interaction, gid, cid,
                               confirm="✅ Weekly digest enabled." if self.enable else "🛑 Weekly digest disabled.")


class CountdownDigestView(discord.ui.View):
    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.add_item(CountdownDigestToggle(True))
        self.add_item(CountdownDigestToggle(False))
        self.add_item(CountdownBackButton())


class CountdownRemoveConfirm(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Yes, remove this countdown", emoji="🗑️", style=discord.ButtonStyle.danger, row=0)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        gid, cid = view.guild_id, view.channel_id
        g = get_guild_state(gid)
        g.get("channels", {}).pop(str(cid), None)
        save_state()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="🗑️ Countdown removed",
                description=(
                    f"<#{cid}> is no longer a countdown channel. Its events and settings were "
                    "cleared. The old pinned message (if any) was left in place — delete it manually."
                ),
                color=discord.Color.orange(),
            ),
            view=None,
        )


class CountdownRemoveView(discord.ui.View):
    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.add_item(CountdownRemoveConfirm())
        self.add_item(CountdownBackButton())


class CountdownAutoDeleteToggle(discord.ui.Button):
    def __init__(self, enable: bool):
        self.enable = enable
        super().__init__(
            label="Auto-delete after 24h" if enable else "Keep forever",
            emoji="🧹" if enable else "📌",
            style=discord.ButtonStyle.success if enable else discord.ButtonStyle.secondary,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        gid, cid = view.guild_id, view.channel_id
        cs = get_channel_state(gid, cid)
        cs["auto_delete_milestones"] = self.enable
        save_state()
        await _countdown_apply(
            interaction, gid, cid,
            confirm=("✅ Milestone/reminder posts will auto-delete after 24h."
                     if self.enable else "✅ Milestone/reminder posts will stay forever."),
        )


class CountdownAutoDeleteView(discord.ui.View):
    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.add_item(CountdownAutoDeleteToggle(True))
        self.add_item(CountdownAutoDeleteToggle(False))
        self.add_item(CountdownBackButton())


class CountdownSettingSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Theme", value="theme", emoji="🎨", description="Change the countdown look"),
            discord.SelectOption(label="Timezone", value="timezone", emoji="🌍", description="Timezone for this channel"),
            discord.SelectOption(label="Time format", value="timeformat", emoji="⏱️", description="How the countdown displays"),
            discord.SelectOption(label="Mention role", value="role", emoji="🔔", description="Role pinged on milestones"),
            discord.SelectOption(label="Auto-delete posts", value="autodelete", emoji="🧹", description="Keep or remove milestone/reminder posts"),
            discord.SelectOption(label="Title (Pro)", value="title", emoji="📝", description="Custom pinned-embed title"),
            discord.SelectOption(label="Description (Pro)", value="description", emoji="💬", description="Custom intro text"),
            discord.SelectOption(label="Weekly digest (Pro)", value="digest", emoji="📊", description="Weekly summary post"),
            discord.SelectOption(label="Build-your-own theme (Pro)", value="buildyourown", emoji="🎨", description="Custom color, emoji, title & footer"),
            discord.SelectOption(label="Remove countdown channel", value="remove", emoji="🗑️", description="Stop this channel's countdown"),
        ]
        super().__init__(placeholder="Choose a setting to change…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        gid, cid = view.guild_id, view.channel_id
        g = get_guild_state(gid)
        choice = self.values[0]

        if choice == "theme":
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="🎨 Pick a theme",
                    description="Pick one to preview it, then Apply. Supporter themes need an active `/vote` or Chromie Pro.",
                    color=EMBED_COLOR,
                ),
                view=CountdownSubView(gid, cid, CountdownThemeSelect(g)),
            )
        elif choice == "timeformat":
            await interaction.response.edit_message(
                embed=discord.Embed(title="⏱️ Pick a time format", color=EMBED_COLOR),
                view=CountdownSubView(gid, cid, CountdownTimeFormatSelect()),
            )
        elif choice == "role":
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="🔔 Mention role",
                    description="Pick a role to ping on milestone posts, or clear it.",
                    color=EMBED_COLOR,
                ),
                view=CountdownRoleView(gid, cid),
            )
        elif choice == "autodelete":
            cur = get_channel_state(gid, cid).get("auto_delete_milestones", True)
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="🧹 Auto-delete posts",
                    description=(
                        f"Currently **{'auto-delete after 24h' if cur else 'keep forever'}**.\n"
                        "Controls whether milestone + reminder announcement messages are "
                        "cleaned up automatically."
                    ),
                    color=EMBED_COLOR,
                ),
                view=CountdownAutoDeleteView(gid, cid),
            )
        elif choice == "timezone":
            cur = get_channel_state(gid, cid).get("timezone") or "UTC"
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="🌍 Timezone",
                    description=f"Current: `{cur}`.\nPick yours from the list — or choose **Other** to type any IANA zone.",
                    color=EMBED_COLOR,
                ),
                view=CountdownSubView(gid, cid, CountdownTimezoneSelect()),
            )
        elif choice in ("title", "description"):
            if not is_pro(g):
                await interaction.response.send_message(
                    f"💎 Custom countdown {'titles' if choice == 'title' else 'descriptions'} are a "
                    "**Chromie Pro** feature ($2.99/mo). Subscribe via Discord Server Subscription.",
                    ephemeral=True,
                )
                return
            if choice == "title":
                await interaction.response.send_modal(CountdownTextModal(
                    gid, cid, interaction, field="countdown_title_override",
                    modal_title="Custom title", label="Title (blank = theme default)",
                    max_len=256, style=discord.TextStyle.short))
            else:
                await interaction.response.send_modal(CountdownTextModal(
                    gid, cid, interaction, field="countdown_description_override",
                    modal_title="Custom description", label="Description (blank = none)",
                    max_len=1500, style=discord.TextStyle.paragraph))
        elif choice == "digest":
            if not is_pro(g):
                await interaction.response.send_message(
                    "💎 The **weekly digest** is a Chromie Pro feature ($2.99/mo). Subscribe via Discord Server Subscription.",
                    ephemeral=True,
                )
                return
            digest = get_channel_state(gid, cid).get("digest") or {}
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="📊 Weekly digest",
                    description=f"Currently **{'on' if digest.get('enabled') else 'off'}**. "
                                "Posts a weekly summary of this channel's upcoming events.",
                    color=EMBED_COLOR,
                ),
                view=CountdownDigestView(gid, cid),
            )
        elif choice == "buildyourown":
            if not is_pro(g):
                await interaction.response.send_message(
                    "💎 **Build-your-own themes** are a Chromie Pro feature ($2.99/mo) — design your own "
                    "embed color, emoji, title and footer. Subscribe via Discord Server Subscription.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_modal(CountdownBuildModal(gid, cid, interaction))
        elif choice == "remove":
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="🗑️ Remove this countdown channel?",
                    description=(
                        f"This clears <#{cid}>'s events and settings and stops its countdown. "
                        "This can't be undone."
                    ),
                    color=discord.Color.red(),
                ),
                view=CountdownRemoveView(gid, cid),
            )


class CountdownHubView(discord.ui.View):
    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.add_item(CountdownSettingSelect())


@bot.tree.command(name="countdown", description="Customize this channel's countdown (theme, timezone, format, and more).")
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def countdown_hub(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None
    g = get_guild_state(guild.id)
    cid, cs = resolve_event_channel(g, interaction.channel_id)
    if cs is None:
        await interaction.response.send_message(no_channel_guidance(g, "/countdown"), ephemeral=True)
        return
    await interaction.response.send_message(
        embed=build_countdown_settings_embed(cs, g, cid),
        view=CountdownHubView(guild.id, cid),
        ephemeral=True,
    )


# ==========================
# /event HUB — per-event editor (pick an event → actions)
# ==========================
# Replaces editevent/removeevent/eventinfo/setmilestones/resetmilestones/silence/
# seteventowner/cleareventowner/banner/setrepeat/clearrepeat/dupeevent/
# editevent_reminder/clear_reminder_time, plus the per-event owner-DM opt-in.
# Views hold a direct reference to the event dict so actions stay correct even
# when re-sorting shifts list indices.

def _event_dt(ev: dict, tz) -> datetime:
    return datetime.fromtimestamp(ev.get("timestamp", 0), tz=tz)


def build_event_hub_embed(cs: dict, guild_state: dict) -> discord.Embed:
    text = format_events_list(cs)
    e = discord.Embed(title="🗓️ Events", description=text[:4000], color=EMBED_COLOR)
    e.set_footer(text="Pick an event to manage it, or add a new one.")
    return e


def build_event_detail_embed(cs: dict, guild_state: dict, ev: dict) -> discord.Embed:
    tz = get_guild_timezone(cs)
    dt = _event_dt(ev, tz)
    now = datetime.now(tz)
    desc, _, passed = compute_time_left(now, dt)
    miles = ", ".join(str(x) for x in ev.get("milestones", DEFAULT_MILESTONES)) or "—"
    repeat_every = ev.get("repeat_every_days")
    repeat_note = f"every {repeat_every} day(s)" if isinstance(repeat_every, int) and repeat_every > 0 else "off"
    owner_id = ev.get("owner_user_id")
    owner_note = f"<@{owner_id}>" if owner_id else "none"
    reminder = ev.get("reminder_time") or "event time"
    dm = "✅ on" if ev.get("dm_opt_in") else "🔕 off"
    banner = "✅ set" if ev.get("banner_url") else "—"

    e = discord.Embed(title=f"🗓️ {str(ev.get('name', 'Event'))[:240]}", color=EMBED_COLOR)
    e.add_field(name="When", value=dt.strftime("%B %d, %Y at %I:%M %p %Z"), inline=False)
    e.add_field(name="Countdown", value=(f"{desc} remaining" if not passed else "started / passed"), inline=False)
    e.add_field(name="🔔 Milestones", value=miles, inline=True)
    e.add_field(name="🕐 Reminder time", value=reminder, inline=True)
    e.add_field(name="🔁 Repeat", value=repeat_note, inline=True)
    e.add_field(name="👤 Owner", value=owner_note, inline=True)
    e.add_field(name="📨 Owner DMs", value=dm, inline=True)
    e.add_field(name="🖼️ Banner", value=banner, inline=True)
    e.add_field(name="🔕 Silenced", value=("yes" if ev.get("silenced") and not passed else "no"), inline=True)
    return e


async def _event_apply(interaction: discord.Interaction, gid: int, cid: int, ev: dict, *, confirm: str):
    """Component path: refresh the pin + re-render the event detail in place."""
    await _countdown_refresh_pin(gid, cid)
    g = get_guild_state(gid)
    cs = get_channel_state(gid, cid)
    embed = build_event_detail_embed(cs, g, ev)
    embed.description = confirm
    await interaction.response.edit_message(embed=embed, view=EventDetailView(gid, cid, ev))


async def _event_apply_via_parent(parent: discord.Interaction, gid: int, cid: int, ev: dict, *, confirm: str):
    """Modal path: refresh the pin + re-render the event detail on the parent message."""
    await _countdown_refresh_pin(gid, cid)
    g = get_guild_state(gid)
    cs = get_channel_state(gid, cid)
    embed = build_event_detail_embed(cs, g, ev)
    embed.description = confirm
    try:
        await parent.edit_original_response(embed=embed, view=EventDetailView(gid, cid, ev))
    except Exception:
        pass


class EventBackToListButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Back to events", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        g = get_guild_state(view.guild_id)
        cs = get_channel_state(view.guild_id, view.channel_id)
        await interaction.response.edit_message(
            embed=build_event_hub_embed(cs, g), view=EventHubView(view.guild_id, view.channel_id)
        )


class EventDetailBackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Back", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        g = get_guild_state(view.guild_id)
        cs = get_channel_state(view.guild_id, view.channel_id)
        await interaction.response.edit_message(
            embed=build_event_detail_embed(cs, g, view.ev),
            view=EventDetailView(view.guild_id, view.channel_id, view.ev),
        )


class EventListSelect(discord.ui.Select):
    def __init__(self, cs: dict):
        tz = get_guild_timezone(cs)
        sort_events(cs)
        opts = []
        for i, ev in enumerate(cs.get("events", [])[:25]):
            dt = _event_dt(ev, tz)
            opts.append(discord.SelectOption(
                label=f"{i + 1}. {str(ev.get('name', 'Event'))[:80]}"[:100],
                value=str(i),
                description=dt.strftime("%m/%d/%Y %H:%M")[:100],
            ))
        if not opts:
            opts = [discord.SelectOption(label="(no events yet — use Add event)", value="none")]
        super().__init__(placeholder="Pick an event to manage…", min_values=1, max_values=1, options=opts)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        gid, cid = view.guild_id, view.channel_id
        if self.values[0] == "none":
            await interaction.response.defer()
            return
        g = get_guild_state(gid)
        cs = get_channel_state(gid, cid)
        sort_events(cs)
        idx = int(self.values[0])
        events = cs.get("events", [])
        if idx >= len(events):
            await interaction.response.edit_message(
                embed=build_event_hub_embed(cs, g), view=EventHubView(gid, cid)
            )
            return
        ev = events[idx]
        await interaction.response.edit_message(
            embed=build_event_detail_embed(cs, g, ev), view=EventDetailView(gid, cid, ev)
        )


class EventAddModal(discord.ui.Modal):
    def __init__(self, gid: int, cid: int, parent: discord.Interaction):
        super().__init__(title="Add event")
        self.gid, self.cid, self.parent = gid, cid, parent
        self.date = discord.ui.TextInput(label="Date (MM/DD/YYYY)", placeholder="04/12/2026", max_length=10)
        self.time = discord.ui.TextInput(label="Time (24-hour HH:MM)", placeholder="09:00", max_length=5)
        self.name = discord.ui.TextInput(label="Event name", max_length=200)
        self.add_item(self.date)
        self.add_item(self.time)
        self.add_item(self.name)

    async def on_submit(self, interaction: discord.Interaction):
        g = get_guild_state(self.gid)
        cs = get_channel_state(self.gid, self.cid)
        guild = bot.get_guild(self.gid)
        member = guild.get_member(interaction.user.id) if guild else None
        msg, _nudge = await add_event_core(
            guild, g, cs, self.cid,
            actor=interaction.user, member=member,
            date=str(self.date.value).strip(), time=str(self.time.value).strip(),
            name=str(self.name.value).strip(),
        )
        await interaction.response.send_message(msg, ephemeral=True)
        try:
            await self.parent.edit_original_response(
                embed=build_event_hub_embed(cs, g), view=EventHubView(self.gid, self.cid)
            )
        except Exception:
            pass


class EventAddButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Add event", emoji="➕", style=discord.ButtonStyle.success, row=1)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        await interaction.response.send_modal(EventAddModal(view.guild_id, view.channel_id, interaction))


class EventHubView(discord.ui.View):
    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.add_item(EventListSelect(get_channel_state(guild_id, channel_id)))
        self.add_item(EventAddButton())


# ---- detail sub-flows ----

class EventEditModal(discord.ui.Modal):
    def __init__(self, gid: int, cid: int, ev: dict, parent: discord.Interaction):
        super().__init__(title="Edit event")
        self.gid, self.cid, self.ev, self.parent = gid, cid, ev, parent
        tz = get_guild_timezone(get_channel_state(gid, cid))
        cur = _event_dt(ev, tz)
        self.name = discord.ui.TextInput(label="Name", required=False, max_length=200, default=ev.get("name") or None)
        self.date = discord.ui.TextInput(label="Date (MM/DD/YYYY)", required=False, max_length=10, default=cur.strftime("%m/%d/%Y"))
        self.time = discord.ui.TextInput(label="Time (24-hour HH:MM)", required=False, max_length=5, default=cur.strftime("%H:%M"))
        self.add_item(self.name)
        self.add_item(self.date)
        self.add_item(self.time)

    async def on_submit(self, interaction: discord.Interaction):
        cs = get_channel_state(self.gid, self.cid)
        tz = get_guild_timezone(cs)
        ev = self.ev
        name = str(self.name.value or "").strip()
        if name:
            ev["name"] = name
        date = str(self.date.value or "").strip()
        time = str(self.time.value or "").strip()
        if date or time:
            cur = _event_dt(ev, tz)
            new_date = date or cur.strftime("%m/%d/%Y")
            new_time = time or cur.strftime("%H:%M")
            try:
                dt = datetime.strptime(f"{new_date} {new_time}", "%m/%d/%Y %H:%M").replace(tzinfo=tz)
            except ValueError:
                await interaction.response.send_message("Invalid date/time. Use MM/DD/YYYY + 24-hour HH:MM.", ephemeral=True)
                return
            if dt <= datetime.now(tz):
                await interaction.response.send_message("That date/time is in the past. Choose a future time.", ephemeral=True)
                return
            ev["timestamp"] = int(dt.timestamp())
            ev["announced_milestones"] = []
            ev["announced_repeat_dates"] = []
        sort_events(cs)
        save_state()
        await interaction.response.send_message("✅ Event updated.", ephemeral=True)
        await _event_apply_via_parent(self.parent, self.gid, self.cid, ev, confirm="✅ Event updated.")


class EventMilestonesModal(discord.ui.Modal):
    def __init__(self, gid: int, cid: int, ev: dict, parent: discord.Interaction):
        super().__init__(title="Set milestone days")
        self.gid, self.cid, self.ev, self.parent = gid, cid, ev, parent
        self.days = discord.ui.TextInput(
            label="Milestone days",
            placeholder="100, 50, 30, 14, 7, 2, 1, 0",
            default=", ".join(str(x) for x in ev.get("milestones", DEFAULT_MILESTONES)),
            max_length=200,
        )
        self.add_item(self.days)

    async def on_submit(self, interaction: discord.Interaction):
        parsed = parse_milestones(str(self.days.value or ""))
        if parsed is None:
            await interaction.response.send_message(
                "Couldn't parse those. Use whole numbers 0–5000, e.g. `100, 50, 30, 7, 1, 0`.", ephemeral=True
            )
            return
        self.ev["milestones"] = parsed
        self.ev["announced_milestones"] = []
        save_state()
        await interaction.response.send_message("✅ Milestones updated.", ephemeral=True)
        await _event_apply_via_parent(self.parent, self.gid, self.cid, self.ev,
                                      confirm=f"✅ Milestones set to: {', '.join(str(x) for x in parsed)}")


class EventMilestonesSetButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Set custom", emoji="🔔", style=discord.ButtonStyle.primary, row=0)

    async def callback(self, interaction: discord.Interaction):
        v = self.view
        await interaction.response.send_modal(EventMilestonesModal(v.guild_id, v.channel_id, v.ev, interaction))


class EventMilestonesResetButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Reset to default", emoji="↩️", style=discord.ButtonStyle.secondary, row=0)

    async def callback(self, interaction: discord.Interaction):
        v = self.view
        cs = get_channel_state(v.guild_id, v.channel_id)
        defaults = cs.get("default_milestones")
        if not isinstance(defaults, list) or not defaults:
            defaults = DEFAULT_MILESTONES
        v.ev["milestones"] = list(defaults)
        v.ev["announced_milestones"] = []
        save_state()
        await _event_apply(interaction, v.guild_id, v.channel_id, v.ev,
                           confirm=f"✅ Milestones reset to defaults: {', '.join(str(x) for x in defaults)}")


class EventMilestonesView(discord.ui.View):
    def __init__(self, gid, cid, ev):
        super().__init__(timeout=300)
        self.guild_id, self.channel_id, self.ev = gid, cid, ev
        self.add_item(EventMilestonesSetButton())
        self.add_item(EventMilestonesResetButton())
        self.add_item(EventDetailBackButton())


class EventReminderModal(discord.ui.Modal):
    def __init__(self, gid, cid, ev, parent):
        super().__init__(title="Set reminder time")
        self.gid, self.cid, self.ev, self.parent = gid, cid, ev, parent
        self.time = discord.ui.TextInput(label="Reminder time (24-hour HH:MM)", placeholder="09:00",
                                         default=ev.get("reminder_time") or None, max_length=5)
        self.add_item(self.time)

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.time.value or "").strip()
        if not validate_time_format(raw):
            await interaction.response.send_message("Invalid time. Use 24-hour `HH:MM`, e.g. `09:00`.", ephemeral=True)
            return
        self.ev["reminder_time"] = raw
        save_state()
        await interaction.response.send_message(f"✅ Reminder time set to {raw}.", ephemeral=True)
        await _event_apply_via_parent(self.parent, self.gid, self.cid, self.ev, confirm=f"✅ Reminder time set to **{raw}**.")


class EventReminderSetButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Set time", emoji="🕐", style=discord.ButtonStyle.primary, row=0)

    async def callback(self, interaction: discord.Interaction):
        v = self.view
        await interaction.response.send_modal(EventReminderModal(v.guild_id, v.channel_id, v.ev, interaction))


class EventReminderClearButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Clear (use event time)", emoji="🚫", style=discord.ButtonStyle.secondary, row=0)

    async def callback(self, interaction: discord.Interaction):
        v = self.view
        v.ev["reminder_time"] = None
        save_state()
        await _event_apply(interaction, v.guild_id, v.channel_id, v.ev, confirm="✅ Reminder time cleared (using event time).")


class EventReminderView(discord.ui.View):
    def __init__(self, gid, cid, ev):
        super().__init__(timeout=300)
        self.guild_id, self.channel_id, self.ev = gid, cid, ev
        self.add_item(EventReminderSetButton())
        self.add_item(EventReminderClearButton())
        self.add_item(EventDetailBackButton())


class EventOwnerSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Pick an event owner…", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        v = self.view
        user = self.values[0]
        v.ev["owner_user_id"] = int(user.id)
        v.ev["owner_name"] = getattr(user, "display_name", None) or user.name
        save_state()
        note = "" if v.ev.get("dm_opt_in") else " (turn on Owner DMs to actually DM them)"
        await _event_apply(interaction, v.guild_id, v.channel_id, v.ev,
                           confirm=f"✅ Owner set to {user.mention}.{note}")


class EventOwnerClearButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Clear owner", emoji="🚫", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction: discord.Interaction):
        v = self.view
        v.ev["owner_user_id"] = None
        v.ev["owner_name"] = None
        save_state()
        await _event_apply(interaction, v.guild_id, v.channel_id, v.ev, confirm="✅ Owner cleared.")


class EventOwnerView(discord.ui.View):
    def __init__(self, gid, cid, ev):
        super().__init__(timeout=300)
        self.guild_id, self.channel_id, self.ev = gid, cid, ev
        self.add_item(EventOwnerSelect())
        self.add_item(EventOwnerClearButton())
        self.add_item(EventDetailBackButton())


class EventBannerModal(discord.ui.Modal):
    def __init__(self, gid, cid, ev, parent):
        super().__init__(title="Set banner image")
        self.gid, self.cid, self.ev, self.parent = gid, cid, ev, parent
        self.url = discord.ui.TextInput(label="Image URL (https://…)", default=ev.get("banner_url") or None, max_length=500)
        self.add_item(self.url)

    async def on_submit(self, interaction: discord.Interaction):
        u = _clean_url(str(self.url.value or ""))
        if not (u.startswith("https://") or u.startswith("http://")):
            await interaction.response.send_message("Banner URL must start with http:// or https://", ephemeral=True)
            return
        self.ev["banner_url"] = u
        save_state()
        await interaction.response.send_message("✅ Banner set.", ephemeral=True)
        await _event_apply_via_parent(self.parent, self.gid, self.cid, self.ev, confirm="✅ Banner set.")


class EventBannerSetButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Set banner", emoji="🖼️", style=discord.ButtonStyle.primary, row=0)

    async def callback(self, interaction: discord.Interaction):
        v = self.view
        await interaction.response.send_modal(EventBannerModal(v.guild_id, v.channel_id, v.ev, interaction))


class EventBannerClearButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Clear banner", emoji="🚫", style=discord.ButtonStyle.secondary, row=0)

    async def callback(self, interaction: discord.Interaction):
        v = self.view
        v.ev["banner_url"] = None
        save_state()
        await _event_apply(interaction, v.guild_id, v.channel_id, v.ev, confirm="✅ Banner cleared.")


class EventBannerView(discord.ui.View):
    def __init__(self, gid, cid, ev):
        super().__init__(timeout=300)
        self.guild_id, self.channel_id, self.ev = gid, cid, ev
        self.add_item(EventBannerSetButton())
        self.add_item(EventBannerClearButton())
        self.add_item(EventDetailBackButton())


class EventRepeatModal(discord.ui.Modal):
    def __init__(self, gid, cid, ev, parent):
        super().__init__(title="Set repeat interval")
        self.gid, self.cid, self.ev, self.parent = gid, cid, ev, parent
        cur = ev.get("repeat_every_days")
        self.days = discord.ui.TextInput(label="Repeat every N days (1–365)", placeholder="7",
                                         default=str(cur) if cur else None, max_length=3)
        self.add_item(self.days)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            n = int(str(self.days.value or "").strip())
        except ValueError:
            await interaction.response.send_message("Enter a whole number of days (1–365).", ephemeral=True)
            return
        if n < 1 or n > 365:
            await interaction.response.send_message("Repeat interval must be between 1 and 365 days.", ephemeral=True)
            return
        cs = get_channel_state(self.gid, self.cid)
        self.ev["repeat_every_days"] = n
        self.ev["repeat_anchor_date"] = _today_local_date(get_guild_timezone(cs)).isoformat()
        self.ev["announced_repeat_dates"] = []
        save_state()
        await interaction.response.send_message(f"✅ Repeating every {n} day(s).", ephemeral=True)
        await _event_apply_via_parent(self.parent, self.gid, self.cid, self.ev, confirm=f"✅ Repeating every {n} day(s).")


class EventRepeatSetButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Set repeat", emoji="🔁", style=discord.ButtonStyle.primary, row=0)

    async def callback(self, interaction: discord.Interaction):
        v = self.view
        await interaction.response.send_modal(EventRepeatModal(v.guild_id, v.channel_id, v.ev, interaction))


class EventRepeatClearButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Turn off repeat", emoji="🚫", style=discord.ButtonStyle.secondary, row=0)

    async def callback(self, interaction: discord.Interaction):
        v = self.view
        v.ev["repeat_every_days"] = None
        v.ev["repeat_anchor_date"] = None
        v.ev["announced_repeat_dates"] = []
        save_state()
        await _event_apply(interaction, v.guild_id, v.channel_id, v.ev, confirm="✅ Repeating reminders turned off.")


class EventRepeatView(discord.ui.View):
    def __init__(self, gid, cid, ev):
        super().__init__(timeout=300)
        self.guild_id, self.channel_id, self.ev = gid, cid, ev
        self.add_item(EventRepeatSetButton())
        self.add_item(EventRepeatClearButton())
        self.add_item(EventDetailBackButton())


class EventDupeModal(discord.ui.Modal):
    def __init__(self, gid, cid, ev, parent):
        super().__init__(title="Duplicate event")
        self.gid, self.cid, self.ev, self.parent = gid, cid, ev, parent
        tz = get_guild_timezone(get_channel_state(gid, cid))
        cur = _event_dt(ev, tz)
        self.date = discord.ui.TextInput(label="New date (MM/DD/YYYY)", placeholder="04/12/2026", max_length=10)
        self.time = discord.ui.TextInput(label="New time (HH:MM, blank = same)", required=False, max_length=5,
                                         default=cur.strftime("%H:%M"))
        self.name = discord.ui.TextInput(label="New name (blank = same)", required=False, max_length=200,
                                         default=ev.get("name") or None)
        self.add_item(self.date)
        self.add_item(self.time)
        self.add_item(self.name)

    async def on_submit(self, interaction: discord.Interaction):
        cs = get_channel_state(self.gid, self.cid)
        g = get_guild_state(self.gid)
        tz = get_guild_timezone(cs)
        src = self.ev
        use_time = str(self.time.value or "").strip() or _event_dt(src, tz).strftime("%H:%M")
        use_name = str(self.name.value or "").strip() or src.get("name", "Event")
        try:
            dt = datetime.strptime(f"{str(self.date.value).strip()} {use_time}", "%m/%d/%Y %H:%M").replace(tzinfo=tz)
        except ValueError:
            await interaction.response.send_message("Invalid date/time. Use MM/DD/YYYY + 24-hour HH:MM.", ephemeral=True)
            return
        if dt <= datetime.now(tz):
            await interaction.response.send_message("That date/time is in the past. Choose a future time.", ephemeral=True)
            return
        guild = bot.get_guild(self.gid)
        maker = guild.get_member(interaction.user.id) if guild else None
        maker_name = getattr(maker, "display_name", None) or interaction.user.name
        new_ev = {
            "name": use_name,
            "timestamp": int(dt.timestamp()),
            "milestones": (src.get("milestones") or DEFAULT_MILESTONES).copy(),
            "announced_milestones": [],
            "repeat_every_days": src.get("repeat_every_days"),
            "repeat_anchor_date": None,
            "announced_repeat_dates": [],
            "silenced": src.get("silenced", False),
            "dm_opt_in": False,
            "start_announced": False,
            "banner_url": src.get("banner_url"),
            "created_by_user_id": int(interaction.user.id),
            "created_by_name": maker_name,
            "owner_user_id": int(interaction.user.id),
            "owner_name": maker_name,
        }
        cs["events"].append(new_ev)
        sort_events(cs)
        save_state()
        await _countdown_refresh_pin(self.gid, self.cid)
        await interaction.response.send_message(f"🧬 Duplicated → **{use_name}** on {dt.strftime('%B %d, %Y at %I:%M %p %Z')}.", ephemeral=True)
        try:
            await self.parent.edit_original_response(embed=build_event_hub_embed(cs, g), view=EventHubView(self.gid, self.cid))
        except Exception:
            pass


class EventDeleteConfirmButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Yes, delete this event", emoji="🗑️", style=discord.ButtonStyle.danger, row=0)

    async def callback(self, interaction: discord.Interaction):
        v = self.view
        cs = get_channel_state(v.guild_id, v.channel_id)
        g = get_guild_state(v.guild_id)
        try:
            cs["events"].remove(v.ev)
        except ValueError:
            pass
        save_state()
        await _countdown_refresh_pin(v.guild_id, v.channel_id)
        await interaction.response.edit_message(
            embed=build_event_hub_embed(cs, g), view=EventHubView(v.guild_id, v.channel_id)
        )


class EventDeleteView(discord.ui.View):
    def __init__(self, gid, cid, ev):
        super().__init__(timeout=120)
        self.guild_id, self.channel_id, self.ev = gid, cid, ev
        self.add_item(EventDeleteConfirmButton())
        self.add_item(EventDetailBackButton())


class EventActionSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Edit name / date / time", value="edit", emoji="✏️"),
            discord.SelectOption(label="Milestones", value="milestones", emoji="🔔"),
            discord.SelectOption(label="Reminder time", value="reminder", emoji="🕐"),
            discord.SelectOption(label="Owner", value="owner", emoji="👤"),
            discord.SelectOption(label="Owner DMs (toggle)", value="dmtoggle", emoji="📨"),
            discord.SelectOption(label="Silence (toggle)", value="silence", emoji="🔕"),
            discord.SelectOption(label="Banner (Supporter)", value="banner", emoji="🖼️"),
            discord.SelectOption(label="Repeat (Pro)", value="repeat", emoji="🔁"),
            discord.SelectOption(label="Duplicate (Pro)", value="dupe", emoji="🧬"),
            discord.SelectOption(label="Delete event", value="delete", emoji="🗑️"),
        ]
        super().__init__(placeholder="Choose an action…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        v = self.view
        gid, cid, ev = v.guild_id, v.channel_id, v.ev
        g = get_guild_state(gid)
        choice = self.values[0]

        if choice == "edit":
            await interaction.response.send_modal(EventEditModal(gid, cid, ev, interaction))
        elif choice == "milestones":
            await interaction.response.edit_message(
                embed=discord.Embed(title="🔔 Milestones",
                                    description="Set custom milestone days, or reset to the channel default.",
                                    color=EMBED_COLOR),
                view=EventMilestonesView(gid, cid, ev))
        elif choice == "reminder":
            await interaction.response.edit_message(
                embed=discord.Embed(title="🕐 Reminder time",
                                    description="The final day-of reminder fires at this time instead of the event time.",
                                    color=EMBED_COLOR),
                view=EventReminderView(gid, cid, ev))
        elif choice == "owner":
            await interaction.response.edit_message(
                embed=discord.Embed(title="👤 Event owner",
                                    description="Pick who owns this event. They receive DMs only if Owner DMs is on.",
                                    color=EMBED_COLOR),
                view=EventOwnerView(gid, cid, ev))
        elif choice == "dmtoggle":
            ev["dm_opt_in"] = not bool(ev.get("dm_opt_in"))
            save_state()
            if ev["dm_opt_in"] and not ev.get("owner_user_id"):
                note = "✅ Owner DMs **on** — set an owner so there's someone to DM."
            else:
                note = "✅ Owner DMs **on**." if ev["dm_opt_in"] else "🔕 Owner DMs **off**."
            await _event_apply(interaction, gid, cid, ev, confirm=note)
        elif choice == "silence":
            ev["silenced"] = not bool(ev.get("silenced"))
            save_state()
            await _event_apply(interaction, gid, cid, ev,
                               confirm=("🔕 Reminders silenced." if ev["silenced"] else "🔔 Reminders re-enabled."))
        elif choice == "banner":
            if not (is_pro(g) or await topgg_has_voted(interaction.user.id, force=True)):
                await send_vote_required(interaction, feature_label="event banners")
                return
            await interaction.response.edit_message(
                embed=discord.Embed(title="🖼️ Banner",
                                    description="Set or clear this event's banner image.", color=EMBED_COLOR),
                view=EventBannerView(gid, cid, ev))
        elif choice == "repeat":
            if not is_pro(g):
                await interaction.response.send_message(
                    "💎 Recurring reminders are a **Chromie Pro** feature ($2.99/mo). Subscribe via Discord Server Subscription.",
                    ephemeral=True)
                return
            await interaction.response.edit_message(
                embed=discord.Embed(title="🔁 Repeat", description="Set or turn off recurring reminders.", color=EMBED_COLOR),
                view=EventRepeatView(gid, cid, ev))
        elif choice == "dupe":
            if not is_pro(g):
                await interaction.response.send_message(
                    "💎 Duplicating events is a **Chromie Pro** feature ($2.99/mo). Subscribe via Discord Server Subscription.",
                    ephemeral=True)
                return
            await interaction.response.send_modal(EventDupeModal(gid, cid, ev, interaction))
        elif choice == "delete":
            await interaction.response.edit_message(
                embed=discord.Embed(title="🗑️ Delete this event?",
                                    description=f"**{str(ev.get('name', 'Event'))[:240]}** will be permanently removed.",
                                    color=discord.Color.red()),
                view=EventDeleteView(gid, cid, ev))


class EventDetailView(discord.ui.View):
    def __init__(self, guild_id: int, channel_id: int, ev: dict):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.ev = ev
        self.add_item(EventActionSelect())
        self.add_item(EventBackToListButton())


@bot.tree.command(name="event", description="Add and manage this channel's events (edit, milestones, owner, repeat, …).")
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def event_hub(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None
    g = get_guild_state(guild.id)
    cid, cs = resolve_event_channel(g, interaction.channel_id)
    if cs is None:
        await interaction.response.send_message(no_channel_guidance(g, "/event"), ephemeral=True)
        return
    await interaction.response.send_message(
        embed=build_event_hub_embed(cs, g), view=EventHubView(guild.id, cid), ephemeral=True
    )


async def add_event_core(guild, guild_state, cs, cid, *, actor, member, date, time, name):
    """Validate the tier limit + date, append the event to the channel bucket, and
    rebuild that channel's pin. Returns (message:str, nudge:bool). Shared by the
    top-level /addevent command and the /event hub's Add button so both enforce
    the same per-channel Free/Supporter/Pro limits."""
    tz = get_guild_timezone(cs)

    # EVENT LIMIT ENFORCEMENT (per countdown channel) — only count FUTURE events.
    now = datetime.now(tz)
    future_events = [
        ev for ev in cs.get("events", [])
        if datetime.fromtimestamp(ev.get("timestamp", 0), tz=tz) > now
    ]
    current_event_count = len(future_events)

    is_pro_guild = is_pro(guild_state)
    has_voted = await topgg_has_voted(actor.id, force=True)

    if has_voted:
        nowu = datetime.now(timezone.utc)
        sup = guild_state.setdefault("supporter", {})
        sup["last_vote_at"] = nowu.isoformat()
        sup["vote_until"] = (nowu + timedelta(hours=12)).isoformat()
        save_state()

    if is_pro_guild:
        event_limit, tier_name = None, "Chromie Pro"
    elif has_voted:
        event_limit, tier_name = 5, "Supporter"
    else:
        event_limit, tier_name = 3, "Free"

    if event_limit is not None and current_event_count >= event_limit:
        if tier_name == "Free":
            return (
                f"❌ **Event limit reached!**\n\n"
                f"You have **{current_event_count}/{event_limit} events** (Free tier limit).\n\n"
                f"**Upgrade options:**\n"
                f"• 🗳️ **Vote on Top.gg** → Get 5 events (resets every 12 hours)\n"
                f"  Use `/vote` to get the link!\n"
                f"• 💎 **Chromie Pro** → Unlimited events + premium features\n\n"
                f"Or delete an event from `/event` first."
            ), False
        return (
            f"❌ **Event limit reached!**\n\n"
            f"You have **{current_event_count}/{event_limit} events** (Supporter tier limit).\n\n"
            f"**Upgrade to Chromie Pro for:**\n"
            f"• ♾️ Unlimited events\n"
            f"• 💎 Premium features\n"
            f"• 🎨 Advanced customization\n\n"
            f"Or delete an event from `/event` first."
        ), False

    try:
        dt = datetime.strptime(f"{date} {time}", "%m/%d/%Y %H:%M").replace(tzinfo=tz)
    except ValueError:
        return ("I couldn't understand that date/time.\nUse: `date: 04/12/2026` `time: 09:00` (MM/DD/YYYY + 24-hour HH:MM).", False)

    if dt <= datetime.now(tz):
        return ("That date/time is in the past. Please choose a future time.", False)

    creator_display = getattr(member, "display_name", None) or actor.name

    event = {
        "name": name,
        "timestamp": int(dt.timestamp()),
        "owner_id": actor.id,
        "owner_tag": str(actor),
        "milestones": cs.get("default_milestones", DEFAULT_MILESTONES.copy()).copy(),
        "announced_milestones": [],
        "milestone_messages": [],
        "milestones_cleaned": False,
        "repeat_every_days": None,
        "repeat_anchor_date": None,
        "announced_repeat_dates": [],
        "silenced": False,
        "dm_opt_in": False,  # owner DMs opt-in, default off (toggle in /event)
        "created_by_user_id": int(actor.id),
        "created_by_name": creator_display,
        "owner_user_id": int(actor.id),
        "owner_name": creator_display,
        "banner_url": None,
        "start_announced": False,
    }

    cs["events"].append(event)
    sort_events(cs)
    save_state()

    channel = await get_text_channel(cid)
    if channel is not None:
        await rebuild_pinned_message_for_channel(channel, cs, guild_state)

    msg = (
        f"✅ Added event **{name}** on {dt.strftime('%B %d, %Y at %I:%M %p %Z')} in server **{guild.name}**.\n"
        f"• {tier_name}: {len(cs['events'])}/{event_limit if event_limit else '∞'} events"
    )
    nudge = tier_name == "Free" and len(cs["events"]) >= 2
    return msg, nudge


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
        target_channel_id = interaction.channel_id

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
        target_channel_id = None  # DM has no "current channel"; resolve below

    # Resolve which countdown channel this event belongs to.
    cid, cs = resolve_event_channel(guild_state, target_channel_id)
    if cs is None:
        if count_countdown_channels(guild_state) == 0:
            msg = "I don't know which channel to use yet.\nRun `/seteventchannel` in the channel where you want the countdown pinned."
            if is_dm:
                msg += "\n(Do this in the linked server.)"
        else:
            msg = (
                "This server has multiple countdown channels.\n"
                "Run `/addevent` **inside** the specific countdown channel you want to add to."
            )
        await interaction.edit_original_response(content=msg)
        return

    msg, nudge = await add_event_core(
        guild, guild_state, cs, cid,
        actor=interaction.user, member=member, date=date, time=time, name=name,
    )
    await interaction.edit_original_response(content=msg)
    if nudge:
        await maybe_vote_nudge(interaction, "Event scheduled! Vote on Top.gg to unlock 5 events.")



@bot.tree.command(name="listevents", description="List this channel's events.")
@app_commands.guild_only()
async def listevents(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None
    guild_state = get_guild_state(guild.id)
    _cid, cs = resolve_event_channel(guild_state, interaction.channel_id)
    if cs is None:
        await interaction.response.send_message(
            no_channel_guidance(guild_state, "/listevents"), ephemeral=True
        )
        return

    text = format_events_list(cs)
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
    _cid, cs = resolve_event_channel(g, interaction.channel_id)
    if cs is None:
        await interaction.response.send_message(
            no_channel_guidance(g, "/nextevent"), ephemeral=True
        )
        return
    sort_events(cs)

    tz = get_guild_timezone(cs)
    now = datetime.now(tz)
    next_ev = None
    for ev in cs.get("events", []):
        dt = datetime.fromtimestamp(ev["timestamp"], tz=tz)
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
    cid, cs = resolve_event_channel(g, interaction.channel_id)
    if cs is None:
        await interaction.edit_original_response(content=no_channel_guidance(g, "/remindall"))
        return
    sort_events(cs)

    channel = await get_text_channel(cid)
    if channel is None:
        await interaction.edit_original_response(content="I couldn't access the configured event channel.")
        return

    bot_member = await get_bot_member(guild)
    if not bot_member:
        await interaction.edit_original_response(content="I couldn't resolve my own permissions in this server.")
        return

    ev = None
    dt = None

    tz = get_guild_timezone(cs)

    now = datetime.now(tz)

    if index is not None:
        ev = get_event_by_index(cs, index)
        if not ev:
            await interaction.edit_original_response(content="Invalid index. Use `/listevents`.")
            return
        dt = datetime.fromtimestamp(ev["timestamp"], tz=tz)
    else:
        for candidate in cs.get("events", []):
            cdt = datetime.fromtimestamp(candidate["timestamp"], tz=tz)
            if cdt > now:
                ev = candidate
                dt = cdt
                break

    if not ev or not dt:
        await interaction.edit_original_response(content="No upcoming event found to remind about.")
        return

    if ev.get("silenced", False):
        await interaction.edit_original_response(content="That event is currently silenced (use `/event` → Silence to toggle it back on).")
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
        mention_prefix, allowed = build_milestone_mention(channel, cs)

    date_str = dt.strftime("%B %d, %Y at %I:%M %p %Z")
    body = build_remindall_message(cs, event_name=ev["name"], time_left=desc, date_str=date_str)
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


    





template_group = app_commands.Group(name="template", description="Event templates (Supporter perk)")
bot.tree.add_command(template_group)

@template_group.command(name="save", description="[Pro] Save an event as a template.")
@require_pro("Event Templates")
@app_commands.describe(index="Event number from /listevents", name="Template name")
@app_commands.autocomplete(index=event_index_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def template_save_cmd(interaction: discord.Interaction, index: int, name: str):
    guild = interaction.guild
    assert guild is not None

    g = get_guild_state(guild.id)
    _cid, cs = resolve_event_channel(g, interaction.channel_id)
    if cs is None:
        await interaction.response.send_message(no_channel_guidance(g, "/template save"), ephemeral=True)
        return
    ev = get_event_by_index(cs, index)
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


@template_group.command(name="load", description="[Pro] Create a new event from a template.")
@require_pro("Event Templates")
@app_commands.describe(
    name="Template name",
    date="MM/DD/YYYY",
    time="24-hour HH:MM",
    event_name="Name for the new event",
)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def template_load_cmd(interaction: discord.Interaction, name: str, date: str, time: str, event_name: str):
    guild = interaction.guild
    assert guild is not None

    g = get_guild_state(guild.id)
    cid, cs = resolve_event_channel(g, interaction.channel_id)
    if cs is None:
        await interaction.response.send_message(no_channel_guidance(g, "/template load"), ephemeral=True)
        return
    tz = get_guild_timezone(cs)
    templates = g.get("templates", {})
    key = (name or "").strip().lower()
    tpl = templates.get(key)
    if not tpl:
        await interaction.response.send_message("Template not found. Use /template save first.", ephemeral=True)
        return

    try:
        dt = datetime.strptime(f"{date} {time}", "%m/%d/%Y %H:%M").replace(tzinfo=tz)
    except ValueError:
        await interaction.response.send_message("Invalid date/time. Use MM/DD/YYYY + 24-hour HH:MM.", ephemeral=True)
        return

    if dt <= datetime.now(tz):
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
        "dm_opt_in": False,
        "start_announced": False,
        "banner_url": None,
        "created_by_user_id": int(interaction.user.id),
        "created_by_name": maker_name,
        "owner_user_id": int(interaction.user.id),
        "owner_name": maker_name,
    }

    cs["events"].append(new_ev)
    sort_events(cs)
    save_state()
    ch = await get_text_channel(cid)
    if ch:
        await rebuild_pinned_message_for_channel(ch, cs, g)

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
    



















@bot.tree.command(name="archivepast", description="Remove past events.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def archivepast(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None
    
    await interaction.response.defer(ephemeral=True)

    g = get_guild_state(guild.id)
    removed = 0
    for cid, cs in iter_channel_states(g):
        tz = get_guild_timezone(cs)
        now = datetime.now(tz)
        before = len(cs.get("events", []))
        cs["events"] = [ev for ev in cs.get("events", []) if datetime.fromtimestamp(ev["timestamp"], tz=tz) > now]
        if len(cs["events"]) != before:
            removed += before - len(cs["events"])
            ch = await get_text_channel(cid)
            if ch:
                await rebuild_pinned_message_for_channel(ch, cs, g)
    save_state()

    await interaction.edit_original_response(content=f"🧹 Archived **{removed}** past event(s) across this server's countdown channels.")




@bot.tree.command(name="healthcheck", description="Show config + permission diagnostics.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def healthcheck(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None
    g = get_guild_state(guild.id)

    n_channels = count_countdown_channels(g)
    lines = ["**ChronoBot Healthcheck**", f"Server: **{guild.name}**",
             f"Countdown channels: **{n_channels}** • Pro: {'✅' if is_pro(g) else '❌'}"]

    if n_channels == 0:
        lines.append("\nNo countdown channels yet. Run `/seteventchannel` in a channel to start.")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)
        return

    bot_member = await get_bot_member(guild)
    for cid, cs in iter_channel_states(g):
        ch = await get_text_channel(cid)
        n_events = len(cs.get("events", []))
        if ch is None:
            lines.append(f"\n<#{cid}>: ❌ (channel not found / not accessible) — {n_events} event(s)")
            continue
        lines.append(f"\n{ch.mention} — {n_events} event(s)")
        if bot_member:
            perms = ch.permissions_for(bot_member)
            lines.append(f"• view {'✅' if perms.view_channel else '❌'} • send {'✅' if perms.send_messages else '❌'} "
                         f"• embed {'✅' if perms.embed_links else '❌'} • history {'✅' if perms.read_message_history else '❌'} "
                         f"• pin {'✅' if perms.manage_messages else '❌'}")
        role_id = cs.get("mention_role_id")
        if role_id:
            role = guild.get_role(int(role_id))
            lines.append(f"• Mention role: {role.mention if role else '❌ (not found)'}")

    await interaction.response.send_message("\n".join(lines)[:1990], ephemeral=True)


@bot.tree.command(name="purgeevents", description="Delete all events in this countdown channel (requires confirm).")
@app_commands.describe(confirm="Type YES to confirm you want to delete all events.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def purgeevents(interaction: discord.Interaction, confirm: str):
    guild = interaction.guild
    assert guild is not None

    await interaction.response.defer(ephemeral=True)

    g = get_guild_state(guild.id)
    cid, cs = resolve_event_channel(g, interaction.channel_id)
    if cs is None:
        await interaction.edit_original_response(content=no_channel_guidance(g, "/purgeevents"))
        return

    if (confirm or "").strip().upper() != "YES":
        await interaction.edit_original_response(content="Not confirmed. To purge, run `/purgeevents confirm: YES`.")
        return

    cs["events"] = []
    save_state()

    ch = await get_text_channel(cid)
    if ch:
        await rebuild_pinned_message_for_channel(ch, cs, g)

    await interaction.edit_original_response(content=f"🧨 All events deleted in <#{cid}>.")


@bot.tree.command(name="update_countdown", description="Force-refresh the pinned countdown.")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.guild_only()
async def update_countdown_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    await interaction.response.defer(ephemeral=True)

    g = get_guild_state(guild.id)
    cid, cs = resolve_event_channel(g, interaction.channel_id)
    if cs is None:
        await interaction.edit_original_response(content=no_channel_guidance(g, "/update_countdown"))
        return

    channel = await get_text_channel(cid)
    if channel is None:
        await interaction.edit_original_response(
            content="I couldn't access that countdown channel. Check my permissions.",
        )
        return

    await refresh_countdown_message_for_channel(guild, channel, cs, g)
    await interaction.edit_original_response(content=f"⏱ Countdown refreshed in <#{cid}>.")


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






@bot.tree.command(name="chronohelp", description="Show Chromie help (paged).")
async def chronohelp(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=build_help_embed("quick"),
        view=HelpView(),
        ephemeral=True,
    )


# ==========================
# TIMEZONE AUTOCOMPLETE
# ==========================

async def timezone_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[str]]:
    """Autocomplete for timezone selection"""
    import pytz
    
    # Get all available timezones
    all_timezones = pytz.all_timezones
    
    # Filter based on user input
    current_lower = (current or "").lower()
    
    # Prioritize common timezones
    common_first = [
        "UTC",
        "US/Eastern",
        "US/Central",
        "US/Mountain",
        "US/Pacific",
        "Europe/London",
        "Europe/Paris",
        "Asia/Tokyo",
        "Australia/Sydney",
    ]
    
    # Separate common and other timezones
    common_matches = []
    other_matches = []
    
    for tz in all_timezones:
        if current_lower and current_lower not in tz.lower():
            continue
        
        if tz in common_first:
            common_matches.append(tz)
        else:
            other_matches.append(tz)
    
    # Sort and combine (common first, then others)
    matches = sorted(common_matches) + sorted(other_matches)
    
    # Return top 25 matches as choices
    return [
        app_commands.Choice(name=tz, value=tz)
        for tz in matches[:25]
    ]


# ==========================
# TIMEZONE COMMANDS
# ==========================





# ==========================
# REMINDER TIME COMMANDS
# ==========================

def validate_time_format(time_str: str) -> bool:
    """Validate time format HH:MM (24-hour)"""
    try:
        datetime.strptime(time_str.strip(), "%H:%M")
        return True
    except ValueError:
        return False


# ==========================
# EVENT INDEX AUTOCOMPLETE
# ==========================

async def event_index_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[int]]:
    """Autocomplete for event index with event names"""
    try:
        guild_state = get_guild_state(interaction.guild_id)
        events = guild_state.get("events", [])
        
        if not events:
            return []
        
        # Create choices showing event names
        choices = []
        current_lower = (current or "").lower()
        
        for idx, event in enumerate(events):
            event_name = event.get("name", f"Event {idx + 1}")
            # 1-based indexing for display
            display_index = idx + 1
            
            # Filter based on user input (search by name or index number)
            if current_lower:
                if not (current_lower in event_name.lower() or current_lower in str(display_index)):
                    continue
            
            choice_name = f"{display_index}. {event_name}"
            choices.append(app_commands.Choice(name=choice_name, value=display_index))
        
        return choices[:25]  # Return top 25
    except Exception:
        return []






@bot.tree.command(name="pro_status", description="Check if your server has Pro")
@app_commands.guild_only()
async def pro_status(interaction: discord.Interaction):
    """Check Pro subscription status"""
    await interaction.response.defer(ephemeral=True)
    
    guild_state = get_guild_state(interaction.guild_id)
    pro_active = is_pro(guild_state)
    
    if pro_active:
        embed = discord.Embed(
            title="✅ PRO ACTIVE",
            description="Your server has Chromie Pro!",
            color=discord.Color.green()
        )
    else:
        embed = discord.Embed(
            title="❌ NOT PRO",
            description="Your server does not have Chromie Pro.",
            color=discord.Color.red()
        )
    
    pro_data = guild_state.get("pro", {})
    pro_until = pro_data.get("pro_until", "Not set")
    is_discord_sub = pro_data.get("discord_subscription", False)
    
    embed.add_field(name="Pro Until", value=str(pro_until), inline=False)
    if is_discord_sub:
        embed.add_field(name="Type", value="Discord Subscription (Auto-renewing)", inline=False)
    
    await interaction.edit_original_response(embed=embed)


@bot.tree.command(name="sync_subscription", description="Sync your Discord subscription status")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.guild_only()
async def sync_subscription(interaction: discord.Interaction):
    """Sync Discord subscription status from Discord's servers"""
    await interaction.response.defer(ephemeral=True)
    
    embed = discord.Embed(
        title="🔄 Syncing Subscription...",
        description="Checking Discord for active subscriptions...",
        color=discord.Color.blue()
    )
    await interaction.edit_original_response(embed=embed)
    
    try:
        # Try to sync the subscription
        has_pro = await sync_discord_subscription(interaction.guild_id)
        
        if has_pro:
            embed = discord.Embed(
                title="✅ Subscription Synced!",
                description="Your Discord subscription has been synced. You now have Chromie Pro!",
                color=discord.Color.green()
            )
        else:
            embed = discord.Embed(
                title="❌ No Active Subscription",
                description="No active Discord subscription found for Chromie Pro.",
                color=discord.Color.red()
            )
            embed.add_field(
                name="Need Pro?",
                value="Subscribe to Chromie Pro via Discord Server Subscription for $2.99/month",
                inline=False
            )
        
        await interaction.edit_original_response(embed=embed)
    except Exception as e:
        embed = discord.Embed(
            title="❌ Error Syncing",
            description=f"Could not sync subscription: {str(e)}",
            color=discord.Color.red()
        )
        await interaction.edit_original_response(embed=embed)


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

# ==========================


# ==========================
# OWNER-ONLY COMMANDS (from spec)
# ==========================

@bot.tree.command(name="owner_unlock", description="[Owner] Temporarily unlock features")
@app_commands.describe(
    feature="supporter or pro",
    duration_hours="Hours to unlock (default 12)"
)
async def owner_unlock_command(
    interaction: discord.Interaction,
    feature: str,
    duration_hours: int = 12
):
    """Owner-only temporary unlock."""
    if interaction.user.id != bot.application_info_cached.owner.id if hasattr(bot, 'application_info_cached') else None:
        # Try to get owner ID
        if not hasattr(bot, 'application_info_cached'):
            try:
                app_info = await bot.application_info()
                bot.application_info_cached = app_info
            except:
                await interaction.response.send_message("❌ Owner only", ephemeral=True)
                return
        
        if interaction.user.id != bot.application_info_cached.owner.id:
            await interaction.response.send_message("❌ Owner only", ephemeral=True)
            return
    
    guild_state = get_guild_state(interaction.guild_id)
    now = datetime.now(timezone.utc)
    until = now + timedelta(hours=duration_hours)
    
    if feature.lower() == "supporter":
        if "supporter" not in guild_state:
            guild_state["supporter"] = {}
        guild_state["supporter"]["last_vote_at"] = now.isoformat()
        guild_state["supporter"]["vote_until"] = until.isoformat()
        msg = f"✅ Supporter unlocked for {duration_hours} hours"
    elif feature.lower() == "pro":
        if "pro" not in guild_state:
            guild_state["pro"] = {}
        guild_state["pro"]["pro_active"] = True
        guild_state["pro"]["pro_until"] = until.isoformat()
        msg = f"✅ Pro unlocked for {duration_hours} hours"
    else:
        await interaction.response.send_message("❌ Invalid feature (use: supporter or pro)", ephemeral=True)
        return
    
    save_state()
    for cid, cs in iter_channel_states(guild_state):
        ch = await get_text_channel(cid)
        if ch:
            try:
                await refresh_countdown_message_for_channel(interaction.guild, ch, cs, guild_state)
            except Exception:
                pass

    await interaction.response.send_message(msg, ephemeral=True)


# ==========================
# LAUNCH ANNOUNCEMENT (owner-only, one-off broadcast)
# ==========================
# Bump this string to broadcast a NEW announcement later; the per-channel
# "already-announced" guard is keyed on it, so each version sends at most once
# per channel even if the command is re-run.
ANNOUNCEMENT_VERSION = "per-channel-2026-06"


def _build_launch_announcement_embed() -> discord.Embed:
    return discord.Embed(
        title="🎉 Chromie just leveled up!",
        description=(
            "You can now run **multiple countdowns — one per channel!** 🗓️\n"
            "Perfect for keeping kids' events, game nights, and community stuff on their own boards.\n\n"
            "**✨ The new flow:**\n"
            "🆕 `/seteventchannel` — turn any channel into a countdown "
            "*(1 free; more with ChronoBot Plus 💎)*\n"
            "📝 `/event` — add, edit & customize events: milestones, owners, repeats, "
            "banners & per-event DM reminders — all in one menu\n"
            "🎨 `/countdown` — make it yours: themes with **live preview**, timezone, "
            "time format, or build-your-own (Pro)\n\n"
            "Fresh themes too, including **Family & Kids** 👨‍👩‍👧‍👦 and seasonal looks.\n"
            "Type `/chronohelp` anytime — happy counting! ⏳💜"
        ),
        color=EMBED_COLOR,
    )


async def _is_bot_owner(interaction: discord.Interaction) -> bool:
    try:
        if not getattr(bot, "application_info_cached", None):
            bot.application_info_cached = await bot.application_info()
        return interaction.user.id == bot.application_info_cached.owner.id
    except Exception:
        return False


async def _broadcast_launch_announcement(embed: discord.Embed, *, confirm: bool, throttle: float = 0.5) -> dict:
    """Post `embed` once to every active countdown channel (one with a pinned
    countdown). Idempotent per ANNOUNCEMENT_VERSION, throttled, and resumable
    (progress is saved after each send). With confirm=False it only counts."""
    sent = skipped = failed = would_send = 0
    for gid_str, guild_state in list(state.get("guilds", {}).items()):
        announced = guild_state.setdefault("_announced", {})
        done = announced.setdefault(ANNOUNCEMENT_VERSION, [])
        for cid, cs in iter_channel_states(guild_state):
            if not cs.get("pinned_message_id"):
                continue  # only channels with a live countdown
            if str(cid) in done:
                skipped += 1
                continue
            would_send += 1
            if not confirm:
                continue  # dry run: count only
            channel = await get_text_channel(int(cid))
            if channel is None:
                failed += 1
                continue
            try:
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                done.append(str(cid))
                sent += 1
                save_state()  # persist progress so a restart resumes
                if throttle:
                    await asyncio.sleep(throttle)
            except Exception as e:
                failed += 1
                print(f"[Announce] {gid_str}/{cid} failed: {type(e).__name__}: {e}")
    return {"sent": sent, "skipped": skipped, "failed": failed, "would_send": would_send}


@bot.tree.command(name="announce_update", description="[Owner] Broadcast the launch announcement to all countdown channels.")
@app_commands.describe(confirm="Leave unchecked for a dry run (counts only). Set True to actually send.")
async def announce_update(interaction: discord.Interaction, confirm: bool = False):
    if not await _is_bot_owner(interaction):
        await interaction.response.send_message("❌ Owner only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    embed = _build_launch_announcement_embed()
    result = await _broadcast_launch_announcement(embed, confirm=confirm)

    if not confirm:
        msg = (
            f"🧪 **Dry run — nothing sent.**\n"
            f"Would post to **{result['would_send']}** countdown channels "
            f"(**{result['skipped']}** already announced for `{ANNOUNCEMENT_VERSION}`).\n\n"
            f"Re-run with **confirm: True** to send for real."
        )
    else:
        msg = (
            f"📣 **Announcement broadcast complete!**\n"
            f"✅ Posted: **{result['sent']}**\n"
            f"⏭️ Skipped (already announced): **{result['skipped']}**\n"
            f"⚠️ Failed (no access / channel gone): **{result['failed']}**"
        )
    await interaction.followup.send(msg, ephemeral=True)
