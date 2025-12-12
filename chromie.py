import os
import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, Tuple, List

import discord
from discord.ext import commands, tasks
from discord import app_commands

VERSION = "ChronoBot v3.0 (2025-12-12)"
print("BOOTING:", VERSION)

# ==========================
# CONFIG
# ==========================

# Default timezone for all events
DEFAULT_TZ = ZoneInfo("America/Chicago")

# How often to update pinned countdowns (seconds)
UPDATE_INTERVAL_SECONDS = 60

# Default milestones (days before event) – messages at these offsets
DEFAULT_MILESTONES = [100, 50, 30, 14, 7, 2, 1, 0]

# Where we store all data (per server)
DATA_FILE = Path(os.getenv("CHROMIE_DATA_PATH", "/var/data/chromie_state.json"))

# Bot token – preferred: set DISCORD_BOT_TOKEN in your hosting env
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
# For local testing only, you *could* paste your token here instead:
# if not TOKEN:
#     TOKEN = "YOUR_BOT_TOKEN_HERE"

EMBED_COLOR = discord.Color.from_rgb(140, 82, 255)  # ChronoBot purple

# ==========================
# STATE HANDLING
# ==========================

"""
State structure:

{
  "guilds": {
    "123456789012345678": {
      "event_channel_id": 987654321098765432,
      "pinned_message_id": 123123123123123123,
      "events": [
        {
          "name": "Couples Retreat 💕",
          "timestamp": 1771000800,          # unix seconds
          "milestones": [100, 50, 30, ...],
          "announced_milestones": [100, 50],
        },
        ...
      ],
      "welcomed": true
    },
    ...
  },
  "user_links": {
    "user_id_str": guild_id_int
  }
}
"""


def load_state():
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    else:
        data = {}

    data.setdefault("guilds", {})
    data.setdefault("user_links", {})
    return data


def save_state():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def get_guild_state(guild_id: int):
    gid = str(guild_id)
    guilds = state.setdefault("guilds", {})
    if gid not in guilds:
        guilds[gid] = {
            "event_channel_id": None,
            "pinned_message_id": None,
            "events": [],
            "welcomed": False,
            "timezone": "America/Chicago",
            "mention_role_id": None,
        }
    else:
        guilds[gid].setdefault("event_channel_id", None)
        guilds[gid].setdefault("pinned_message_id", None)
        guilds[gid].setdefault("events", [])
        guilds[gid].setdefault("welcomed", False)
        guilds[gid].setdefault("timezone", "America/Chicago")
        guilds[gid].setdefault("mention_role_id", None)
    return guilds[gid]


def get_user_links():
    return state.setdefault("user_links", {})


def sort_events(guild_state: dict):
    """Sort events soonest → farthest based on timestamp."""
    events = guild_state.get("events", [])
    events.sort(key=lambda ev: ev.get("timestamp", 0))
    guild_state["events"] = events



def get_guild_tz(guild_state: dict) -> ZoneInfo:
    """Return the guild's configured timezone (fallback to DEFAULT_TZ)."""
    tz_name = guild_state.get("timezone") or str(DEFAULT_TZ)
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return DEFAULT_TZ


def get_guild_mention_role(guild: discord.Guild, guild_state: dict) -> Optional[discord.Role]:
    """Return configured mention role if it exists in this guild."""
    role_id = guild_state.get("mention_role_id")
    if not role_id:
        return None
    return guild.get_role(int(role_id))


def build_milestone_text(event_name: str, days_left: int) -> str:
    if days_left <= 0:
        return f"🎉 **{event_name}** is **today**! 🎉"
    if days_left == 1:
        return f"✨ **{event_name}** is **tomorrow**! ✨"
    return f"💌 **{event_name}** is **{days_left} day{'s' if days_left != 1 else ''}** away!"

state = load_state()
for _, g_state in state.get("guilds", {}).items():
    sort_events(g_state)
save_state()


# ==========================
# DISCORD SETUP
# ==========================

intents = discord.Intents.default()
# Slash commands do NOT require message_content, but having it on is fine.
bot = commands.Bot(command_prefix="None", intents=intents)


async def send_onboarding_for_guild(guild: discord.Guild):
    """Send the onboarding/setup message for a guild, and mark it welcomed once."""
    guild_state = get_guild_state(guild.id)

    # If we've already tried to welcome this guild, don't do it again automatically
    if guild_state.get("welcomed"):
        return

    contact_user = guild.owner or (await bot.fetch_user(guild.owner_id))
    setup_message = (
    f"Hi {contact_user.mention if contact_user else ''}! "
    f"Thanks for adding **ChronoBot** to **{guild.name}** 🕒💕\n\n"
    "I’m Chromie, your server’s friendly countdown bot for all your upcoming events! "
    "I’ll keep track of the big day and send reminders along the way, so no one forgets what’s coming up.\n\n"
    "I’ll announce milestones at **100 days, 50 days, about 1 month (30 days), 14 days, 1 week, 2 days, "
    "the day before, and on the day of the event**.\n\n"
    "Goodbye forgotten events, hello Chromie-powered hype.\n\n"
    "**Here’s a quick setup guide:**\n\n"
    "1️⃣ **Choose your events channel**\n"
    "   • Go to the channel where you want the live countdown pinned.\n"
    "   • Run: `/seteventchannel`\n\n"
    "2️⃣ **Add your first event (MM/DD/YYYY)**\n"
    "   • Example: `/addevent date: 04/12/2026 time: 09:00 name: Game Night  `\n"
    "   • Format: `MM/DD/YYYY` and `HH:MM` 24-hour time (server timezone).\n\n"
    "3️⃣ **Manage your events**\n"
    "   • `/listevents` – show all events\n"
    "   • `/removeevent` – remove by list number\n"
    "   • `/update_countdown` – refresh the pinned countdown\n\n"
    "🔁 **Optional: DM control**\n"
    "   • In this server, run `/linkserver`.\n"
    "   • Then DM me: `/addevent` with your date, time, and name.\n\n"
    "I’ll handle the live countdown and milestone reminders automatically once an "
    "events channel and at least one event are set up. ✨"
    )


    sent = False
    if contact_user:
        try:
            await contact_user.send(setup_message)
            sent = True
        except discord.Forbidden:
            sent = False

    if not sent:
        # Fallback: try system channel, then first text channel where I can speak
        fallback_channel = guild.system_channel
        if fallback_channel is None:
            for channel in guild.text_channels:
                perms = channel.permissions_for(guild.me)
                if perms.send_messages:
                    fallback_channel = channel
                    break

        if fallback_channel is not None:
            try:
                await fallback_channel.send(setup_message)
                sent = True
            except discord.Forbidden:
                sent = False

    # Mark as welcomed after the first attempt
    guild_state["welcomed"] = True
    save_state()

CLEANUP_GLOBAL_COMMANDS = os.getenv("CLEANUP_GLOBAL_COMMANDS", "0") == "1"
DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID", "0") or 0)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")

    # --- ONE-TIME: delete ALL global commands ---
    if CLEANUP_GLOBAL_COMMANDS:
        bot.tree.clear_commands(guild=None)   # clears local view of global commands
        await bot.tree.sync()                 # pushes empty -> deletes global commands remotely
        print("🧹 Deleted GLOBAL slash commands. Turn CLEANUP_GLOBAL_COMMANDS off and redeploy.")
        return

    # --- Normal dev behavior: guild sync (instant) ---
    if DEV_GUILD_ID:
        guild = discord.Object(id=DEV_GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        print(f"✅ Synced to guild {DEV_GUILD_ID}")
    else:
        await bot.tree.sync()
        print("✅ Synced globally (may take time)")

@bot.event
async def on_guild_join(guild: discord.Guild):
    """When the bot is added to a new server, send the setup guide."""
    g_state = get_guild_state(guild.id)
    sort_events(g_state)
    save_state()
    await send_onboarding_for_guild(guild)


# ==========================
# TIME & EMBED HELPERS
# ==========================

def compute_time_left(dt: datetime, tz: ZoneInfo):
    """Return (description, days_left, event_passed)."""
    now = datetime.now(tz)
    delta = dt - now
    total_seconds = int(delta.total_seconds())

    if total_seconds <= 0:
        return "The event is happening now or has already started! 💕", 0, True

    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)

    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if not parts:
        parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")

    return " • ".join(parts), days, False


def build_embed_for_guild(guild_state: dict):
    sort_events(guild_state)
    tz = get_guild_tz(guild_state)
    events = guild_state.get("events", [])
    embed = discord.Embed(
        title="Upcoming Event Countdowns",
        description="Live countdowns for this server’s events.",
        color=EMBED_COLOR,
    )

    if not events:
        embed.add_field(
            name="No events yet",
            value="Use `/addevent` to add one.",
            inline=False,
        )
        return embed

    any_upcoming = False

    for ev in events:
        dt = datetime.fromtimestamp(ev["timestamp"], tz=tz)
        desc, days_left, passed = compute_time_left(dt, tz)
        date_str = dt.strftime("%B %d, %Y at %I:%M %p %Z")

        if passed:
            value = f"**{date_str}**\n➡️ Event has started or passed. 🎉"
        else:
            any_upcoming = True
            value = f"**{date_str}**\n⏱ **{desc}** remaining"

        embed.add_field(
            name=ev["name"],
            value=value,
            inline=False,
        )

    if not any_upcoming:
        embed.set_footer(text="All listed events have already started or passed.")

    return embed


async def rebuild_pinned_message(guild_id: int, channel: discord.TextChannel, guild_state: dict):
    """Unpin the old pinned countdown (if any), send a new one, and pin it."""
    sort_events(guild_state)
    old_id = guild_state.get("pinned_message_id")
    if old_id:
        try:
            old_msg = await channel.fetch_message(old_id)
            await old_msg.unpin()
        except (discord.NotFound, discord.Forbidden):
            pass

    embed = build_embed_for_guild(guild_state)
    msg = await channel.send(embed=embed)

    try:
        await msg.pin()
    except discord.Forbidden:
        print(f"[Guild {guild_id}] Missing permission to pin messages.")
    except discord.HTTPException as e:
        print(f"[Guild {guild_id}] Failed to pin message: {e}")

    guild_state["pinned_message_id"] = msg.id
    save_state()
    return msg


async def get_or_create_pinned_message(guild_id: int, channel: discord.TextChannel):
    guild_state = get_guild_state(guild_id)
    sort_events(guild_state)
    pinned_id = guild_state.get("pinned_message_id")

    if pinned_id:
        try:
            msg = await channel.fetch_message(pinned_id)
            return msg
        except discord.NotFound:
            pass

    embed = build_embed_for_guild(guild_state)
    msg = await channel.send(embed=embed)

    try:
        await msg.pin()
    except discord.Forbidden:
        print(f"[Guild {guild_id}] Missing permission to pin messages.")
    except discord.HTTPException as e:
        print(f"[Guild {guild_id}] Failed to pin message: {e}")

    guild_state["pinned_message_id"] = msg.id
    save_state()
    return msg


# ==========================
# BACKGROUND LOOP
# ==========================

@tasks.loop(seconds=UPDATE_INTERVAL_SECONDS)
async def update_countdowns():
    guilds = state.get("guilds", {})
    for gid_str, guild_state in list(guilds.items()):
        guild_id = int(gid_str)
        sort_events(guild_state)
        tz = get_guild_tz(guild_state)
        channel_id = guild_state.get("event_channel_id")
        if not channel_id:
            continue

        channel = bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            continue

        # Update pinned embed
        pinned = await get_or_create_pinned_message(guild_id, channel)
        embed = build_embed_for_guild(guild_state)
        try:
            await pinned.edit(embed=embed)
        except discord.HTTPException as e:
            print(f"[Guild {guild_id}] Failed to edit pinned message: {e}")

        # Milestone checks
        for ev in guild_state.get("events", []):
            dt = datetime.fromtimestamp(ev["timestamp"], tz=tz)
            desc, days_left, passed = compute_time_left(dt, tz)
            if passed or days_left < 0:
                continue

            milestones = ev.get("milestones", DEFAULT_MILESTONES)
            announced = ev.get("announced_milestones", [])

            if ev.get("silenced"):
                continue

            if days_left in milestones and days_left not in announced:
                text = build_milestone_text(ev["name"], days_left)

                role = get_guild_mention_role(channel.guild, guild_state)
                if role:
                    text = f"{role.mention} {text}"

                await channel.send(text)

                announced.append(days_left)
                ev["announced_milestones"] = announced
                save_state()


# ==========================
# SLASH COMMANDS
# ==========================

def format_events_list(guild_state: dict) -> str:
    sort_events(guild_state)
    tz = get_guild_tz(guild_state)
    events = guild_state.get("events", [])
    if not events:
        return (
            "There are no events set for this server yet.\n"
            "Add one with `/addevent`."
        )

    lines = []
    for idx, ev in enumerate(events, start=1):
        dt = datetime.fromtimestamp(ev["timestamp"], tz=tz)
        desc, days_left, passed = compute_time_left(dt, tz)
        status = "✅ done" if passed else "⏳ active"
        lines.append(
            f"**{idx}. {ev['name']}** — {dt.strftime('%m/%d/%Y %H:%M')} "
            f"({desc}) [{status}]"
        )
    return "\n".join(lines)


@bot.tree.command(name="seteventchannel", description="Set this channel as the event countdown channel.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def seteventchannel(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    guild_state = get_guild_state(guild.id)
    guild_state["event_channel_id"] = interaction.channel.id
    guild_state["pinned_message_id"] = None
    sort_events(guild_state)
    save_state()

    await interaction.response.send_message(
        "✅ This channel is now the event countdown channel for this server.\n"
        "Use `/addevent` to add events.",
        ephemeral=True,
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
        "🔗 Linked your user to this server.\n"
        "You can now DM me `/addevent` and I’ll add events to this server (as long as you still have Manage Server).",
        ephemeral=True,
    )

@bot.tree.command(name="addevent", description="Add a new event to the countdown.")
@app_commands.describe(
    date="Date in MM/DD/YYYY format",
    time="Time in 24-hour HH:MM format (server timezone)",
    name="Name of the event",
)
async def addevent(interaction: discord.Interaction, date: str, time: str, name: str):
    user = interaction.user

    # ---------------------------
    # Decide which guild to target
    # ---------------------------
    if interaction.guild is not None:
        # In a server: require Manage Server OR Administrator
        guild = interaction.guild

        member = interaction.user
        if not isinstance(member, discord.Member):
            member = guild.get_member(user.id)

        perms = getattr(member, "guild_permissions", None)
        if not perms or not (perms.manage_guild or perms.administrator):
            await interaction.response.send_message(
                "You need the **Manage Server** or **Administrator** permission "
                "to add events in this server.",
                ephemeral=True,
            )
            return

        guild_state = get_guild_state(guild.id)
        is_dm = False

    else:
        # In DMs: use linked server (no extra member/permission check here)
        user_links = get_user_links()
        linked_guild_id = user_links.get(str(user.id))
        if not linked_guild_id:
            await interaction.response.send_message(
                "I don't know which server to use for your DMs yet.\n"
                "In the server you want to control, run `/linkserver`, then use `/addevent` here again.",
                ephemeral=True,
            )
            return

        guild = bot.get_guild(linked_guild_id)
        if not guild:
            await interaction.response.send_message(
                "I can't find the linked server anymore. Maybe I was removed from it?\n"
                "Re-add me and run `/linkserver` again.",
                ephemeral=True,
            )
            return

        # At this point we trust the link: the user who linked is the one using DMs.
        guild_state = get_guild_state(guild.id)
        is_dm = True

    # ---------------------------
    # Make sure we have an events channel
    # ---------------------------
    if not guild_state.get("event_channel_id"):
        msg = (
            "I don't know which channel to use yet.\n"
            "Run `/seteventchannel` in the channel where you want the countdown pinned."
        )
        if is_dm:
            msg += "\n(Do this in the linked server.)"
        await interaction.response.send_message(msg, ephemeral=True)
        return

    # ---------------------------
    # Parse date/time (MM/DD/YYYY HH:MM)
    # ---------------------------
    try:
        dt = datetime.strptime(f"{date} {time}", "%m/%d/%Y %H:%M")
    except ValueError:
        await interaction.response.send_message(
            "I couldn't understand that date/time.\n"
            "Use something like: `date: 04/12/2026` `time: 09:00` (MM/DD/YYYY and 24-hour time).",
            ephemeral=True,
        )
        return

    tz = get_guild_tz(guild_state)
    dt = dt.replace(tzinfo=tz)

    event = {
        "name": name,
        "timestamp": int(dt.timestamp()),
        "milestones": DEFAULT_MILESTONES.copy(),
        "announced_milestones": [],
    }

    guild_state["events"].append(event)
    sort_events(guild_state)
    save_state()

    # Rebuild pinned message with the new full event list
    channel_id = guild_state.get("event_channel_id")
    channel = bot.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        await rebuild_pinned_message(guild.id, channel, guild_state)

    await interaction.response.send_message(
        f"✅ Added event **{name}** on {dt.strftime('%B %d, %Y at %I:%M %p %Z')} "
        f"in server **{guild.name}**.",
        ephemeral=True,
    )
    
@bot.tree.command(name="listevents", description="List all events for this server.")
@app_commands.guild_only()
async def listevents(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    guild_state = get_guild_state(guild.id)
    text = format_events_list(guild_state)
    await interaction.response.send_message(text, ephemeral=True)

@bot.tree.command(
    name="removeevent",
    description="Remove an event by its list number (from /listevents)."
)
@app_commands.describe(
    index="The number shown in /listevents (1, 2, 3, ...)"
)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def removeevent(interaction: discord.Interaction, index: int):
    guild = interaction.guild
    assert guild is not None  # guaranteed by @guild_only

    guild_state = get_guild_state(guild.id)
    # Make sure events are in soonest → farthest order
    sort_events(guild_state)
    events = guild_state.get("events", [])

    if not events:
        await interaction.response.send_message(
            "There are no events to remove.",
            ephemeral=True,
        )
        return

    if index < 1 or index > len(events):
        await interaction.response.send_message(
            f"Index must be between 1 and {len(events)}.",
            ephemeral=True,
        )
        return

    # Remove the chosen event
    ev = events.pop(index - 1)
    save_state()

    # Rebuild the pinned countdown so the list is accurate
    channel_id = guild_state.get("event_channel_id")
    channel = bot.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        await rebuild_pinned_message(guild.id, channel, guild_state)

    await interaction.response.send_message(
        f"🗑 Removed event **{ev['name']}**.",
        ephemeral=True,
    )


@bot.tree.command(name="update_countdown", description="Force-refresh the pinned countdown.")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.guild_only()
async def update_countdown_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    guild_state = get_guild_state(guild.id)
    sort_events(guild_state)
    channel_id = guild_state.get("event_channel_id")

    if not channel_id:
        await interaction.response.send_message(
            "No events channel set yet. Run `/seteventchannel` in your events channel.",
            ephemeral=True,
        )
        return

    if interaction.channel_id != channel_id:
        await interaction.response.send_message(
            "Please run this command in the configured events channel.",
            ephemeral=True,
        )
        return

    channel = interaction.channel
    assert isinstance(channel, discord.TextChannel)

    pinned = await get_or_create_pinned_message(guild.id, channel)
    embed = build_embed_for_guild(guild_state)
    await pinned.edit(embed=embed)

    await interaction.response.send_message(
        "⏱ Countdown updated.",
        ephemeral=True,
    )


@bot.tree.command(name="resendsetup", description="Resend the onboarding/setup message.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def resendsetup(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    g_state = get_guild_state(guild.id)
    g_state["welcomed"] = False
    save_state()

    await send_onboarding_for_guild(guild)
    await interaction.response.send_message(
        "📨 Setup instructions have been resent to the server owner (or a fallback channel).",
        ephemeral=True,
    )


@bot.tree.command(name="chronohelp", description="Show ChronoBot setup & command help.")
async def chronohelp(interaction: discord.Interaction):
    text = (
        "**ChronoBot – Setup & Commands**\n\n"
        "All slash command responses are ephemeral, so only you see them.\n\n"
        "1️⃣ Pick your events channel (in a server):\n"
        "   • Go to the channel you want the pinned countdown in.\n"
        "   • Run: `/seteventchannel`\n\n"
        "2️⃣ Add an event (MM/DD/YYYY):\n"
        "   • Example: `/addevent date: 04/12/2026 time: 09:00 name: Couples Retreat 💕`\n"
        "   • Format: `MM/DD/YYYY` and 24-hour `HH:MM` (server timezone).\n\n"
        "3️⃣ Manage events (in a server):\n"
        "   • `/listevents` – show all events (soonest → farthest)\n"
        "   • `/removeevent index: <number>` – remove by list number\n"
        "   • `/update_countdown` – force-refresh the pinned countdown\n\n"
        "4️⃣ Optional: DM control:\n"
        "   • In your server, run `/linkserver` (requires Manage Server).\n"
        "   • Then DM me `/addevent` with your event details.\n\n"
        "5️⃣ Onboarding:\n"
        "   • `/resendsetup` – resend the setup guide to the server owner.\n\n"
        "I’ll keep the pinned message updated and announce milestone reminders automatically. ✨"
    )
    await interaction.response.send_message(text, ephemeral=True)



# --------------------------
# Extra utilities
# --------------------------

def _require_events_channel(guild: discord.Guild, guild_state: dict) -> Tuple[Optional[discord.TextChannel], Optional[str]]:
    """Return (channel, error_message)."""
    channel_id = guild_state.get("event_channel_id")
    if not channel_id:
        return None, "No events channel set yet. Run `/seteventchannel` in your events channel."
    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return None, "Configured events channel is missing or not a text channel. Run `/seteventchannel` again."
    return channel, None


def _parse_milestones(raw: str) -> Optional[List[int]]:
    try:
        parts = [p.strip() for p in raw.split(",") if p.strip() != ""]
        ms = [int(p) for p in parts]
    except Exception:
        return None
    if not ms:
        return None
    if any(m < 0 for m in ms):
        return None
    # de-dupe while preserving order
    out = []
    seen = set()
    for m in ms:
        if m not in seen:
            out.append(m)
            seen.add(m)
    return out


# --------------------------
# New commands (Batch 2)
# --------------------------

@bot.tree.command(name="nextevent", description="Show the next upcoming event for this server.")
@app_commands.guild_only()
async def nextevent(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    guild_state = get_guild_state(guild.id)
    sort_events(guild_state)
    tz = get_guild_tz(guild_state)

    events = [ev for ev in guild_state.get("events", [])]
    if not events:
        await interaction.response.send_message("No events yet. Add one with `/addevent`.", ephemeral=True)
        return

    # Find first not-passed
    next_ev = None
    for ev in events:
        dt = datetime.fromtimestamp(ev["timestamp"], tz=tz)
        _, _, passed = compute_time_left(dt, tz)
        if not passed:
            next_ev = ev
            break

    if not next_ev:
        await interaction.response.send_message("All events have already started/passed. Use `/archivepast` to clean up.", ephemeral=True)
        return

    dt = datetime.fromtimestamp(next_ev["timestamp"], tz=tz)
    desc, days_left, _ = compute_time_left(dt, tz)
    embed = discord.Embed(title="Next Event", color=EMBED_COLOR)
    embed.add_field(name=next_ev["name"], value=f"**{dt.strftime('%B %d, %Y at %I:%M %p %Z')}**\n⏱ **{desc}** remaining", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="eventinfo", description="Show detailed info for one event by index (from /listevents).")
@app_commands.describe(index="The number shown in /listevents (1, 2, 3, ...)")
@app_commands.guild_only()
async def eventinfo(interaction: discord.Interaction, index: int):
    guild = interaction.guild
    assert guild is not None

    guild_state = get_guild_state(guild.id)
    sort_events(guild_state)
    tz = get_guild_tz(guild_state)
    events = guild_state.get("events", [])

    if not events:
        await interaction.response.send_message("There are no events yet.", ephemeral=True)
        return
    if index < 1 or index > len(events):
        await interaction.response.send_message(f"Index must be between 1 and {len(events)}.", ephemeral=True)
        return

    ev = events[index - 1]
    dt = datetime.fromtimestamp(ev["timestamp"], tz=tz)
    desc, days_left, passed = compute_time_left(dt, tz)

    milestones = ev.get("milestones", DEFAULT_MILESTONES)
    announced = ev.get("announced_milestones", [])
    silenced = bool(ev.get("silenced"))

    embed = discord.Embed(title="Event Info", color=EMBED_COLOR)
    embed.add_field(name="Name", value=ev["name"], inline=False)
    embed.add_field(name="When", value=dt.strftime("%B %d, %Y at %I:%M %p %Z"), inline=False)
    embed.add_field(name="Status", value=("✅ started/passed" if passed else f"⏳ {desc} remaining"), inline=False)
    embed.add_field(name="Milestones", value=", ".join(str(m) for m in milestones), inline=False)
    embed.add_field(name="Already announced", value=(", ".join(str(m) for m in announced) if announced else "None"), inline=False)
    embed.add_field(name="Silenced", value=("Yes" if silenced else "No"), inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="editevent", description="Edit an event by index (change name and/or date/time).")
@app_commands.describe(
    index="The number shown in /listevents (1, 2, 3, ...)",
    name="New name (optional)",
    date="New date in MM/DD/YYYY (optional)",
    time="New time in 24-hour HH:MM (optional)"
)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def editevent(interaction: discord.Interaction, index: int, name: str = None, date: str = None, time: str = None):
    guild = interaction.guild
    assert guild is not None

    guild_state = get_guild_state(guild.id)
    sort_events(guild_state)
    tz = get_guild_tz(guild_state)
    events = guild_state.get("events", [])

    if not events:
        await interaction.response.send_message("There are no events to edit.", ephemeral=True)
        return
    if index < 1 or index > len(events):
        await interaction.response.send_message(f"Index must be between 1 and {len(events)}.", ephemeral=True)
        return

    ev = events[index - 1]
    old_dt = datetime.fromtimestamp(ev["timestamp"], tz=tz)

    new_name = (name or ev["name"]).strip()

    # Build new datetime if date/time provided
    new_dt = old_dt
    if date or time:
        date_part = date or old_dt.strftime("%m/%d/%Y")
        time_part = time or old_dt.strftime("%H:%M")
        try:
            naive = datetime.strptime(f"{date_part} {time_part}", "%m/%d/%Y %H:%M")
        except ValueError:
            await interaction.response.send_message(
                "Couldn't understand the new date/time. Use `MM/DD/YYYY` and 24-hour `HH:MM`.",
                ephemeral=True,
            )
            return
        new_dt = naive.replace(tzinfo=tz)

    ev["name"] = new_name
    ev["timestamp"] = int(new_dt.timestamp())

    # If the date/time changed, clear announced milestones so reminders can re-fire appropriately
    if int(old_dt.timestamp()) != ev["timestamp"]:
        ev["announced_milestones"] = []

    save_state()

    channel, err = _require_events_channel(guild, guild_state)
    if channel:
        await rebuild_pinned_message(guild.id, channel, guild_state)

    await interaction.response.send_message(
        f"✅ Updated event #{index}: **{ev['name']}** on {new_dt.strftime('%B %d, %Y at %I:%M %p %Z')}.",
        ephemeral=True,
    )


@bot.tree.command(name="setmilestones", description="Set custom milestone days for an event (comma-separated).")
@app_commands.describe(index="The number shown in /listevents", milestones="Example: 90,60,30,7,1,0")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def setmilestones(interaction: discord.Interaction, index: int, milestones: str):
    guild = interaction.guild
    assert guild is not None

    guild_state = get_guild_state(guild.id)
    sort_events(guild_state)
    events = guild_state.get("events", [])

    if not events:
        await interaction.response.send_message("There are no events to update.", ephemeral=True)
        return
    if index < 1 or index > len(events):
        await interaction.response.send_message(f"Index must be between 1 and {len(events)}.", ephemeral=True)
        return

    ms = _parse_milestones(milestones)
    if ms is None:
        await interaction.response.send_message(
            "I couldn't parse that. Use a comma-separated list of non-negative integers, e.g. `90,60,30,7,1,0`.",
            ephemeral=True,
        )
        return

    ev = events[index - 1]
    ev["milestones"] = ms
    # prune announced milestones not in the new set
    ev["announced_milestones"] = [m for m in ev.get("announced_milestones", []) if m in ms]
    save_state()

    await interaction.response.send_message(
        f"✅ Milestones updated for **{ev['name']}**: {', '.join(str(m) for m in ms)}",
        ephemeral=True,
    )


@bot.tree.command(name="resetmilestones", description="Reset an event's milestones back to the default list.")
@app_commands.describe(index="The number shown in /listevents")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def resetmilestones(interaction: discord.Interaction, index: int):
    guild = interaction.guild
    assert guild is not None

    guild_state = get_guild_state(guild.id)
    sort_events(guild_state)
    events = guild_state.get("events", [])

    if not events:
        await interaction.response.send_message("There are no events to update.", ephemeral=True)
        return
    if index < 1 or index > len(events):
        await interaction.response.send_message(f"Index must be between 1 and {len(events)}.", ephemeral=True)
        return

    ev = events[index - 1]
    ev["milestones"] = DEFAULT_MILESTONES.copy()
    ev["announced_milestones"] = []
    save_state()

    await interaction.response.send_message(
        f"✅ Reset milestones for **{ev['name']}** to defaults: {', '.join(str(m) for m in DEFAULT_MILESTONES)}",
        ephemeral=True,
    )


@bot.tree.command(name="archivepast", description="Remove events that have already started/passed.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def archivepast(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    guild_state = get_guild_state(guild.id)
    sort_events(guild_state)
    tz = get_guild_tz(guild_state)

    before = len(guild_state.get("events", []))
    kept = []
    removed = 0
    for ev in guild_state.get("events", []):
        dt = datetime.fromtimestamp(ev["timestamp"], tz=tz)
        _, _, passed = compute_time_left(dt, tz)
        if passed:
            removed += 1
        else:
            kept.append(ev)

    guild_state["events"] = kept
    save_state()

    channel, err = _require_events_channel(guild, guild_state)
    if channel:
        await rebuild_pinned_message(guild.id, channel, guild_state)

    await interaction.response.send_message(
        f"🧹 Archived {removed} past event(s). {len(kept)} event(s) remain.",
        ephemeral=True,
    )


@bot.tree.command(name="resetchannel", description="Clear the configured event channel for this server.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def resetchannel(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    guild_state = get_guild_state(guild.id)
    guild_state["event_channel_id"] = None
    guild_state["pinned_message_id"] = None
    save_state()

    await interaction.response.send_message(
        "✅ Events channel cleared. Run `/seteventchannel` in the channel you want to use.",
        ephemeral=True,
    )


@bot.tree.command(name="healthcheck", description="Check ChronoBot configuration and permissions in the events channel.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def healthcheck(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    guild_state = get_guild_state(guild.id)
    tz = get_guild_tz(guild_state)

    channel, err = _require_events_channel(guild, guild_state)
    if err:
        await interaction.response.send_message(f"⚠️ {err}", ephemeral=True)
        return

    me = guild.me
    perms = channel.permissions_for(me)
    checks = {
        "Send Messages": perms.send_messages,
        "Embed Links": perms.embed_links,
        "Read Message History": perms.read_message_history,
        "Manage Messages (pin/unpin/edit pins)": perms.manage_messages,
        "View Channel": perms.view_channel,
    }

    embed = discord.Embed(title="ChronoBot Healthcheck", color=EMBED_COLOR)
    embed.add_field(name="Events channel", value=channel.mention, inline=False)
    embed.add_field(name="Timezone", value=str(tz), inline=True)
    embed.add_field(name="Events count", value=str(len(guild_state.get("events", []))), inline=True)

    role = get_guild_mention_role(guild, guild_state)
    embed.add_field(name="Mention role", value=(role.mention if role else "None"), inline=True)

    status_lines = []
    for k, ok in checks.items():
        status_lines.append(f"{'✅' if ok else '❌'} {k}")
    embed.add_field(name="Permission checks", value="\n".join(status_lines), inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="settimezone", description="Set this server's timezone (IANA name, e.g. America/Chicago).")
@app_commands.describe(timezone="Example: America/Chicago, America/New_York, Europe/London")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def settimezone(interaction: discord.Interaction, timezone: str):
    guild = interaction.guild
    assert guild is not None

    timezone = timezone.strip()
    try:
        ZoneInfo(timezone)
    except Exception:
        await interaction.response.send_message(
            "I couldn't recognize that timezone. Use an IANA name like `America/Chicago` or `America/New_York`.",
            ephemeral=True,
        )
        return

    guild_state = get_guild_state(guild.id)
    guild_state["timezone"] = timezone
    save_state()

    channel, err = _require_events_channel(guild, guild_state)
    if channel:
        await rebuild_pinned_message(guild.id, channel, guild_state)

    await interaction.response.send_message(
        f"✅ Timezone set to **{timezone}**.",
        ephemeral=True,
    )


@bot.tree.command(name="setmentionrole", description="Set a role to @mention on milestone reminders.")
@app_commands.describe(role="Role to mention on reminders")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def setmentionrole(interaction: discord.Interaction, role: discord.Role):
    guild = interaction.guild
    assert guild is not None

    guild_state = get_guild_state(guild.id)
    guild_state["mention_role_id"] = role.id
    save_state()

    await interaction.response.send_message(
        f"✅ I will mention {role.mention} on milestone reminders.",
        ephemeral=True,
    )


@bot.tree.command(name="clearmentionrole", description="Stop mentioning a role on milestone reminders.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def clearmentionrole(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    guild_state = get_guild_state(guild.id)
    guild_state["mention_role_id"] = None
    save_state()

    await interaction.response.send_message(
        "✅ Mention role cleared. Milestones will no longer ping a role.",
        ephemeral=True,
    )


@bot.tree.command(name="silence", description="Toggle milestone reminders for a specific event (event still stays on the list).")
@app_commands.describe(index="The number shown in /listevents", on_off="True = silence, False = unsilence")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def silence(interaction: discord.Interaction, index: int, on_off: bool):
    guild = interaction.guild
    assert guild is not None

    guild_state = get_guild_state(guild.id)
    sort_events(guild_state)
    events = guild_state.get("events", [])

    if not events:
        await interaction.response.send_message("There are no events to update.", ephemeral=True)
        return
    if index < 1 or index > len(events):
        await interaction.response.send_message(f"Index must be between 1 and {len(events)}.", ephemeral=True)
        return

    ev = events[index - 1]
    ev["silenced"] = bool(on_off)
    save_state()

    await interaction.response.send_message(
        f"🔕 **{ev['name']}** reminders are now {'silenced' if on_off else 'enabled'}.",
        ephemeral=True,
    )


@bot.tree.command(name="testreminder", description="Send a test milestone message for an event (does not change state).")
@app_commands.describe(index="The number shown in /listevents", days_left="Pretend this many days remain (e.g., 30, 7, 1, 0).")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def testreminder(interaction: discord.Interaction, index: int, days_left: int):
    guild = interaction.guild
    assert guild is not None

    guild_state = get_guild_state(guild.id)
    sort_events(guild_state)
    events = guild_state.get("events", [])

    if not events:
        await interaction.response.send_message("There are no events to test.", ephemeral=True)
        return
    if index < 1 or index > len(events):
        await interaction.response.send_message(f"Index must be between 1 and {len(events)}.", ephemeral=True)
        return
    if days_left < 0:
        await interaction.response.send_message("days_left must be 0 or greater.", ephemeral=True)
        return

    channel, err = _require_events_channel(guild, guild_state)
    if err:
        await interaction.response.send_message(f"⚠️ {err}", ephemeral=True)
        return

    ev = events[index - 1]
    text = build_milestone_text(ev["name"], days_left)
    role = get_guild_mention_role(guild, guild_state)
    if role:
        text = f"{role.mention} {text}"

    await channel.send(text)
    await interaction.response.send_message("✅ Test reminder sent to the events channel.", ephemeral=True)


@bot.tree.command(name="purgeevents", description="Delete ALL events for this server (requires typing YES).")
@app_commands.describe(confirm="Type YES to confirm")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def purgeevents(interaction: discord.Interaction, confirm: str):
    guild = interaction.guild
    assert guild is not None

    if confirm.strip().upper() != "YES":
        await interaction.response.send_message("Not confirmed. Type `YES` to purge all events.", ephemeral=True)
        return

    guild_state = get_guild_state(guild.id)
    guild_state["events"] = []
    save_state()

    channel, err = _require_events_channel(guild, guild_state)
    if channel:
        await rebuild_pinned_message(guild.id, channel, guild_state)

    await interaction.response.send_message("🧨 All events have been deleted for this server.", ephemeral=True)


@bot.tree.command(name="searchevents", description="Search events by name.")
@app_commands.describe(query="Text to search for")
@app_commands.guild_only()
async def searchevents(interaction: discord.Interaction, query: str):
    guild = interaction.guild
    assert guild is not None

    query = query.strip().lower()
    guild_state = get_guild_state(guild.id)
    sort_events(guild_state)
    tz = get_guild_tz(guild_state)

    matches = []
    for idx, ev in enumerate(guild_state.get("events", []), start=1):
        if query in ev.get("name", "").lower():
            dt = datetime.fromtimestamp(ev["timestamp"], tz=tz)
            desc, _, passed = compute_time_left(dt, tz)
            status = "✅ done" if passed else f"⏳ {desc}"
            matches.append(f"**{idx}. {ev['name']}** — {dt.strftime('%m/%d/%Y %H:%M')} ({status})")

    if not matches:
        await interaction.response.send_message("No matching events found.", ephemeral=True)
        return

    await interaction.response.send_message("\n".join(matches), ephemeral=True)

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


