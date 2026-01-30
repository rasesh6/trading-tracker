import os
import json
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, jsonify, send_file, request
from requests import post, get

app = Flask(__name__)
DB_PATH = '/tmp/trading_tracker.db'
_db_initialized = False

# Database initialization
def init_db():
    global _db_initialized
    if _db_initialized:
        return

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
        total_amount REAL,
        timestamp TEXT,
        type TEXT,
        strike TEXT,
        expiration TEXT,
        realized_pl REAL DEFAULT 0,
        fee_amount REAL DEFAULT 0
    )''')

    # Add total_amount column if it doesn't exist (for existing databases)
    try:
        c.execute('''ALTER TABLE trades ADD COLUMN total_amount REAL DEFAULT 0''')
    except:
        pass  # Column already exists

    # Add fee_amount column if it doesn't exist (for existing databases)
    try:
        c.execute('''ALTER TABLE trades ADD COLUMN fee_amount REAL DEFAULT 0''')
    except:
        pass  # Column already exists

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
    _db_initialized = True

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
        print("Starting data update...")
        token = get_access_token()
        print(f"Got access token: {token[:10]}..." if token else "No token")
        account_id = get_account_id(token)
        print(f"Got account ID: {account_id}")

        # Fetch YTD data
        now = datetime.now()
        year_start = datetime(now.year, 1, 1).strftime('%Y-%m-%dT%H:%M:%SZ')
        end_date = now.strftime('%Y-%m-%dT%H:%M:%SZ')

        print(f"Fetching history from {year_start} to {end_date}")
        history = fetch_order_history(token, account_id, year_start, end_date)
        print(f"History response keys: {list(history.keys())}")
        portfolio = fetch_portfolio(token, account_id)

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        # Store trades
        transactions = history.get('transactions', [])
        print(f"Total transactions: {len(transactions)}")
        trade_txs = [t for t in transactions if t.get('type') == 'TRADE' and t.get('subType') == 'TRADE']
        print(f"Trade transactions: {len(trade_txs)}")

        # Log sample transaction to check available fields
        if trade_txs:
            print(f"Sample transaction keys: {list(trade_txs[0].keys())}")
            sample = trade_txs[0]
            net_amt = float(sample.get('netAmount', 0))
            princ_amt = float(sample.get('principalAmount', 0))
            print(f"Sample fee calculation: netAmount={net_amt}, principalAmount={princ_amt}, fee={abs(net_amt - princ_amt)}")

        for tx in trade_txs:
            tx_id = tx.get('id')
            symbol = tx.get('symbol', '')
            side = tx.get('side', '')
            quantity = int(tx.get('quantity', 0))
            principal_amount = float(tx.get('principalAmount', 0))
            # For options, principalAmount is per contract (price × 100 × quantity typically)
            # For stocks, principalAmount is total (price × quantity)
            # We store both:
            # - price: per-share or per-contract price (for display)
            # - total_amount: total dollar amount (for P&L calculation)
            security_type = tx.get('securityType', 'EQUITY')
            if security_type == 'EQUITY':
                # Stock: principalAmount is total, price is per share
                price = abs(principal_amount / quantity)
                total_amount = principal_amount
            else:
                # Option: principalAmount is total, calculate per-contract price
                price = abs(principal_amount / quantity)
                total_amount = principal_amount

            timestamp = tx.get('timestamp', '')
            # Get fees/commissions from the transaction
            # Actual fee = netAmount - principalAmount (both negative for buys)
            net_amount = float(tx.get('netAmount', 0))
            fees = abs(net_amount - principal_amount)

            opt_type, strike, expiration = parse_option_symbol(symbol)

            c.execute('''INSERT OR REPLACE INTO trades
                (transaction_id, symbol, side, quantity, price, total_amount, timestamp, type, strike, expiration, fee_amount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (tx_id, symbol, side, quantity, price, total_amount, timestamp, opt_type or security_type, strike, expiration, fees))

        # Calculate realized P&L using FIFO matching
        c.execute('''SELECT transaction_id, symbol, side, quantity, price, total_amount, timestamp, fee_amount, type FROM trades ORDER BY datetime(timestamp) ASC''')
        all_trades = c.fetchall()

        print(f"Processing {len(all_trades)} trades in chronological order")

        # Track positions: symbol -> list of (quantity, total_amount, tx_id, fee)
        from collections import defaultdict, deque

        long_positions = defaultdict(deque)  # Opening BUYs
        short_positions = defaultdict(deque)  # Opening SELLs (short positions)
        realized_pl = {}  # tx_id -> P&L (only for closing trades)

        for tx_id, symbol, side, quantity, price, total_amount, timestamp, fee, security_type in all_trades:
            if side == 'BUY':
                # BUY can either:
                # 1. Open a long position (if no short positions to close)
                # 2. Close a short position (buy to cover)
                quantity_to_process = quantity

                # First, try to close any existing short positions
                while quantity_to_process > 0 and short_positions[symbol]:
                    open_qty, open_total, open_tx_id, open_fee = short_positions[symbol][0]
                    close_qty = min(quantity_to_process, open_qty)

                    # Closing short: P&L = (open_total - close_total) × ratio - fees
                    # For stocks, total_amount is already the full amount
                    # For options, we need to calculate proportionally
                    close_total = total_amount * (close_qty / quantity)
                    open_total_proportional = open_total * (close_qty / open_qty)
                    trading_pl = open_total_proportional + close_total  # Both are negative, so adding gives profit
                    total_pl = trading_pl - open_fee - fee

                    print(f"  CLOSE SHORT: {symbol} bought ${abs(close_total):.2f} to close short opened at ${abs(open_total_proportional):.2f} × {close_qty} = P&L ${total_pl:.2f}")

                    # Assign P&L only to the closing (BUY) trade
                    realized_pl[tx_id] = realized_pl.get(tx_id, 0) + total_pl

                    if open_qty == close_qty:
                        short_positions[symbol].popleft()
                    else:
                        short_positions[symbol][0] = (open_qty - close_qty, open_total, open_tx_id, open_fee)

                    quantity_to_process -= close_qty

                # Any remaining quantity opens a new long position
                if quantity_to_process > 0:
                    long_positions[symbol].append((quantity_to_process, total_amount, tx_id, fee))
                    print(f"  OPEN LONG: {symbol} bought {quantity_to_process} @ ${price:.2f} (${abs(total_amount):.2f} total) fee=${fee:.2f}")

            elif side == 'SELL':
                # SELL can either:
                # 1. Open a short position (selling to open)
                # 2. Close a long position (selling to close)
                quantity_to_process = abs(quantity)

                # First, try to close any existing long positions
                while quantity_to_process > 0 and long_positions[symbol]:
                    open_qty, open_total, open_tx_id, open_fee = long_positions[symbol][0]
                    close_qty = min(quantity_to_process, open_qty)

                    # Closing long: P&L = (close_total - open_total) proportionally - fees
                    close_total = total_amount * (close_qty / quantity)
                    open_total_proportional = open_total * (close_qty / open_qty)
                    trading_pl = close_total + open_total_proportional  # open_total is negative
                    total_pl = trading_pl - open_fee - fee

                    print(f"  CLOSE LONG: {symbol} sold ${abs(close_total):.2f} to close long opened at ${abs(open_total_proportional):.2f} × {close_qty} = P&L ${total_pl:.2f}")

                    # Assign P&L only to the closing (SELL) trade
                    realized_pl[tx_id] = realized_pl.get(tx_id, 0) + total_pl

                    if open_qty == close_qty:
                        long_positions[symbol].popleft()
                    else:
                        long_positions[symbol][0] = (open_qty - close_qty, open_total, open_tx_id, open_fee)

                    quantity_to_process -= close_qty

                # Any remaining quantity opens a new short position
                if quantity_to_process > 0:
                    short_positions[symbol].append((quantity_to_process, total_amount, tx_id, fee))
                    print(f"  OPEN SHORT: {symbol} sold {quantity_to_process} @ ${price:.2f} (${abs(total_amount):.2f} total) fee=${fee:.2f}")

        # Reset all P&L to 0 first, then update only closed positions
        c.execute('''UPDATE trades SET realized_pl = 0''')
        for tx_id, pl in realized_pl.items():
            c.execute('''UPDATE trades SET realized_pl = ? WHERE transaction_id = ?''', (pl, tx_id))

        print(f"Calculated P&L for {len(realized_pl)} closed positions")

        conn.commit()
        conn.close()

        return {'status': 'success', 'updated': datetime.now().isoformat()}
    except Exception as e:
        print(f"Error updating data: {e}")
        import traceback
        traceback.print_exc()
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
    init_db()
    return jsonify(get_stats())

@app.route('/api/trades')
def trades():
    init_db()
    days = int(request.args.get('days', 7))
    return jsonify(get_trades(days))

@app.route('/api/update')
def update():
    init_db()
    return jsonify(update_data())

# Initialize and run
if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=8080)
