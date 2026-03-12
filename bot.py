import os
import re
import json
import asyncio
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

SCORES_FILE = Path(__file__).parent / "scores.json"
RANKED_HISTORY_FILE = Path(__file__).parent / "ranked_history.json"
RANKED_POINTS_FILE = Path(__file__).parent / "ranked_points.json"
STATE_FILE = Path(__file__).parent / "bot_state.json"

# Rank tiers: (name, min_points) — everyone starts in Bronze (50 pts)
RANK_TIERS = [
    ("Bamboo Mountain",  0),
    ("Plastic",         15),
    ("Copper",          30),
    ("Bronze",          50),
    ("Silver",         150),
    ("Gold",           300),
    ("Platinum",       500),
    ("Diamond",        750),
    ("Master",        1000),
    ("Grandmaster",   1300),
    ("Unreal",        1600),
]

RANK_ICONS = {
    "Bamboo Mountain": "🎋", "Plastic": "🥤", "Copper": "🔸",
    "Bronze": "🟤", "Silver": "⚪", "Gold": "🟡",
    "Platinum": "💠", "Diamond": "💎", "Master": "👑",
    "Grandmaster": "🔥", "Unreal": "⚡",
}

STARTING_POINTS = 50
DAILY_FIRST_BONUS = 3
DAILY_LAST_PENALTY = 2
WEEKLY_FIRST = 60
WEEKLY_LAST = -50
MONTHLY_FIRST = 100
MONTHLY_LAST = -80

# --- Score persistence ---


def load_scores() -> dict:
    if SCORES_FILE.exists():
        with open(SCORES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_scores(data: dict) -> None:
    with open(SCORES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# --- Wordle message parser ---

# Matches lines like:  👑 4/6: @ambufire   or   5/6: @Jules Bracke @Stonie   or   X/6: @Griffith
SCORE_LINE_RE = re.compile(
    r"^[👑\s]*([1-6X])/6:\s*(.+)$", re.MULTILINE
)

# Matches Discord mention format: <@123456789>
DISCORD_MENTION_RE = re.compile(r"<@!?(\d+)>")


def parse_wordle_message(
    content: str,
    member_map: dict[int, str] | None = None,
) -> list[tuple[str, int]] | None:
    """Parse a Wordle bot result message.

    Args:
        content: The message text.
        member_map: Optional dict mapping Discord user IDs to display names.
                    Used to resolve <@ID> mentions to readable names.

    Returns a list of (player_name, score) tuples, or None if the message
    is not a Wordle result message.
    """
    if "Here are yesterday's results:" not in content:
        return None

    if member_map is None:
        member_map = {}

    results: list[tuple[str, int]] = []
    for match in SCORE_LINE_RE.finditer(content):
        raw_score, players_str = match.group(1), match.group(2)
        score = 7 if raw_score == "X" else int(raw_score)

        players: list[str] = []

        # 1) Extract Discord <@ID> mentions and resolve to display names
        for id_match in DISCORD_MENTION_RE.finditer(players_str):
            user_id = int(id_match.group(1))
            name = member_map.get(user_id, str(user_id))
            players.append(name)

        # 2) Remove all <@ID> mentions from the string, then parse plain @Name
        remaining = DISCORD_MENTION_RE.sub("", players_str)
        for part in remaining.split("@"):
            name = part.strip().strip("<>").strip()
            if name:
                players.append(name)

        for player in players:
            results.append((player, score))

    return results if results else None


def get_date_key() -> str:
    """Return today's date as YYYY-MM-DD in UTC (used to prevent duplicates)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def record_scores(results: list[tuple[str, int]], date_key: str) -> int:
    """Store parsed scores in JSON. Returns number of new scores added."""
    data = load_scores()
    added = 0

    for player, score in results:
        if player not in data:
            data[player] = {"scores": [], "dates": []}

        # Skip if we already recorded a score for this player on this date
        if date_key in data[player].get("dates", []):
            continue

        data[player]["scores"].append(score)
        data[player].setdefault("dates", []).append(date_key)
        added += 1

    if added > 0:
        save_scores(data)

    return added


def get_filtered_leaderboard(data: dict, days: int) -> list[tuple[str, float, int, float]]:
    """Build a weighted leaderboard from scores within the last N days.

    Weighted score treats missed days as 7 (X/6):
        weighted = (sum_of_scores + missed_days * 7) / total_days

    Returns list of (player, avg, games_played, weighted_score).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    leaderboard = []
    for player, info in data.items():
        scores = info["scores"]
        dates = info.get("dates", [])
        filtered = [s for s, d in zip(scores, dates) if d >= cutoff]
        if filtered:
            avg = sum(filtered) / len(filtered)
            missed = days - len(filtered)
            weighted = (sum(filtered) + missed * 7) / days
            leaderboard.append((player, avg, len(filtered), weighted))
    leaderboard.sort(key=lambda x: x[3])
    return leaderboard


def get_period_leaderboard(
    data: dict, start_date: str, end_date: str, total_days: int
) -> list[tuple[str, float, int, float]]:
    """Build a leaderboard for a specific date range [start_date, end_date]."""
    leaderboard = []
    for player, info in data.items():
        scores = info["scores"]
        dates = info.get("dates", [])
        filtered = [s for s, d in zip(scores, dates) if start_date <= d <= end_date]
        if filtered:
            avg = sum(filtered) / len(filtered)
            missed = max(0, total_days - len(filtered))
            weighted = (sum(filtered) + missed * 7) / total_days
            leaderboard.append((player, avg, len(filtered), weighted))
    leaderboard.sort(key=lambda x: x[3])
    return leaderboard


# --- Ranked points ---


def load_ranked_points() -> dict:
    if RANKED_POINTS_FILE.exists():
        with open(RANKED_POINTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_ranked_points(data: dict) -> None:
    with open(RANKED_POINTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_rank_name(points: int) -> str:
    """Return the rank tier name for a point total."""
    rank = "Copper"
    for name, threshold in RANK_TIERS:
        if points >= threshold:
            rank = name
        else:
            break
    return rank


def ensure_player_rp(rp: dict, player: str) -> None:
    if player not in rp:
        rp[player] = {"points": STARTING_POINTS, "daily_dates": []}


def apply_points(rp: dict, player: str, change: int) -> None:
    ensure_player_rp(rp, player)
    rp[player]["points"] = max(0, rp[player]["points"] + change)


def award_daily_points(rp: dict, results: list[tuple[str, int]], date_key: str) -> None:
    """Award daily points: first place +3, last place -2."""
    if not results or len(results) < 2:
        return
    for player, _ in results:
        ensure_player_rp(rp, player)
        if date_key in rp[player].get("daily_dates", []):
            return  # Already processed this day
    sorted_r = sorted(results, key=lambda x: x[1])
    best = sorted_r[0][1]
    worst = sorted_r[-1][1]
    for player, score in sorted_r:
        if score == best:
            apply_points(rp, player, DAILY_FIRST_BONUS)
        elif score == worst and best != worst:
            apply_points(rp, player, -DAILY_LAST_PENALTY)
        rp[player].setdefault("daily_dates", []).append(date_key)


def award_period_points(rp: dict, leaderboard: list, first_pts: int, last_pts: int) -> None:
    """Award end-of-period points using linear interpolation from first to last."""
    n = len(leaderboard)
    if n < 1:
        return
    if n == 1:
        apply_points(rp, leaderboard[0][0], first_pts)
        return
    span = first_pts - last_pts
    for i, (player, _avg, _games, _w) in enumerate(leaderboard):
        change = round(first_pts - i * span / (n - 1))
        apply_points(rp, player, change)


def recalculate_all_ranked_points(scores_data: dict) -> None:
    """Recalculate all ranked points from scratch (daily + weekly + monthly)."""
    rp = {}
    for player in scores_data:
        rp[player] = {"points": STARTING_POINTS, "daily_dates": []}

    all_dates = set()
    for info in scores_data.values():
        all_dates.update(info.get("dates", []))
    if not all_dates:
        save_ranked_points(rp)
        return

    sorted_dates = sorted(all_dates)
    today = datetime.now(timezone.utc).date()

    # Daily points for each historical day
    for date_str in sorted_dates:
        daily = []
        for player, info in scores_data.items():
            for d, s in zip(info.get("dates", []), info["scores"]):
                if d == date_str:
                    daily.append((player, s))
                    break
        award_daily_points(rp, daily, date_str)

    # Weekly points for each completed week (Mon-Fri)
    min_d = date.fromisoformat(sorted_dates[0])
    mon = min_d - timedelta(days=min_d.weekday())
    while mon + timedelta(days=4) < today:
        fri = mon + timedelta(days=4)
        lb = get_period_leaderboard(scores_data, mon.isoformat(), fri.isoformat(), 5)
        if lb:
            award_period_points(rp, lb, WEEKLY_FIRST, WEEKLY_LAST)
        mon += timedelta(days=7)

    # Monthly points for each completed month
    first = date(min_d.year, min_d.month, 1)
    while True:
        if first.month == 12:
            nxt = date(first.year + 1, 1, 1)
        else:
            nxt = date(first.year, first.month + 1, 1)
        last = nxt - timedelta(days=1)
        if last >= today:
            break
        total = (last - first).days + 1
        lb = get_period_leaderboard(scores_data, first.isoformat(), last.isoformat(), total)
        if lb:
            award_period_points(rp, lb, MONTHLY_FIRST, MONTHLY_LAST)
        first = nxt

    save_ranked_points(rp)


def rebuild_ranked_history(scores_data: dict) -> None:
    """Rebuild the entire ranked history from score data."""
    history = {"weekly": {}, "monthly": {}}
    all_dates = set()
    for info in scores_data.values():
        all_dates.update(info.get("dates", []))
    if not all_dates:
        save_ranked_history(history)
        return

    today = datetime.now(timezone.utc).date()
    min_d = date.fromisoformat(min(all_dates))

    # All completed weeks (Mon-Fri)
    mon = min_d - timedelta(days=min_d.weekday())
    while mon + timedelta(days=4) < today:
        fri = mon + timedelta(days=4)
        week_key = f"{mon.isocalendar()[0]}-W{mon.isocalendar()[1]:02d}"
        lb = get_period_leaderboard(scores_data, mon.isoformat(), fri.isoformat(), 5)
        if lb:
            standings = [{"rank": r, "player": p, "avg": round(a, 2), "games": g, "weighted": round(w, 2)}
                         for r, (p, a, g, w) in enumerate(lb, 1)]
            history["weekly"][week_key] = {"start": mon.isoformat(), "end": fri.isoformat(), "standings": standings}
        mon += timedelta(days=7)

    # All completed months
    first = date(min_d.year, min_d.month, 1)
    while True:
        if first.month == 12:
            nxt = date(first.year + 1, 1, 1)
        else:
            nxt = date(first.year, first.month + 1, 1)
        last = nxt - timedelta(days=1)
        if last >= today:
            break
        total = (last - first).days + 1
        month_key = first.strftime("%Y-%m")
        lb = get_period_leaderboard(scores_data, first.isoformat(), last.isoformat(), total)
        if lb:
            standings = [{"rank": r, "player": p, "avg": round(a, 2), "games": g, "weighted": round(w, 2)}
                         for r, (p, a, g, w) in enumerate(lb, 1)]
            history["monthly"][month_key] = {"start": first.isoformat(), "end": last.isoformat(), "standings": standings}
        first = nxt

    save_ranked_history(history)


# --- Ranked mode helpers ---


def load_ranked_history() -> dict:
    if RANKED_HISTORY_FILE.exists():
        with open(RANKED_HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"weekly": {}, "monthly": {}}


def save_ranked_history(data: dict) -> None:
    with open(RANKED_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_current_week_range() -> tuple[str, str, int]:
    """Return (start_date, end_date, days_elapsed) for the current ranked week (Mon-Fri)."""
    today = datetime.now(timezone.utc).date()
    monday = today - timedelta(days=today.weekday())  # Monday of this week
    friday = monday + timedelta(days=4)
    days_elapsed = min((today - monday).days + 1, 5)  # 1 on Monday, 5 on Friday
    return monday.isoformat(), friday.isoformat(), days_elapsed


def get_current_month_range() -> tuple[str, str, int]:
    """Return (start_date, end_date, days_elapsed) for the current month."""
    today = datetime.now(timezone.utc).date()
    first_day = today.replace(day=1)
    # Last day of month
    if today.month == 12:
        last_day = today.replace(year=today.year + 1, month=1, day=1) - timedelta(days=1)
    else:
        last_day = today.replace(month=today.month + 1, day=1) - timedelta(days=1)
    days_elapsed = (today - first_day).days + 1
    total_days = (last_day - first_day).days + 1
    return first_day.isoformat(), last_day.isoformat(), days_elapsed, total_days


def get_ranked_leaderboard(
    data: dict, start_date: str, days_elapsed: int
) -> list[tuple[str, float, int, float]]:
    """Build a ranked leaderboard for a specific date range.

    Only counts games from start_date onward (up to today).
    Weighted score penalizes missed days within the elapsed period.

    Returns list of (player, avg, games_played, weighted_score).
    """
    leaderboard = []
    for player, info in data.items():
        scores = info["scores"]
        dates = info.get("dates", [])
        filtered = [s for s, d in zip(scores, dates) if d >= start_date]
        if filtered:
            avg = sum(filtered) / len(filtered)
            missed = max(0, days_elapsed - len(filtered))
            weighted = (sum(filtered) + missed * 7) / days_elapsed
            leaderboard.append((player, avg, len(filtered), weighted))
    leaderboard.sort(key=lambda x: x[3])
    return leaderboard


def archive_completed_periods(scores_data: dict) -> None:
    """Check for and archive the previous week/month. Awards ranked points for new archives."""
    history = load_ranked_history()
    rp_data = load_ranked_points()
    today = datetime.now(timezone.utc).date()
    points_changed = False

    # Archive previous week (Mon-Fri)
    last_monday = today - timedelta(days=today.weekday() + 7)
    last_friday = last_monday + timedelta(days=4)
    week_key = f"{last_monday.isocalendar()[0]}-W{last_monday.isocalendar()[1]:02d}"

    if week_key not in history["weekly"]:
        lb = get_period_leaderboard(scores_data, last_monday.isoformat(), last_friday.isoformat(), 5)
        if lb:
            standings = [{"rank": r, "player": p, "avg": round(a, 2), "games": g, "weighted": round(w, 2)}
                         for r, (p, a, g, w) in enumerate(lb, 1)]
            history["weekly"][week_key] = {
                "start": last_monday.isoformat(),
                "end": last_friday.isoformat(),
                "standings": standings
            }
            award_period_points(rp_data, lb, WEEKLY_FIRST, WEEKLY_LAST)
            points_changed = True

    # Archive previous month
    if today.month == 1:
        prev_first = today.replace(year=today.year - 1, month=12, day=1)
    else:
        prev_first = today.replace(month=today.month - 1, day=1)
    prev_last = today.replace(day=1) - timedelta(days=1)
    month_key = prev_first.strftime("%Y-%m")
    total_days_prev = (prev_last - prev_first).days + 1

    if month_key not in history["monthly"]:
        lb = get_period_leaderboard(scores_data, prev_first.isoformat(), prev_last.isoformat(), total_days_prev)
        if lb:
            standings = [{"rank": r, "player": p, "avg": round(a, 2), "games": g, "weighted": round(w, 2)}
                         for r, (p, a, g, w) in enumerate(lb, 1)]
            history["monthly"][month_key] = {
                "start": prev_first.isoformat(),
                "end": prev_last.isoformat(),
                "standings": standings
            }
            award_period_points(rp_data, lb, MONTHLY_FIRST, MONTHLY_LAST)
            points_changed = True

    save_ranked_history(history)
    if points_changed:
        save_ranked_points(rp_data)


# --- Bot state (for catch-up after downtime) ---


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(data: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# --- Bot setup ---

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Wordle Tracker is online as {bot.user}")

    # --- Catch-up: process any Wordle messages we missed while offline ---
    state = load_state()
    channel_id = state.get("channel_id")
    last_message_id = state.get("last_message_id")

    if not channel_id or not last_message_id:
        print("No previous state found — skipping catch-up. Run !scan to backfill.")
        return

    channel = bot.get_channel(channel_id)
    if not channel:
        print(f"Could not find channel {channel_id} for catch-up.")
        return

    print(f"Catching up on missed messages in #{channel.name}...")
    member_map = {}
    if channel.guild:
        member_map = {m.id: m.display_name for m in channel.guild.members}

    caught_up = 0
    new_last_id = last_message_id

    try:
        async for message in channel.history(limit=500, after=discord.Object(id=last_message_id), oldest_first=True):
            new_last_id = message.id
            results = parse_wordle_message(message.content, member_map)
            if results:
                date_key = message.created_at.strftime("%Y-%m-%d")
                added = record_scores(results, date_key)
                if added > 0:
                    rp_data = load_ranked_points()
                    award_daily_points(rp_data, results, date_key)
                    save_ranked_points(rp_data)
                    caught_up += added
    except Exception as e:
        print(f"Catch-up error: {e}")

    if caught_up > 0:
        archive_completed_periods(load_scores())
        print(f"Catch-up complete! Added {caught_up} missed score(s).")
    else:
        print("No missed Wordle messages found.")

    save_state({"channel_id": channel_id, "last_message_id": new_last_id})


@bot.event
async def on_message(message: discord.Message):
    # Don't respond to ourselves
    if message.author == bot.user:
        return

    # Build a member map for resolving <@ID> mentions to display names
    member_map = {}
    if message.guild:
        member_map = {m.id: m.display_name for m in message.guild.members}

    # Try to parse Wordle results from any bot message (or any message matching the format)
    results = parse_wordle_message(message.content, member_map)
    if results:
        date_key = get_date_key()
        added = record_scores(results, date_key)
        if added > 0:
            # Award daily ranked points
            rp_data = load_ranked_points()
            award_daily_points(rp_data, results, date_key)
            save_ranked_points(rp_data)
            # Archive completed periods (also awards period points)
            archive_completed_periods(load_scores())
            await message.channel.send(
                f"✅ Recorded {added} Wordle score(s) for today!"
            )
        else:
            await message.channel.send("ℹ️ These scores were already recorded.")

        # Track this channel + message for catch-up after restarts
        save_state({"channel_id": message.channel.id, "last_message_id": message.id})

    # Ensure prefix commands still work
    await bot.process_commands(message)


# --- Commands ---


@bot.command(name="wordle")
async def wordle_leaderboard(ctx: commands.Context, *, arg: str = ""):
    """Show the Wordle leaderboard or a specific player's stats.

    Usage:
        !wordle          – Show all-time leaderboard
        !wordle weekly   – Show last 7 days leaderboard
        !wordle monthly  – Show last 30 days leaderboard
        !wordle @user    – Show stats for a player
        !wordle reset    – Admin only: clear all data
    """
    arg = arg.strip()

    # --- Help ---
    if arg.lower() == "help":
        embed = discord.Embed(
            title="📖 Wordle Bot — Commands",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="📊 Leaderboard",
            value=(
                "`!wordle` — All-time leaderboard\n"
                "`!wordle weekly` — Last 7 days\n"
                "`!wordle monthly` — Last 30 days\n"
                "`!wordle @user` — Player stats"
            ),
            inline=False,
        )
        embed.add_field(
            name="🏅 Ranked (fresh each period)",
            value=(
                "`!ranked` — Current week standings\n"
                "`!ranked monthly` — Current month standings\n"
                "`!ranked history` — Past weekly winners\n"
                "`!ranked history monthly` — Past monthly winners"
            ),
            inline=False,
        )
        embed.add_field(
            name="🎖️ Rank Tiers",
            value=(
                "`!rank` — All players' rank & points\n"
                "`!rank @user` — Player rank details\n"
                "Tiers: 🎋Bamboo Mountain → 🥤Plastic → 🔸Copper → 🟤Bronze → ⚪Silver → 🟡Gold → 💠Platinum → 💎Diamond → 👑Master → 🔥Grandmaster → ⚡Unreal"
            ),
            inline=False,
        )
        embed.add_field(
            name="⚙️ Admin",
            value=(
                "`!scan` — Re-scan channel history\n"
                "`!wordle reset` — Clear all scores (admin only)\n"
                "`!rank reset` — Reset all ranks to Bronze (admin only)"
            ),
            inline=False,
        )
        embed.set_footer(text="X/6 counts as 7 · Missed days count as 7 · Lower is better")
        await ctx.send(embed=embed)
        return

    # --- Weekly / Monthly leaderboard ---
    if arg.lower() in ("weekly", "week"):
        data = load_scores()
        if not data:
            await ctx.send("No Wordle scores recorded yet!")
            return
        leaderboard = get_filtered_leaderboard(data, days=7)
        if not leaderboard:
            await ctx.send("No scores in the last 7 days!")
            return
        rank_emojis = {1: "\U0001f947", 2: "\U0001f948", 3: "\U0001f949"}
        lines = []
        for i, (player, avg, games, weighted) in enumerate(leaderboard, start=1):
            rank = rank_emojis.get(i, f"**{i}.**")
            lines.append(f"{rank} **{player}** \u2014 {weighted:.2f} weighted \u00b7 {avg:.2f} avg \u00b7 {games}/5 games")
        embed = discord.Embed(
            title="\U0001f4c5 Wordle Leaderboard \u2014 Last 7 Days",
            description="\n".join(lines),
            color=discord.Color.green(),
        )
        embed.set_footer(text="Ranked by weighted score (missed days count as 7) \u00b7 Lower is better")
        await ctx.send(embed=embed)
        return

    if arg.lower() in ("monthly", "month"):
        data = load_scores()
        if not data:
            await ctx.send("No Wordle scores recorded yet!")
            return
        leaderboard = get_filtered_leaderboard(data, days=30)
        if not leaderboard:
            await ctx.send("No scores in the last 30 days!")
            return
        rank_emojis = {1: "\U0001f947", 2: "\U0001f948", 3: "\U0001f949"}
        lines = []
        for i, (player, avg, games, weighted) in enumerate(leaderboard, start=1):
            rank = rank_emojis.get(i, f"**{i}.**")
            lines.append(f"{rank} **{player}** \u2014 {weighted:.2f} weighted \u00b7 {avg:.2f} avg \u00b7 {games}/30 games")
        embed = discord.Embed(
            title="\U0001f4c6 Wordle Leaderboard \u2014 Last 30 Days",
            description="\n".join(lines),
            color=discord.Color.orange(),
        )
        embed.set_footer(text="Ranked by weighted score (missed days count as 7) \u00b7 Lower is better")
        await ctx.send(embed=embed)
        return

    # --- Admin reset ---
    if arg.lower() == "reset":
        if not ctx.author.guild_permissions.administrator:
            await ctx.send("❌ Only admins can reset scores.")
            return
        save_scores({})
        await ctx.send("🗑️ All Wordle scores have been reset.")
        return

    data = load_scores()

    if not data:
        await ctx.send("No Wordle scores recorded yet!")
        return

    # --- Single player stats ---
    if arg:
        # Strip @ if present
        player_name = arg.lstrip("@").strip()

        # Try to find a case-insensitive match
        match_key = None
        for key in data:
            if key.lower() == player_name.lower():
                match_key = key
                break

        if not match_key:
            await ctx.send(f"No scores found for **{player_name}**.")
            return

        scores = data[match_key]["scores"]
        avg = sum(scores) / len(scores)
        best = min(scores)
        worst = max(scores)

        embed = discord.Embed(
            title=f"📊 Wordle Stats — {match_key}",
            color=discord.Color.blue(),
        )
        embed.add_field(name="Average", value=f"**{avg:.2f}**/6", inline=True)
        embed.add_field(name="Best", value=f"**{best}**/6", inline=True)
        embed.add_field(name="Worst", value=f"**{worst}**/6" if worst <= 6 else "**X**/6", inline=True)
        embed.add_field(name="Games", value=str(len(scores)), inline=True)
        await ctx.send(embed=embed)
        return

    # --- Leaderboard ---
    leaderboard = []
    for player, info in data.items():
        scores = info["scores"]
        avg = sum(scores) / len(scores)
        leaderboard.append((player, avg, len(scores)))

    # Sort by average (lowest = best)
    leaderboard.sort(key=lambda x: x[1])

    rank_emojis = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = []
    for i, (player, avg, games) in enumerate(leaderboard, start=1):
        rank = rank_emojis.get(i, f"**{i}.**")
        lines.append(f"{rank} **{player}** — {avg:.2f} avg ({games} games)")

    embed = discord.Embed(
        title="🏆 Wordle Leaderboard",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )
    embed.set_footer(text="Lower average is better · X/6 counts as 7")
    await ctx.send(embed=embed)


@bot.command(name="ranked")
async def wordle_ranked(ctx: commands.Context, *, arg: str = ""):
    """Show the ranked leaderboard for the current week or month.

    Each period is a fresh competition. Missed days count as 7.
    Completed periods are automatically archived to ranked_history.json.

    Usage:
        !ranked              – Current week's ranked standings
        !ranked monthly      – Current month's ranked standings
        !ranked history      – Show past weekly winners
        !ranked history monthly – Show past monthly winners
    """
    arg = arg.strip().lower()
    data = load_scores()

    # Auto-archive any completed periods
    if data:
        archive_completed_periods(data)

    # --- History ---
    if arg.startswith("history"):
        history = load_ranked_history()
        period = "monthly" if "monthly" in arg or "month" in arg else "weekly"
        records = history.get(period, {})

        if not records:
            await ctx.send(f"No archived {period} results yet!")
            return

        lines = []
        for key in sorted(records.keys(), reverse=True)[:10]:  # Last 10
            entry = records[key]
            standings = entry["standings"]
            if standings:
                winner = standings[0]
                lines.append(
                    f"**{key}** — 🥇 **{winner['player']}** "
                    f"({winner['weighted']:.2f} weighted · {winner['avg']:.2f} avg · {winner['games']} games)"
                )

        title = "📜 Ranked History — Weekly" if period == "weekly" else "📜 Ranked History — Monthly"
        embed = discord.Embed(
            title=title,
            description="\n".join(lines) if lines else "No records.",
            color=discord.Color.purple(),
        )
        await ctx.send(embed=embed)
        return

    # --- Monthly ranked ---
    if arg in ("monthly", "month"):
        if not data:
            await ctx.send("No Wordle scores recorded yet!")
            return
        start_date, end_date, days_elapsed, total_days = get_current_month_range()
        month_label = datetime.now(timezone.utc).strftime("%B %Y")
        leaderboard = get_ranked_leaderboard(data, start_date, days_elapsed)

        if not leaderboard:
            await ctx.send(f"No scores yet for {month_label}!")
            return

        rank_emojis = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = []
        for i, (player, avg, games, weighted) in enumerate(leaderboard, start=1):
            rank = rank_emojis.get(i, f"**{i}.**")
            lines.append(
                f"{rank} **{player}** — {weighted:.2f} weighted · "
                f"{avg:.2f} avg · {games}/{days_elapsed} games"
            )

        embed = discord.Embed(
            title=f"🏅 Ranked — {month_label} (Day {days_elapsed}/{total_days})",
            description="\n".join(lines),
            color=discord.Color.orange(),
        )
        embed.set_footer(text="Fresh competition each month · Missed days count as 7 · Lower is better")
        await ctx.send(embed=embed)
        return

    # --- Weekly ranked (default) ---
    if not data:
        await ctx.send("No Wordle scores recorded yet!")
        return

    start_date, end_date, days_elapsed = get_current_week_range()
    iso_cal = datetime.now(timezone.utc).date().isocalendar()
    week_label = f"Week {iso_cal[1]}, {iso_cal[0]}"
    leaderboard = get_ranked_leaderboard(data, start_date, days_elapsed)

    if not leaderboard:
        await ctx.send(f"No scores yet for {week_label}!")
        return

    rank_emojis = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = []
    for i, (player, avg, games, weighted) in enumerate(leaderboard, start=1):
        rank = rank_emojis.get(i, f"**{i}.**")
        lines.append(
            f"{rank} **{player}** — {weighted:.2f} weighted · "
            f"{avg:.2f} avg · {games}/{days_elapsed} games"
        )

    embed = discord.Embed(
        title=f"🏅 Ranked — {week_label} (Day {days_elapsed}/5)",
        description="\n".join(lines),
        color=discord.Color.teal(),
    )
    embed.set_footer(text="Fresh competition Mon–Fri · Missed days count as 7 · Lower is better")
    await ctx.send(embed=embed)


@bot.command(name="rank")
async def wordle_rank(ctx: commands.Context, *, arg: str = ""):
    """Show ranked standings with tier badges.

    Usage:
        !rank         – Show all players' ranks sorted by points
        !rank @user   – Show a specific player's rank details
    """
    rp_data = load_ranked_points()
    arg = arg.strip()

    # --- Rank reset ---
    if arg.lower() == "reset":
        if not ctx.author.guild_permissions.administrator:
            await ctx.send("❌ Only admins can reset ranks.")
            return
        scores = load_scores()
        rp_data = {}
        for player in scores:
            rp_data[player] = {"points": STARTING_POINTS, "daily_dates": []}
        save_ranked_points(rp_data)
        await ctx.send("🔄 All ranks have been reset! Everyone starts at 🟤 Bronze.")
        return

    if not rp_data:
        await ctx.send("No ranked data yet! Use `!scan` to load historical scores first.")
        return

    if arg:
        player_name = arg.lstrip("@").strip()
        match_key = None
        for key in rp_data:
            if key.lower() == player_name.lower():
                match_key = key
                break
        if not match_key:
            await ctx.send(f"No rank data for **{player_name}**.")
            return

        pts = rp_data[match_key]["points"]
        rank_name = get_rank_name(pts)
        icon = RANK_ICONS[rank_name]

        next_rank_info = None
        for name, threshold in RANK_TIERS:
            if threshold > pts:
                next_rank_info = (name, threshold)
                break

        embed = discord.Embed(title=f"{icon} {match_key}", color=discord.Color.blue())
        embed.add_field(name="Rank", value=f"{icon} {rank_name}", inline=True)
        embed.add_field(name="Points", value=str(pts), inline=True)
        if next_rank_info:
            n_icon = RANK_ICONS[next_rank_info[0]]
            needed = next_rank_info[1] - pts
            embed.add_field(name="Next", value=f"{n_icon} {next_rank_info[0]} ({needed} pts away)", inline=True)
        else:
            embed.add_field(name="Next", value="**MAX RANK** ⚡", inline=True)
        await ctx.send(embed=embed)
        return

    sorted_players = sorted(rp_data.items(), key=lambda x: x[1]["points"], reverse=True)
    lines = []
    for i, (player, info) in enumerate(sorted_players, start=1):
        pts = info["points"]
        rank_name = get_rank_name(pts)
        icon = RANK_ICONS[rank_name]
        lines.append(f"**{i}.** {icon} **{player}** — {rank_name} ({pts} pts)")

    embed = discord.Embed(
        title="🏅 Ranked Standings",
        description="\n".join(lines),
        color=discord.Color.dark_gold(),
    )
    embed.set_footer(text="Daily: 1st +3 / last -2 · Weekly/Monthly: position-based points")
    await ctx.send(embed=embed)


@bot.command(name="scan")
async def wordle_scan(ctx: commands.Context):
    """Scan the channel history for past Wordle bot messages and backfill scores.

    Usage: !scan
    """
    # Clear existing scores so we get a clean re-scan
    save_scores({})
    await ctx.send("🔍 Scanning channel history for Wordle results... This may take a moment.")

    # Build member map for resolving <@ID> mentions
    member_map = {}
    if ctx.guild:
        member_map = {m.id: m.display_name for m in ctx.guild.members}

    total_added = 0
    messages_found = 0
    batch_count = 0

    async for message in ctx.channel.history(limit=None, oldest_first=True):
        batch_count += 1
        # Pause briefly every 50 messages to avoid rate limits
        if batch_count % 50 == 0:
            await asyncio.sleep(1)

        results = parse_wordle_message(message.content, member_map)
        if results:
            messages_found += 1
            # Use the message's timestamp as the date key
            date_key = message.created_at.strftime("%Y-%m-%d")
            added = record_scores(results, date_key)
            total_added += added

    # Recalculate ranked points from all historical data
    all_scores = load_scores()
    if all_scores:
        recalculate_all_ranked_points(all_scores)
        rebuild_ranked_history(all_scores)

    # Save state so catch-up works after restart
    # Get the latest message in the channel to use as checkpoint
    last_msg = [m async for m in ctx.channel.history(limit=1)]
    if last_msg:
        save_state({"channel_id": ctx.channel.id, "last_message_id": last_msg[0].id})

    if total_added > 0:
        await ctx.send(
            f"✅ Scan complete! Found **{messages_found}** Wordle result messages "
            f"and added **{total_added}** new score(s)."
        )
    elif messages_found > 0:
        await ctx.send(
            f"ℹ️ Found **{messages_found}** Wordle result messages, "
            f"but all scores were already recorded."
        )
    else:
        await ctx.send("❌ No Wordle result messages found in this channel.")


# --- Run ---

token = os.getenv("DISCORD_TOKEN")
if not token:
    raise SystemExit("Error: DISCORD_TOKEN not found. Create a .env file with your bot token.")

bot.run(token)
