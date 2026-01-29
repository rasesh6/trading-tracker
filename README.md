# Trading Tracker

Track all your Public.com trades with daily, monthly, and YTD statistics.

## Features

- **Real-time P&L Tracking**: Today, Month, and Year-to-Date profit/loss
- **Win Rate Statistics**: Track your success rate across all time periods
- **Visual Charts**: Cumulative P&L chart showing trends over time
- **Trade History**: Detailed breakdown of recent trades
- **Auto-refresh**: Data updates automatically every 5 minutes

## Setup

1. Set `PUBLIC_API_TOKEN` environment variable (from Public.com)
2. Deploy to Railway or run locally

## Environment Variables

- `PUBLIC_API_TOKEN`: Your Public.com API secret token
- `PORT`: Server port (default: 8080)

## Local Development

```bash
pip install -r requirements.txt
export PUBLIC_API_TOKEN=your_token
python trading_tracker.py
```

Visit http://localhost:8080

## Railway Deployment

Connect this GitHub repo to Railway and set:
- `PUBLIC_API_TOKEN` environment variable

## API Endpoints

- `GET /` - Dashboard
- `GET /api/stats` - Trading statistics
- `GET /api/trades?days=7` - Recent trades
- `GET /api/update` - Force data update

## Data Storage

Uses SQLite database (`trading_tracker.db`) for persistent storage.
