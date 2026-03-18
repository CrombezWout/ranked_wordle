import os
import re
import json
import asyncio
import traceback
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

# The Wordle bot's user ID — only process messages from this bot
WORDLE_BOT_ID = 1211781489931452447

# --- Score persistence ---


def _atomic_json_write(path: Path, data: dict) -> None:
    """Write JSON atomically to avoid partial/corrupted files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)


def ensure_storage_files() -> None:
    """Create missing JSON storage files with safe defaults."""
    defaults = {
        SCORES_FILE: {},
        RANKED_POINTS_FILE: {},
        RANKED_HISTORY_FILE: {"weekly": {}, "monthly": {}},
        STATE_FILE: {},
    }
    for path, default_value in defaults.items():
        if not path.exists():
            _atomic_json_write(path, default_value)


def verify_storage_writable() -> tuple[bool, str]:
    """Return whether bot storage files are writable by current process."""
    try:
        ensure_storage_files()
        for p in (SCORES_FILE, RANKED_POINTS_FILE, RANKED_HISTORY_FILE, STATE_FILE):
            with open(p, "a", encoding="utf-8"):
                pass
        return True, "Storage files are writable."
    except Exception as e:
        return False, f"Storage not writable: {e}"


def load_scores() -> dict:
    if SCORES_FILE.exists():
        with open(SCORES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_scores(data: dict) -> None:
    _atomic_json_write(SCORES_FILE, data)


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


def get_date_key(base_dt: datetime | None = None) -> str:
    """Return date key (YYYY-MM-DD) for Wordle daily result posts.

    The Wordle bot posts "yesterday's results", often in the morning after.
    We therefore store scores under (message_date - 1 day).
    """
    if base_dt is None:
        base_dt = datetime.now(timezone.utc)
    return (base_dt - timedelta(days=1)).strftime("%Y-%m-%d")


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


def shift_all_score_dates(days_delta: int) -> tuple[int, int]:
    """Shift all stored score dates by days_delta and deduplicate per player/day.

    Returns (shifted_entries, deduped_entries).
    """
    data = load_scores()
    shifted_entries = 0
    deduped_entries = 0

    for player, info in data.items():
        pairs = sorted(
            zip(info.get("dates", []), info.get("scores", [])),
            key=lambda x: x[0],
        )

        new_dates: list[str] = []
        new_scores: list[int] = []
        seen_dates: set[str] = set()

        for old_date, score in pairs:
            new_date = (date.fromisoformat(old_date) + timedelta(days=days_delta)).isoformat()
            if new_date in seen_dates:
                deduped_entries += 1
                continue
            seen_dates.add(new_date)
            new_dates.append(new_date)
            new_scores.append(score)
            if new_date != old_date:
                shifted_entries += 1

        info["dates"] = new_dates
        info["scores"] = new_scores

    save_scores(data)
    if data:
        recalculate_all_ranked_points(data)
        rebuild_ranked_history(data)

    return shifted_entries, deduped_entries


def get_filtered_leaderboard(data: dict, days: int) -> list[tuple[str, float, int, float]]:
    """Build a weighted leaderboard from scores within the last N days.

    Weighted score treats missed days as 7 (X/6):
        weighted = (sum_of_scores + missed_days * 7) / total_days

    Returns list of (player, avg, games_played, weighted_score).
    """
    # Inclusive rolling window: last N calendar days including today.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    leaderboard = []
    for player, info in data.items():
        scores = info["scores"]
        dates = info.get("dates", [])
        filtered = [s for s, d in zip(scores, dates) if d >= cutoff]
        if filtered:
            avg = sum(filtered) / len(filtered)
            missed = max(0, days - len(filtered))
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
    _atomic_json_write(RANKED_POINTS_FILE, data)


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
            daily_results = build_daily_results(scores_data, mon.isoformat(), fri.isoformat())
            history["weekly"][week_key] = {
                "start": mon.isoformat(),
                "end": fri.isoformat(),
                "standings": standings,
                "daily_results": daily_results,
            }
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

    assign_week_numbers(history)
    save_ranked_history(history)


# --- Ranked mode helpers ---


def load_ranked_history() -> dict:
    if RANKED_HISTORY_FILE.exists():
        with open(RANKED_HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"weekly": {}, "monthly": {}}


def save_ranked_history(data: dict) -> None:
    _atomic_json_write(RANKED_HISTORY_FILE, data)

def assign_week_numbers(history: dict) -> None:
    """Assign sequential labels to weekly history: Week 1, Week 2, ..."""
    weekly = history.setdefault("weekly", {})
    ordered = sorted(weekly.items(), key=lambda kv: kv[1].get("start", ""))
    for idx, (_key, entry) in enumerate(ordered, start=1):
        entry["week_number"] = idx


def get_current_week_range() -> tuple[str, str, int]:
    """Return (start_date, end_date, days_elapsed) for current ranked week (Mon-Fri).

    Wordle results are posted the day after, so days_elapsed is based on the
    latest available result day (today - 1 day).
    """
    today = datetime.now(timezone.utc).date()
    available_day = today - timedelta(days=1)
    monday = today - timedelta(days=today.weekday())  # Monday of this week
    friday = monday + timedelta(days=4)

    if available_day < monday:
        days_elapsed = 0
    else:
        effective_end = min(available_day, friday)
        days_elapsed = (effective_end - monday).days + 1

    return monday.isoformat(), friday.isoformat(), days_elapsed


def get_current_month_range() -> tuple[str, str, int, int]:
    """Return (start_date, end_date, days_elapsed, total_days) for current month.

    days_elapsed is based on the latest available result day (today - 1 day).
    """
    today = datetime.now(timezone.utc).date()
    available_day = today - timedelta(days=1)
    first_day = today.replace(day=1)
    # Last day of month
    if today.month == 12:
        last_day = today.replace(year=today.year + 1, month=1, day=1) - timedelta(days=1)
    else:
        last_day = today.replace(month=today.month + 1, day=1) - timedelta(days=1)

    if available_day < first_day:
        days_elapsed = 0
    else:
        days_elapsed = (min(available_day, last_day) - first_day).days + 1

    total_days = (last_day - first_day).days + 1
    return first_day.isoformat(), last_day.isoformat(), days_elapsed, total_days


def get_ranked_leaderboard(
    data: dict, start_date: str, days_elapsed: int, end_date: str | None = None
) -> list[tuple[str, float, int, float]]:
    """Build a ranked leaderboard for a specific date range.

    Only counts games from start_date onward (up to today).
    Weighted score penalizes missed days within the elapsed period.

    Returns list of (player, avg, games_played, weighted_score).
    """
    if days_elapsed <= 0:
        return []

    leaderboard = []
    if end_date is None:
        # Latest fully available result day (Wordle posts yesterday's results).
        end_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    for player, info in data.items():
        scores = info["scores"]
        dates = info.get("dates", [])
        filtered = [s for s, d in zip(scores, dates) if start_date <= d <= end_date]
        if filtered:
            avg = sum(filtered) / len(filtered)
            missed = max(0, days_elapsed - len(filtered))
            weighted = (sum(filtered) + missed * 7) / days_elapsed
            leaderboard.append((player, avg, len(filtered), weighted))
    leaderboard.sort(key=lambda x: x[3])
    return leaderboard


def build_daily_results(scores_data: dict, start_date: str, end_date: str) -> list[dict]:
    """Build per-day score rows for a period from start_date to end_date."""
    daily_rows = []
    current = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)

    while current <= end:
        day_key = current.isoformat()
        scores = []
        for player, info in scores_data.items():
            for d, s in zip(info.get("dates", []), info.get("scores", [])):
                if d == day_key:
                    scores.append({"player": player, "score": s})
                    break
        if scores:
            scores.sort(key=lambda x: x["score"])
            daily_rows.append({"date": day_key, "scores": scores})
        current += timedelta(days=1)

    return daily_rows


def archive_completed_periods(scores_data: dict, rank_points_cutoff: str | None = None) -> list[dict]:
    """Archive ALL completed but un-archived weeks/months.

    Scans from the earliest recorded score date to today, so even if the bot
    was offline for weeks, no period is ever lost.

    Returns a list of announcement dicts with point change details.
    """
    history = load_ranked_history()
    rp_data = load_ranked_points()
    today = datetime.now(timezone.utc).date()
    announcements = []

    # Find the earliest date in scores
    all_dates = set()
    for info in scores_data.values():
        all_dates.update(info.get("dates", []))
    if not all_dates:
        return announcements

    min_d = date.fromisoformat(min(all_dates))

    # --- Archive ALL completed weeks (Mon-Fri) ---
    mon = min_d - timedelta(days=min_d.weekday())
    while mon + timedelta(days=4) < today:
        fri = mon + timedelta(days=4)
        week_key = f"{mon.isocalendar()[0]}-W{mon.isocalendar()[1]:02d}"
        if week_key not in history.setdefault("weekly", {}):
            lb = get_period_leaderboard(scores_data, mon.isoformat(), fri.isoformat(), 5)
            if lb:
                changes = []
                standings = []
                can_award = rank_points_cutoff is None or fri.isoformat() >= rank_points_cutoff
                snapshots = {}
                if can_award:
                    for p, _, _, _ in lb:
                        ensure_player_rp(rp_data, p)
                        snapshots[p] = rp_data[p]["points"]
                    award_period_points(rp_data, lb, WEEKLY_FIRST, WEEKLY_LAST)

                for r, (p, a, g, w) in enumerate(lb, 1):
                    if can_award:
                        old_pts = snapshots[p]
                        new_pts = rp_data[p]["points"]
                        delta = new_pts - old_pts
                        old_rank = get_rank_name(old_pts)
                        new_rank = get_rank_name(new_pts)
                        changes.append({
                            "player": p,
                            "delta": delta,
                            "new_total": new_pts,
                            "old_rank": old_rank,
                            "new_rank": new_rank,
                        })
                    standings.append({
                        "rank": r,
                        "player": p,
                        "avg": round(a, 2),
                        "games": g,
                        "weighted": round(w, 2),
                    })

                daily_results = build_daily_results(scores_data, mon.isoformat(), fri.isoformat())
                history["weekly"][week_key] = {
                    "start": mon.isoformat(),
                    "end": fri.isoformat(),
                    "standings": standings,
                    "point_changes": changes,
                    "daily_results": daily_results,
                }

                if can_award:
                    announcements.append({
                        "type": "weekly",
                        "key": week_key,
                        "start": mon.isoformat(),
                        "end": fri.isoformat(),
                        "standings": standings,
                        "changes": changes,
                    })
        mon += timedelta(days=7)

    # --- Archive ALL completed months ---
    first = date(min_d.year, min_d.month, 1)
    while True:
        nxt = date(first.year + 1, 1, 1) if first.month == 12 else date(first.year, first.month + 1, 1)
        last = nxt - timedelta(days=1)
        if last >= today:
            break
        month_key = first.strftime("%Y-%m")
        total_days = (last - first).days + 1
        if month_key not in history.setdefault("monthly", {}):
            lb = get_period_leaderboard(scores_data, first.isoformat(), last.isoformat(), total_days)
            if lb:
                changes = []
                standings = []
                can_award = rank_points_cutoff is None or last.isoformat() >= rank_points_cutoff
                snapshots = {}
                if can_award:
                    for p, _, _, _ in lb:
                        ensure_player_rp(rp_data, p)
                        snapshots[p] = rp_data[p]["points"]
                    award_period_points(rp_data, lb, MONTHLY_FIRST, MONTHLY_LAST)

                for r, (p, a, g, w) in enumerate(lb, 1):
                    if can_award:
                        old_pts = snapshots[p]
                        new_pts = rp_data[p]["points"]
                        delta = new_pts - old_pts
                        old_rank = get_rank_name(old_pts)
                        new_rank = get_rank_name(new_pts)
                        changes.append({"player": p, "delta": delta, "new_total": new_pts,
                                        "old_rank": old_rank, "new_rank": new_rank})
                    standings.append({"rank": r, "player": p, "avg": round(a, 2),
                                      "games": g, "weighted": round(w, 2)})

                history["monthly"][month_key] = {
                    "start": first.isoformat(), "end": last.isoformat(),
                    "standings": standings, "point_changes": changes,
                }
                if can_award:
                    announcements.append({"type": "monthly", "key": month_key,
                                          "start": first.isoformat(), "end": last.isoformat(),
                                          "standings": standings, "changes": changes})
        first = nxt

    assign_week_numbers(history)
    save_ranked_history(history)
    save_ranked_points(rp_data)
    return announcements


def reset_rank_points_now() -> int:
    """Reset all players' ranked points back to starting tier."""
    scores = load_scores()
    rp_data = {}
    for player in scores:
        rp_data[player] = {"points": STARTING_POINTS, "daily_dates": []}
    save_ranked_points(rp_data)
    return len(rp_data)


def reset_rank_points_from_current_week_with_bonus() -> tuple[int, int, str, str]:
    """Reset ranks, then reapply current Mon-Fri week points including weekly bonus.

    Returns (players_count, active_players_in_week, start_date, end_date).
    """
    scores_data = load_scores()

    # Reset everyone who has score history to starting points.
    rp_data = {}
    for player in scores_data:
        rp_data[player] = {"points": STARTING_POINTS, "daily_dates": []}

    start_date, week_end_date, _days_elapsed = get_current_week_range()
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    end_date = min(week_end_date, today_str)

    # Re-apply daily points for each day in this week range.
    current = date.fromisoformat(start_date)
    end_d = date.fromisoformat(end_date)
    while current <= end_d:
        day_key = current.isoformat()
        daily = []
        for player, info in scores_data.items():
            for d, s in zip(info.get("dates", []), info.get("scores", [])):
                if d == day_key:
                    daily.append((player, s))
                    break
        if daily:
            award_daily_points(rp_data, daily, day_key)
        current += timedelta(days=1)

    # Apply the weekly end-result bonus immediately.
    lb = get_period_leaderboard(scores_data, start_date, end_date, 5)
    if lb:
        award_period_points(rp_data, lb, WEEKLY_FIRST, WEEKLY_LAST)

    save_ranked_points(rp_data)
    return len(rp_data), len(lb), start_date, end_date


def reset_rank_points_from_previous_week_until_now() -> tuple[int, int, str, str, int]:
    """Reset ranks, then rebuild from previous week Monday to latest available day.

    Applies daily points for each day in range and weekly bonuses for each
    fully completed Mon-Fri week contained in that range.

    Returns (players_count, active_players, start_date, end_date, weeks_awarded).
    """
    scores_data = load_scores()

    # Reset everyone who has score history to starting points.
    rp_data = {}
    for player in scores_data:
        rp_data[player] = {"points": STARTING_POINTS, "daily_dates": []}

    today = datetime.now(timezone.utc).date()
    available_day = today - timedelta(days=1)
    this_monday = today - timedelta(days=today.weekday())
    start_date_d = this_monday - timedelta(days=7)
    end_date_d = available_day

    if end_date_d < start_date_d:
        save_ranked_points(rp_data)
        return len(rp_data), 0, start_date_d.isoformat(), end_date_d.isoformat(), 0

    start_date = start_date_d.isoformat()
    end_date = end_date_d.isoformat()

    # Re-apply daily points for each day in the range.
    active_players: set[str] = set()
    current = start_date_d
    while current <= end_date_d:
        day_key = current.isoformat()
        daily = []
        for player, info in scores_data.items():
            for d, s in zip(info.get("dates", []), info.get("scores", [])):
                if d == day_key:
                    daily.append((player, s))
                    active_players.add(player)
                    break
        if daily:
            award_daily_points(rp_data, daily, day_key)
        current += timedelta(days=1)

    # Apply weekly bonus for each completed week in the range.
    weeks_awarded = 0
    mon = start_date_d
    while mon + timedelta(days=4) <= end_date_d:
        fri = mon + timedelta(days=4)
        lb = get_period_leaderboard(scores_data, mon.isoformat(), fri.isoformat(), 5)
        if lb:
            award_period_points(rp_data, lb, WEEKLY_FIRST, WEEKLY_LAST)
            weeks_awarded += 1
        mon += timedelta(days=7)

    save_ranked_points(rp_data)
    return len(rp_data), len(active_players), start_date, end_date, weeks_awarded


def get_rank_reset_cutoff_date() -> str | None:
    """Return the latest applied rank reset date (YYYY-MM-DD), if any."""
    state = load_state()
    if state.get("rank_reset_last_applied"):
        return state["rank_reset_last_applied"]
    done = state.get("rank_reset_done", [])
    return max(done) if done else None


def apply_due_rank_resets(today_str: str | None = None) -> tuple[bool, list[str], int]:
    """Apply scheduled rank resets due today or earlier.

    Returns (applied, due_dates, affected_players).
    """
    if today_str is None:
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    state = load_state()
    scheduled = sorted(set(state.get("rank_reset_dates", [])))
    done = set(state.get("rank_reset_done", []))
    due_dates = [d for d in scheduled if d <= today_str and d not in done]

    if not due_dates:
        return False, [], 0

    affected = reset_rank_points_now()
    done.update(due_dates)
    state["rank_reset_dates"] = scheduled
    state["rank_reset_done"] = sorted(done)
    state["rank_reset_last_applied"] = max(due_dates)
    save_state(state)
    return True, due_dates, affected


def format_period_embed(announcement: dict) -> discord.Embed:
    """Create a Discord embed for a completed period announcement."""
    if announcement["type"] == "weekly":
        title = f"\U0001f4ca Weekly Ranked — {announcement['key']}"
        color = discord.Color.blue()
        period_label = f"{announcement['start']}  ➜  {announcement['end']}"
    else:
        title = f"\U0001f4c5 Monthly Ranked — {announcement['key']}"
        color = discord.Color.gold()
        period_label = f"{announcement['start']}  ➜  {announcement['end']}"

    embed = discord.Embed(title=title, color=color, description=period_label)

    # Standings
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for s in announcement["standings"]:
        medal = medals[s["rank"] - 1] if s["rank"] <= 3 else f"**{s['rank']}.**"
        cheating = " *(stonie is cheating)*" if s["rank"] <= 2 and s["player"].lower() == "stonie" else ""
        lines.append(f"{medal} {s['player']}{cheating} — {s['weighted']:.2f} weighted ({s['games']} games)")
    embed.add_field(name="Standings", value="\n".join(lines), inline=False)

    # Point changes
    pt_lines = []
    for c in announcement["changes"]:
        sign = "+" if c["delta"] >= 0 else ""
        icon = RANK_ICONS.get(c["new_rank"], "")
        rank_change = ""
        if c["old_rank"] != c["new_rank"]:
            old_icon = RANK_ICONS.get(c["old_rank"], "")
            rank_change = f"  {old_icon} {c['old_rank']} ➜ {icon} {c['new_rank']}"
        pt_lines.append(f"{icon} {c['player']}: {sign}{c['delta']} pts ({c['new_total']} total){rank_change}")
    embed.add_field(name="Point Changes", value="\n".join(pt_lines), inline=False)

    return embed


# --- Bot state (for catch-up after downtime) ---


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(data: dict) -> None:
    _atomic_json_write(STATE_FILE, data)


# --- Bot setup ---

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Wordle Tracker is online as {bot.user}")

    storage_ok, storage_msg = verify_storage_writable()
    print(storage_msg)
    if not storage_ok:
        print("WARNING: Bot cannot write score files. Score updates will fail until permissions/path are fixed.")

    reset_applied, due_dates, affected = apply_due_rank_resets()
    if reset_applied:
        print(f"Applied scheduled rank reset(s): {', '.join(due_dates)} ({affected} players reset).")

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
    rank_cutoff = get_rank_reset_cutoff_date()

    try:
        async for message in channel.history(limit=None, after=discord.Object(id=last_message_id), oldest_first=True):
            if message.author.id != WORDLE_BOT_ID:
                continue
            new_last_id = message.id
            results = parse_wordle_message(message.content, member_map)
            if results:
                date_key = get_date_key(message.created_at)
                added = record_scores(results, date_key)
                if added > 0:
                    if not rank_cutoff or date_key >= rank_cutoff:
                        rp_data = load_ranked_points()
                        award_daily_points(rp_data, results, date_key)
                        save_ranked_points(rp_data)
                    caught_up += added
    except Exception as e:
        print(f"Catch-up error: {e}")

    if caught_up > 0:
        announcements = archive_completed_periods(load_scores(), rank_cutoff)
        print(f"Catch-up complete! Added {caught_up} missed score(s).")
        # Post period-end announcements for any weeks/months completed while offline
        for ann in announcements:
            try:
                await channel.send(embed=format_period_embed(ann))
            except Exception as e:
                print(f"Could not post catch-up announcement: {e}")
    else:
        # Even with no new scores, check if periods ended while we were offline
        announcements = archive_completed_periods(load_scores(), rank_cutoff)
        if announcements:
            print(f"No new scores, but {len(announcements)} period(s) were archived.")
            for ann in announcements:
                try:
                    await channel.send(embed=format_period_embed(ann))
                except Exception as e:
                    print(f"Could not post catch-up announcement: {e}")
        else:
            print("No missed Wordle messages found.")

    save_state({"channel_id": channel_id, "last_message_id": new_last_id})


@bot.event
async def on_message(message: discord.Message):
    # Don't respond to ourselves
    if message.author == bot.user:
        return

    # Only listen to the Wordle bot
    if message.author.id != WORDLE_BOT_ID:
        await bot.process_commands(message)
        return

    # Build a member map for resolving <@ID> mentions to display names
    member_map = {}
    if message.guild:
        member_map = {m.id: m.display_name for m in message.guild.members}

    # Try to parse Wordle results from any bot message (or any message matching the format)
    results = parse_wordle_message(message.content, member_map)
    if results:
        try:
            reset_applied, due_dates, affected = apply_due_rank_resets()
            if reset_applied:
                await message.channel.send(
                    f"🔄 Scheduled rank reset applied ({', '.join(due_dates)}). "
                    f"Reset {affected} player(s) to 🟤 Bronze."
                )

            date_key = get_date_key(message.created_at)
            added = record_scores(results, date_key)
            if added > 0:
                # Award daily ranked points
                rp_data = load_ranked_points()
                award_daily_points(rp_data, results, date_key)
                save_ranked_points(rp_data)
                # Archive completed periods (also awards period points)
                announcements = archive_completed_periods(load_scores(), get_rank_reset_cutoff_date())
                await message.channel.send(
                    f"✅ Recorded {added} Wordle score(s) for today!"
                )
                for ann in announcements:
                    await message.channel.send(embed=format_period_embed(ann))
            else:
                await message.channel.send("ℹ️ These scores were already recorded.")

            # Track this channel + message for catch-up after restarts
            save_state({"channel_id": message.channel.id, "last_message_id": message.id})
        except Exception as e:
            print(f"Error while saving Wordle results: {e}")
            traceback.print_exc()
            await message.channel.send(
                "⚠️ I parsed the results, but failed to save score files. "
                "Please check bot file permissions and service logs."
            )

    # Ensure prefix commands still work
    await bot.process_commands(message)


# --- Commands ---


@bot.command(name="wordle")
async def wordle_leaderboard(ctx: commands.Context, *, arg: str = ""):
    """Show the Wordle leaderboard or a specific player's stats.

    Usage:
        !wordle          – Show all-time leaderboard
        !wordle weekly   – Show current Mon-Fri week leaderboard
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
                "`!wordle weekly` — Current Mon-Fri week\n"
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
                "`!wordle redate` — Shift all saved dates back by 1 day (admin)\n"
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

        start_date, _end_date, days_elapsed = get_current_week_range()
        leaderboard = get_ranked_leaderboard(data, start_date, days_elapsed)
        if not leaderboard:
            await ctx.send("No scores yet for this Mon-Fri week!")
            return

        rank_emojis = {1: "\U0001f947", 2: "\U0001f948", 3: "\U0001f949"}
        lines = []
        for i, (player, avg, games, weighted) in enumerate(leaderboard, start=1):
            rank = rank_emojis.get(i, f"**{i}.**")
            cheating = " *(stonie is cheating)*" if i <= 2 and player.lower() == "stonie" else ""
            lines.append(
                f"{rank} **{player}**{cheating} \u2014 {weighted:.2f} weighted \u00b7 "
                f"{avg:.2f} avg \u00b7 {games}/{days_elapsed} games"
            )
        embed = discord.Embed(
            title=f"\U0001f4c5 Wordle Leaderboard \u2014 This Week (Mon-Fri, Day {days_elapsed}/5)",
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
            cheating = " *(stonie is cheating)*" if i <= 2 and player.lower() == "stonie" else ""
            lines.append(f"{rank} **{player}**{cheating} \u2014 {weighted:.2f} weighted \u00b7 {avg:.2f} avg \u00b7 {games}/30 games")
        embed = discord.Embed(
            title="\U0001f4c6 Wordle Leaderboard \u2014 Last 30 Days",
            description="\n".join(lines),
            color=discord.Color.orange(),
        )
        embed.set_footer(text="Ranked by weighted score (missed days count as 7) \u00b7 Lower is better")
        await ctx.send(embed=embed)
        return

    # --- Admin reset ---
    if arg.lower() == "redate":
        if not ctx.author.guild_permissions.administrator:
            await ctx.send("❌ Only admins can run date migration.")
            return
        shifted, deduped = shift_all_score_dates(-1)
        await ctx.send(
            f"🛠️ Date migration complete. Shifted **{shifted}** entries by -1 day"
            f" and deduped **{deduped}** collisions."
        )
        return

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
        cheating = " *(stonie is cheating)*" if i <= 2 and player.lower() == "stonie" else ""
        lines.append(f"{rank} **{player}**{cheating} — {avg:.2f} avg ({games} games)")

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
        archive_completed_periods(data, get_rank_reset_cutoff_date())

    # --- History ---
    if arg.startswith("history"):
        history = load_ranked_history()
        period = "monthly" if "monthly" in arg or "month" in arg else "weekly"
        records = history.get(period, {})

        if not records:
            await ctx.send(f"No archived {period} results yet!")
            return

        if period == "weekly":
            assign_week_numbers(history)
            # Backfill daily_results for older history entries that predate this feature.
            all_scores = load_scores()
            changed = False
            for _k, _entry in history.get("weekly", {}).items():
                if "daily_results" not in _entry:
                    _entry["daily_results"] = build_daily_results(all_scores, _entry["start"], _entry["end"])
                    changed = True
            if changed:
                save_ranked_history(history)
            save_ranked_history(history)
            records = history.get("weekly", {})

        lines = []
        sorted_records = sorted(records.items(), key=lambda kv: kv[1].get("start", ""), reverse=True)
        for key, entry in sorted_records[:10]:
            standings = entry["standings"]
            if standings:
                winner = standings[0]
                if period == "weekly":
                    week_num = entry.get("week_number", "?")
                    lines.append(
                        f"**Week {week_num}** ({key}) — 🥇 **{winner['player']}** "
                        f"({winner['weighted']:.2f} weighted · {winner['avg']:.2f} avg · {winner['games']} games)"
                    )
                else:
                    lines.append(
                        f"**{key}** — 🥇 **{winner['player']}** "
                        f"({winner['weighted']:.2f} weighted · {winner['avg']:.2f} avg · {winner['games']} games)"
                    )

        if period == "weekly" and sorted_records:
            latest_key, latest_entry = sorted_records[0]
            latest_week_num = latest_entry.get("week_number", "?")
            daily_rows = latest_entry.get("daily_results", [])
            if daily_rows:
                lines.append("")
                lines.append(f"**Daily Scores — Most Recent Week {latest_week_num} ({latest_key})**")
                for row in daily_rows:
                    score_parts = [f"{s['player']} {s['score']}/6" for s in row.get("scores", [])]
                    lines.append(f"`{row['date']}`: " + " | ".join(score_parts))

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
        leaderboard = get_ranked_leaderboard(data, start_date, days_elapsed, end_date)

        if not leaderboard:
            await ctx.send(f"No scores yet for {month_label}!")
            return

        rank_emojis = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = []
        for i, (player, avg, games, weighted) in enumerate(leaderboard, start=1):
            rank = rank_emojis.get(i, f"**{i}.**")
            cheating = " *(stonie is cheating)*" if i <= 2 and player.lower() == "stonie" else ""
            lines.append(
                f"{rank} **{player}**{cheating} — {weighted:.2f} weighted · "
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
    leaderboard = get_ranked_leaderboard(data, start_date, days_elapsed, end_date)

    if not leaderboard:
        await ctx.send(f"No scores yet for {week_label}!")
        return

    rank_emojis = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = []
    for i, (player, avg, games, weighted) in enumerate(leaderboard, start=1):
        rank = rank_emojis.get(i, f"**{i}.**")
        cheating = " *(stonie is cheating)*" if i <= 2 and player.lower() == "stonie" else ""
        lines.append(
            f"{rank} **{player}**{cheating} — {weighted:.2f} weighted · "
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

    # --- Schedule rank reset at date ---
    if arg.lower().startswith("resetat "):
        if not ctx.author.guild_permissions.administrator:
            await ctx.send("❌ Only admins can schedule rank resets.")
            return

        target = arg.split(" ", 1)[1].strip()
        try:
            date.fromisoformat(target)
        except ValueError:
            await ctx.send("❌ Invalid date. Use `!rank resetat YYYY-MM-DD`.")
            return

        state = load_state()
        scheduled = sorted(set(state.get("rank_reset_dates", [])))
        if target not in scheduled:
            scheduled.append(target)
            scheduled.sort()
        state["rank_reset_dates"] = scheduled
        save_state(state)

        applied, due_dates, affected = apply_due_rank_resets()
        if applied:
            await ctx.send(
                f"✅ Scheduled reset added and immediately applied for {', '.join(due_dates)}. "
                f"Reset {affected} player(s) to 🟤 Bronze."
            )
        else:
            await ctx.send(f"🗓️ Rank reset scheduled for **{target}**.")
        return

    # --- Show scheduled rank resets ---
    if arg.lower() in ("resets", "schedule"):
        state = load_state()
        scheduled = sorted(set(state.get("rank_reset_dates", [])))
        done = set(state.get("rank_reset_done", []))
        pending = [d for d in scheduled if d not in done]

        embed = discord.Embed(title="🗓️ Rank Reset Schedule", color=discord.Color.dark_teal())
        embed.add_field(name="Pending", value="\n".join(pending) if pending else "None", inline=False)
        embed.add_field(name="Completed", value="\n".join(sorted(done)) if done else "None", inline=False)
        embed.set_footer(text="Use !rank resetat YYYY-MM-DD to add one")
        await ctx.send(embed=embed)
        return

    # --- Rank reset this week (Mon-Fri to now) + weekly bonus ---
    if arg.lower() in ("resetweek", "reset week"):
        if not ctx.author.guild_permissions.administrator:
            await ctx.send("❌ Only admins can reset ranks.")
            return

        players_count, active_players, start_date, end_date = reset_rank_points_from_current_week_with_bonus()
        await ctx.send(
            "🔄 Rank reset complete for current week context. "
            f"Rebuilt points from **{start_date}** to **{end_date}** and applied weekly bonus. "
            f"Updated **{players_count}** players (**{active_players}** active in this week)."
        )
        return

    # --- Rank reset previous week Monday up to latest available day ---
    if arg.lower() in ("resetprevweek", "reset prevweek", "resetlastweek", "reset lastweek"):
        if not ctx.author.guild_permissions.administrator:
            await ctx.send("❌ Only admins can reset ranks.")
            return

        players_count, active_players, start_date, end_date, weeks_awarded = (
            reset_rank_points_from_previous_week_until_now()
        )
        await ctx.send(
            "🔄 Rank reset complete for previous-week window. "
            f"Rebuilt points from **{start_date}** to **{end_date}**. "
            f"Applied **{weeks_awarded}** completed weekly bonus(es). "
            f"Updated **{players_count}** players (**{active_players}** active in range)."
        )
        return

    # --- Rank reset now ---
    if arg.lower() == "reset":
        if not ctx.author.guild_permissions.administrator:
            await ctx.send("❌ Only admins can reset ranks.")
            return
        affected = reset_rank_points_now()
        state = load_state()
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        done = set(state.get("rank_reset_done", []))
        done.add(today_str)
        state["rank_reset_done"] = sorted(done)
        state["rank_reset_last_applied"] = today_str
        save_state(state)
        await ctx.send(f"🔄 All ranks have been reset! {affected} player(s) start at 🟤 Bronze.")
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
    msg_count = 0
    # Keep scores in memory during scan — avoids disk I/O per message
    data = {}

    async for message in ctx.channel.history(limit=None, oldest_first=True):
        msg_count += 1
        if message.author.id != WORDLE_BOT_ID:
            continue

        results = parse_wordle_message(message.content, member_map)
        if results:
            messages_found += 1
            date_key = get_date_key(message.created_at)
            for player, score in results:
                if player not in data:
                    data[player] = {"scores": [], "dates": []}
                if date_key not in data[player]["dates"]:
                    data[player]["scores"].append(score)
                    data[player]["dates"].append(date_key)
                    total_added += 1

    # Single write to disk after all messages are processed
    save_scores(data)

    # Always rebuild ranked files from scanned scores so history/points
    # cannot drift from scores.json after an empty or partial scan.
    recalculate_all_ranked_points(data)
    rebuild_ranked_history(data)

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
