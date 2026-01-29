import os
import json
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, jsonify, send_file, request
from requests import post, get

app = Flask(__name__)
DB_PATH = 'trading_tracker.db'

# Database initialization
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Trades table
    c.execute('''CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        transaction_id TEXT UNIQUE,
        symbol TEXT,
        side TEXT,
        quantity INTEGER,
        price REAL,
        timestamp TEXT,
        type TEXT,
        strike TEXT,
        expiration TEXT,
        realized_pl REAL DEFAULT 0
    )''')

    # Daily summary table
    c.execute('''CREATE TABLE IF NOT EXISTS daily_summary (
        date TEXT PRIMARY KEY,
        total_orders INTEGER,
        filled_orders INTEGER,
        pending_orders INTEGER,
        total_pl REAL,
        win_rate REAL,
        fees REAL
    )''')

    conn.commit()
    conn.close()

def get_access_token():
    """Get access token from Public API"""
    secret = os.environ.get('PUBLIC_API_TOKEN')
    if not secret:
        raise Exception("PUBLIC_API_TOKEN not set")

    response = post(
        'https://api.public.com/userapiauthservice/personal/access-tokens',
        json={'secret': secret, 'validityInMinutes': 120},
        headers={'Content-Type': 'application/json'}
    )
    return response.json()['accessToken']

def get_account_id(token):
    """Get brokerage account ID"""
    response = get(
        'https://api.public.com/userapigateway/trading/account',
        headers={'Authorization': f'Bearer {token}'}
    )
    accounts = response.json().get('accounts', [])
    for acc in accounts:
        if acc.get('accountType') == 'BROKERAGE':
            return acc['accountId']
    return accounts[0]['accountId'] if accounts else None

def fetch_order_history(token, account_id, start_date, end_date):
    """Fetch order history from Public API"""
    url = f"https://api.public.com/userapigateway/trading/{account_id}/history"
    params = {
        'start': start_date,
        'end': end_date,
        'pageSize': 1000
    }
    response = get(url, params=params, headers={'Authorization': f'Bearer {token}'})
    return response.json()

def fetch_portfolio(token, account_id):
    """Fetch current portfolio"""
    url = f"https://api.public.com/userapigateway/trading/{account_id}/portfolio/v2"
    response = get(url, headers={'Authorization': f'Bearer {token}'})
    return response.json()

def parse_option_symbol(symbol):
    """Parse option symbol to get type, strike, expiration"""
    import re
    match = re.match(r'(.+)(\d{6})([PC])(\d{8})', symbol)
    if match:
        underlying = match.group(1)
        yymmdd = match.group(2)
        opt_type = 'PUT' if match.group(3) == 'P' else 'CALL'
        strike_cents = int(match.group(4))
        strike = strike_cents / 1000
        year = '20' + yymmdd[0:2]
        month = yymmdd[2:4]
        day = yymmdd[4:6]
        expiration = f'{year}-{month}-{day}'
        return opt_type, strike, expiration
    return None, None, None

def update_data():
    """Fetch latest data and update database"""
    try:
        token = get_access_token()
        account_id = get_account_id(token)

        # Fetch YTD data
        now = datetime.now()
        year_start = datetime(now.year, 1, 1).isoformat() + 'Z'[:0]
        end_date = now.isoformat() + 'Z'[:0]

        history = fetch_order_history(token, account_id, year_start, end_date)
        portfolio = fetch_portfolio(token, account_id)

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        # Store trades
        transactions = history.get('transactions', [])
        trade_txs = [t for t in transactions if t.get('type') == 'TRADE' and t.get('subType') == 'TRADE']

        for tx in trade_txs:
            tx_id = tx.get('id')
            symbol = tx.get('symbol', '')
            side = tx.get('side', '')
            quantity = int(tx.get('quantity', 0))
            price = abs(float(tx.get('principalAmount', 0)) / max(quantity, 1))
            timestamp = tx.get('timestamp', '')

            opt_type, strike, expiration = parse_option_symbol(symbol)

            c.execute('''INSERT OR REPLACE INTO trades
                (transaction_id, symbol, side, quantity, price, timestamp, type, strike, expiration)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (tx_id, symbol, side, quantity, price, timestamp, opt_type or 'STOCK', strike, expiration))

        # Calculate daily summaries
        c.execute('''SELECT date(timestamp) as trade_date,
                    COUNT(*) as orders,
                    SUM(CASE WHEN realized_pl != 0 THEN 1 ELSE 0 END) as filled,
                    SUM(realized_pl) as pl
                    FROM trades GROUP BY date(timestamp)''')

        conn.commit()
        conn.close()

        return {'status': 'success', 'updated': datetime.now().isoformat()}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}

def get_stats():
    """Get trading statistics"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Today's stats
    today = datetime.now().strftime('%Y-%m-%d')
    c.execute('''SELECT COUNT(*), SUM(realized_pl),
                SUM(CASE WHEN realized_pl > 0 THEN 1 ELSE 0 END) as wins
                FROM trades WHERE date(timestamp) = ?''', (today,))
    row = c.fetchone()
    today_stats = {
        'orders': row[0] or 0,
        'pl': row[1] or 0,
        'wins': row[2] or 0,
        'win_rate': round((row[2] or 0) / max(row[0] or 1, 1) * 100, 1)
    }

    # Month stats
    month_start = datetime.now().replace(day=1).strftime('%Y-%m-%d')
    c.execute('''SELECT COUNT(*), SUM(realized_pl),
                SUM(CASE WHEN realized_pl > 0 THEN 1 ELSE 0 END) as wins
                FROM trades WHERE date(timestamp) >= ?''', (month_start,))
    row = c.fetchone()
    month_stats = {
        'orders': row[0] or 0,
        'pl': row[1] or 0,
        'wins': row[2] or 0,
        'win_rate': round((row[2] or 0) / max(row[0] or 1, 1) * 100, 1)
    }

    # YTD stats
    year_start = datetime.now().replace(month=1, day=1).strftime('%Y-%m-%d')
    c.execute('''SELECT COUNT(*), SUM(realized_pl),
                SUM(CASE WHEN realized_pl > 0 THEN 1 ELSE 0 END) as wins,
                date(timestamp) as trade_date, SUM(realized_pl) as daily_pl
                FROM trades WHERE date(timestamp) >= ?
                GROUP BY date(timestamp) ORDER BY trade_date''', (year_start,))

    ytd_trades = c.fetchall()
    ytd_daily = [{'date': r[3], 'pl': r[4]} for r in ytd_trades]

    ytd_stats = {
        'orders': sum(r[0] for r in ytd_trades),
        'pl': sum(r[1] for r in ytd_trades),
        'wins': sum(r[2] for r in ytd_trades),
        'win_rate': round(sum(r[2] for r in ytd_trades) / max(sum(r[0] for r in ytd_trades), 1) * 100, 1),
        'daily_breakdown': ytd_daily
    }

    conn.close()

    return {
        'today': today_stats,
        'month': month_stats,
        'ytd': ytd_stats,
        'last_updated': datetime.now().isoformat()
    }

def get_trades(days=7):
    """Get recent trades"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    c.execute('''SELECT * FROM trades WHERE date(timestamp) >= ?
                ORDER BY timestamp DESC''', (since,))

    columns = ['id', 'transaction_id', 'symbol', 'side', 'quantity', 'price', 'timestamp', 'type', 'strike', 'expiration', 'realized_pl']
    trades = [dict(zip(columns, row)) for row in c.fetchall()]

    conn.close()
    return trades

# API Routes
@app.route('/')
def index():
    return send_file('dashboard.html')

@app.route('/api/stats')
def stats():
    return jsonify(get_stats())

@app.route('/api/trades')
def trades():
    days = int(request.args.get('days', 7))
    return jsonify(get_trades(days))

@app.route('/api/update')
def update():
    return jsonify(update_data())

# Initialize and run
if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=8080)
