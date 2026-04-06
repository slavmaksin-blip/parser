# Ricardo.ch Telegram Monitor Bot

A Telegram bot that monitors [ricardo.ch](https://www.ricardo.ch) for new listings and notifies you when they match your filters.

## Features

- **Category filter** — choose one or more categories (Electronics, Fashion, Home & Garden, etc.)
- **Keyword filter** — search for specific terms
- **Price range** — set a minimum and/or maximum price in CHF
- **Seller registration date** — only notify for sellers who registered before a given date (useful for filtering out new / potentially untrusted sellers)
- **Listing age** — only notify for listings posted within the last N hours
- Listings are checked every **30 minutes** automatically after `/monitor` is started

## Requirements

- Python 3.11+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## Setup

```bash
# 1. Clone the repo and install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env and set TELEGRAM_BOT_TOKEN

# 3. Run the bot
python bot.py
```

## Bot Commands

| Command | Description |
|---|---|
| `/start` | Welcome message |
| `/filter` | Open the interactive filter setup menu |
| `/myfilters` | Show your current filters |
| `/monitor` | Start receiving notifications |
| `/stop` | Stop receiving notifications |
| `/help` | Show help |
| `/cancel` | Cancel the current operation |

## Filter Options

| Filter | Description | Example |
|---|---|---|
| Categories | Which Ricardo.ch categories to watch | Electronics, Fashion |
| Keywords | Search terms (comma-separated) | `iPhone, MacBook` |
| Min. Price | Minimum listing price in CHF | `50` |
| Max. Price | Maximum listing price in CHF | `500` |
| Max. Seller Reg. Date | Only sellers registered *before* this date | `2023-01-01` |
| Max. Listing Age | Only listings posted within the last N hours | `24` |

## Project Structure

```
parser/
├── bot.py          # Telegram bot (entry point)
├── scraper.py      # Ricardo.ch async scraper
├── db.py           # SQLite database (aiosqlite)
├── requirements.txt
├── .env.example
└── README.md
```

## Notes

- The bot stores data in `parser.db` (SQLite) in the current directory.
- Seen listings are automatically cleaned up after 30 days.
- The scraper uses HTML parsing with BeautifulSoup. If Ricardo.ch changes its markup, the scraper selectors may need to be updated.
