# Wordle Score Tracker — Discord Bot

Tracks daily Wordle results posted by the **Wordle** bot in your Discord server and keeps a running leaderboard with average scores.

## Setup

### 1. Create a Discord Bot

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application**, give it a name
3. Go to **Bot** → click **Reset Token** → copy the token
4. Under **Privileged Gateway Intents**, enable **Message Content Intent**
5. Go to **OAuth2** → **URL Generator** → select `bot` scope → select permissions:
   - Read Messages/View Channels
   - Send Messages
   - Embed Links
6. Copy the generated URL, open it in your browser, and invite the bot to your server

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` and paste your bot token:

```
DISCORD_TOKEN=your-actual-bot-token
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Run

```bash
python bot.py
```

## Commands

| Command | Description |
|---------|-------------|
| `!wordle` | Show the leaderboard (sorted by best average) |
| `!wordle @PlayerName` | Show a specific player's stats |
| `!wordle reset` | **Admin only** — clear all stored scores |

## How it works

- The bot watches for messages containing `"Here are yesterday's results:"` (the Wordle bot's daily summary)
- It parses each score line like `👑 4/6: @ambufire` or `5/6: @Jules Bracke @Stonie`
- `X/6` (failed attempt) is counted as a score of **7**
- Scores are stored in `scores.json` with date tracking to prevent duplicates
- The leaderboard ranks players by **lowest average** (best)

## Score storage

All data is stored locally in `scores.json`. Example:

```json
{
  "ambufire": {
    "scores": [4, 3, 5, 4],
    "dates": ["2026-03-10", "2026-03-11", "2026-03-12", "2026-03-13"]
  }
}
```
