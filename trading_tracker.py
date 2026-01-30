"""
Trading Tracker with Options Spread Support

Key Concepts:
- Credit Spreads: Receive premium, max profit = credit received
- Debit Spreads: Pay premium, max profit = (strike width - debit) × 100
- Spreads must be grouped together (multiple legs opened together)
- P&L calculated per spread, not per leg
"""

import os
import json
import sqlite3
import re
from datetime import datetime, timedelta
from flask import Flask, jsonify, send_file, request
from requests import post, get

app = Flask(__name__)
DB_PATH = '/tmp/trading_tracker.db'
_db_initialized = False

def init_db():
    global _db_initialized
    if _db_initialized:
        return

    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    c = conn.cursor()

    # Trades table - individual legs
    c.execute('''CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        transaction_id TEXT UNIQUE,
        symbol TEXT,
        underlying TEXT,
        expiry TEXT,
        strike REAL,
        opt_type TEXT,
        side TEXT,
        quantity INTEGER,
        price REAL,
        total_amount REAL,
        timestamp TEXT,
        fee_amount REAL DEFAULT 0,
        spread_id TEXT,
        realized_pl REAL DEFAULT 0
    )''')

    # Spreads table - grouped spread positions
    c.execute('''CREATE TABLE IF NOT EXISTS spreads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        spread_id TEXT UNIQUE,
        spread_type TEXT,
        underlying TEXT,
        expiry TEXT,
        opened_date TEXT,
        closed_date TEXT,
        status TEXT DEFAULT 'open',
        legs TEXT,
        entry_credit REAL DEFAULT 0,
        exit_debit REAL DEFAULT 0,
        realized_pl REAL DEFAULT 0,
        unrealized_pl REAL DEFAULT 0
    )''')

    # Single legs table - individual option positions
    c.execute('''CREATE TABLE IF NOT EXISTS single_legs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        leg_id TEXT UNIQUE,
        underlying TEXT,
        expiry TEXT,
        strike REAL,
        opt_type TEXT,
        side TEXT,
        quantity INTEGER,
        entry_price REAL,
        opened_date TEXT,
        closed_date TEXT,
        status TEXT DEFAULT 'open',
        transaction_id TEXT,
        realized_pl REAL DEFAULT 0,
        unrealized_pl REAL DEFAULT 0
    )''')

    conn.commit()
    conn.close()
    _db_initialized = True

def parse_option_symbol(symbol):
    """Parse option symbol: underlying, yymmdd, C/P, strike_cents"""
    match = re.match(r'(.+)(\d{6})([PC])(\d{8})', symbol)
    if match:
        underlying = match.group(1)
        yymmdd = match.group(2)
        opt_type = match.group(3)
        strike_cents = int(match.group(4))
        strike = strike_cents / 1000
        return underlying, yymmdd, opt_type, strike
    return None, None, None, None

def identify_spread_type(legs):
    """Identify spread type from legs"""
    calls = [l for l in legs if l['opt_type'] == 'C']
    puts = [l for l in legs if l['opt_type'] == 'P']

    # Debug logging for unknown spreads
    if len(legs) != 2:
        print(f"  DEBUG: Not 2 legs: {len(legs)} legs")
        for leg in legs:
            print(f"    {leg['symbol']} - {leg['opt_type']} - {leg['side']} - strike:{leg['strike']} - qty:{leg['quantity']}")
        return "Unknown Spread"

    if len(calls) == 2 and len(puts) == 0:
        long_calls = [l for l in calls if l['side'] == 'BUY']
        short_calls = [l for l in calls if l['side'] == 'SELL']
        if long_calls and short_calls:
            if long_calls[0]['strike'] < short_calls[0]['strike']:
                return "Bull Call Spread (Debit)"
            else:
                return "Bear Call Spread (Credit)"
        else:
            print(f"  DEBUG: 2 calls but no BUY/SELL pair: both {[l['side'] for l in calls]}")
            return "Unknown Spread"

    if len(puts) == 2 and len(calls) == 0:
        long_puts = [l for l in puts if l['side'] == 'BUY']
        short_puts = [l for l in puts if l['side'] == 'SELL']
        if long_puts and short_puts:
            if long_puts[0]['strike'] > short_puts[0]['strike']:
                return "Bear Put Spread (Debit)"
            else:
                return "Bull Put Spread (Credit)"
        else:
            print(f"  DEBUG: 2 puts but no BUY/SELL pair: both {[l['side'] for l in puts]}")
            return "Unknown Spread"

    if len(calls) == 2 and len(puts) == 2:
        return "Iron Condor"

    print(f"  DEBUG: Mixed legs - {len(calls)} calls, {len(puts)} puts")
    return "Unknown Spread"

def group_trades_into_spreads(trades):
    """Group option trades into spreads"""
    from collections import defaultdict

    # Separate options from stocks
    option_trades = []
    stock_trades = []

    for trade in trades:
        symbol = trade.get('symbol', '')
        underlying, expiry, opt_type, strike = parse_option_symbol(symbol)
        if underlying:
            option_trades.append({**trade, 'underlying': underlying, 'expiry': expiry, 'opt_type': opt_type, 'strike': strike})
        else:
            stock_trades.append(trade)

    # Group options by underlying + expiry + opening day
    potential_groups = defaultdict(list)
    for trade in option_trades:
        timestamp = datetime.fromisoformat(trade['timestamp'].replace('Z', '+00:00'))
        date_key = timestamp.date().isoformat()
        key = f"{trade['underlying']}_{trade['expiry']}_{date_key}"
        potential_groups[key].append({**trade, 'datetime': timestamp})

    # Identify spreads (legs opened within 1 hour)
    spreads = []
    single_legs = []
    processed = set()

    for key, legs in potential_groups.items():
        legs.sort(key=lambda x: x['datetime'])

        i = 0
        while i < len(legs):
            if legs[i]['transaction_id'] in processed:
                i += 1
                continue

            current = legs[i]
            group = [current]
            j = i + 1

            while j < len(legs):
                time_diff = (legs[j]['datetime'] - current['datetime']).total_seconds()
                if time_diff < 3600 and legs[j]['transaction_id'] not in processed:
                    group.append(legs[j])
                    j += 1
                else:
                    break

            if len(group) >= 2:
                # Found a spread!
                spread_type = identify_spread_type(group)
                spread_id = f"{group[0]['underlying']}_{group[0]['expiry']}_{current['datetime'].strftime('%Y%m%d%H%M')}"

                # Calculate entry credit/debit with detailed logging
                entry_credit = 0
                print(f"  SPREAD {spread_id}: {spread_type}")
                for leg in group:
                    leg_contrib = leg['total_amount']  # Use raw amount
                    entry_credit += leg_contrib
                    print(f"    {leg['symbol']}: {leg['side']} {leg['quantity']} @ ${leg['price']:.2f} = ${leg_contrib:+.2f}")

                print(f"    → Net entry: ${entry_credit:+.2f}")

                spreads.append({
                    'spread_id': spread_id,
                    'type': spread_type,
                    'underlying': group[0]['underlying'],
                    'expiry': group[0]['expiry'],
                    'opened_date': current['datetime'].isoformat(),
                    'legs': group,
                    'entry_credit': entry_credit,
                    'leg_ids': [l['transaction_id'] for l in group]
                })

                for leg in group:
                    processed.add(leg['transaction_id'])
            else:
                # Single leg
                single_legs.append(current)

            i += 1

    return spreads, single_legs, stock_trades

def get_access_token():
    """Get access token from Public API"""
    secret = os.environ.get('PUBLIC_API_TOKEN')
    if not secret:
        raise Exception('PUBLIC_API_TOKEN not set')

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
    params = {'start': start_date, 'end': end_date, 'pageSize': 1000}
    response = get(url, params=params, headers={'Authorization': f'Bearer {token}'})
    return response.json()

def fetch_portfolio(token, account_id):
    """Fetch current portfolio for unrealized P&L calculation"""
    url = f"https://api.public.com/userapigateway/trading/{account_id}/portfolio/v2"
    response = get(url, headers={'Authorization': f'Bearer {token}'})
    return response.json()

def calculate_unrealized_pl(spread, portfolio_positions):
    """Calculate unrealized P&L for an open spread using current market prices"""
    try:
        legs = json.loads(spread['legs'])
        total_current_value = 0

        for leg_tx_id in legs:
            # Find matching position in portfolio
            # Portfolio positions contain: symbol, quantity, averagePrice, currentPrice
            for pos in portfolio_positions:
                if pos.get('symbol') and leg_tx_id in str(pos):
                    # Current value = quantity × currentPrice
                    # For long: positive value, for short: negative value
                    qty = float(pos.get('quantity', 0))
                    current_price = float(pos.get('currentPrice', pos.get('averagePrice', 0)))
                    position_value = qty * current_price
                    total_current_value += position_value
                    break

        # Unrealized P&L = current spread value - entry credit
        # For credit spread (positive entry): profit if current value < entry
        # For debit spread (negative entry): profit if current value > entry
        entry_value = spread['entry_credit']
        unrealized_pl = total_current_value - entry_value

        return unrealized_pl
    except Exception as e:
        print(f"  Error calculating unrealized P&L for spread {spread['spread_id']}: {e}")
        return 0

def update_data():
    """Fetch latest data and update database"""
    try:
        print("Starting data update...")
        token = get_access_token()
        account_id = get_account_id(token)

        # Fetch YTD data
        now = datetime.now()
        year_start = datetime(now.year, 1, 1).strftime('%Y-%m-%dT%H:%M:%SZ')
        end_date = now.strftime('%Y-%m-%dT%H:%M:%SZ')

        print(f"Fetching history from {year_start} to {end_date}")
        history = fetch_order_history(token, account_id, year_start, end_date)

        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        c = conn.cursor()

        # Store trades
        transactions = history.get('transactions', [])
        trade_txs = [t for t in transactions if t.get('type') == 'TRADE' and t.get('subType') == 'TRADE']

        print(f"Processing {len(trade_txs)} trades...")

        for tx in trade_txs:
            tx_id = tx.get('id')
            symbol = tx.get('symbol', '')
            side = tx.get('side', '')
            quantity = int(tx.get('quantity', 0))
            principal_amount = float(tx.get('principalAmount', 0))

            price = abs(principal_amount / max(quantity, 1))
            total_amount = principal_amount
            timestamp = tx.get('timestamp', '')

            net_amount = float(tx.get('netAmount', 0))
            fees = abs(net_amount - principal_amount)

            underlying, expiry, opt_type, strike = parse_option_symbol(symbol)

            c.execute('''INSERT OR REPLACE INTO trades
                (transaction_id, symbol, underlying, expiry, strike, opt_type, side, quantity, price, total_amount, timestamp, fee_amount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (tx_id, symbol, underlying or '', expiry or '', strike or 0, opt_type or 'STOCK', side, quantity, price, total_amount, timestamp, fees))

        # Group trades into spreads
        c.execute('''SELECT * FROM trades ORDER BY datetime(timestamp) ASC''')
        all_trades = []
        columns = ['id', 'transaction_id', 'symbol', 'underlying', 'expiry', 'strike', 'opt_type', 'side', 'quantity', 'price', 'total_amount', 'timestamp', 'fee_amount', 'spread_id', 'realized_pl']
        for row in c.fetchall():
            all_trades.append(dict(zip(columns, row)))

        spreads, single_legs, stock_trades = group_trades_into_spreads(all_trades)

        # Fetch portfolio to detect closed positions
        print("Fetching portfolio to detect closed positions...")
        portfolio_data = fetch_portfolio(token, account_id)
        portfolio_positions = portfolio_data.get('positions', [])
        portfolio_symbols = set(pos.get('symbol', '') for pos in portfolio_positions)
        print(f"Portfolio has {len(portfolio_positions)} positions: {list(portfolio_symbols)[:10]}")

        # Determine which spreads are open vs closed
        # A spread is open if any of its leg symbols are in the current portfolio
        for spread in spreads:
            leg_symbols = set(leg['symbol'] for leg in spread['legs'])
            is_open = bool(leg_symbols & portfolio_symbols)  # Intersection non-empty = open
            status = 'open' if is_open else 'closed'

            # For closed spreads, calculate realized P&L by matching opening and closing trades
            realized_pl = 0
            closed_date = None

            if status == 'closed':
                # Find all trades for this spread's underlying+expiry
                spread_trades = [t for t in all_trades if t.get('underlying') == spread['underlying'] and t.get('expiry') == spread['expiry']]

                # Separate opening and closing trades
                # Opening: trades that are part of the spread
                opening_tx_ids = set(l['transaction_id'] for l in spread['legs'])
                closing_trades = [t for t in spread_trades if t['transaction_id'] not in opening_tx_ids]

                if closing_trades:
                    # Calculate total entry (opening) and exit (closing)
                    total_entry = sum(l['total_amount'] for l in spread['legs'])
                    total_exit = sum(t['total_amount'] for t in closing_trades)

                    # Realized P&L = exit - entry (for spreads, this is the profit/loss)
                    realized_pl = total_exit - total_entry

                    # Find the latest closing date
                    closing_trades_sorted = sorted(closing_trades, key=lambda x: x['timestamp'], reverse=True)
                    if closing_trades_sorted:
                        closed_date = closing_trades_sorted[0]['timestamp']

                    print(f"    CLOSED: Entry ${total_entry:+.2f} → Exit ${total_exit:+.2f} = P&L ${realized_pl:+.2f}")

            c.execute('''INSERT OR REPLACE INTO spreads
                (spread_id, spread_type, underlying, expiry, opened_date, closed_date, status, legs, entry_credit, realized_pl, unrealized_pl)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (spread['spread_id'], spread['type'], spread['underlying'], spread['expiry'],
                 spread['opened_date'], closed_date, status, json.dumps([l['transaction_id'] for l in spread['legs']]),
                 spread['entry_credit'], realized_pl, 0))  # Unrealized = 0 for now

            if status == 'open':
                print(f"  {spread['underlying']} {spread['type']}: OPEN - Entry ${spread['entry_credit']:+.2f}")
            else:
                print(f"  {spread['underlying']} {spread['type']}: CLOSED - Entry ${spread['entry_credit']:+.2f}, P&L ${realized_pl:+.2f}")

            # Link trades to spread
            for leg_id in spread['leg_ids']:
                c.execute('''UPDATE trades SET spread_id = ? WHERE transaction_id = ?''', (spread['spread_id'], leg_id))

        # Store single legs with unrealized P&L
        for leg in single_legs:
            leg_id = f"single_{leg['transaction_id']}"
            opt_type_name = 'CALL' if leg['opt_type'] == 'C' else 'PUT'
            leg_type = f"{leg['side']} {opt_type_name}"  # e.g., "BUY CALL" or "SELL PUT"

            # Check if this single leg is still open
            is_open = leg['symbol'] in portfolio_symbols
            status = 'open' if is_open else 'closed'

            # Calculate unrealized P&L for single leg
            unrealized_pl = 0
            realized_pl = 0

            if status == 'open' and portfolio_positions:
                for pos in portfolio_positions:
                    if pos.get('symbol') == leg['symbol']:
                        qty = float(pos.get('quantity', 0))
                        current_price = float(pos.get('currentPrice', pos.get('averagePrice', leg['price'])))
                        current_value = qty * current_price
                        entry_value = leg['total_amount']

                        # For long (BUY): P&L = current - entry
                        # For short (SELL): P&L = entry - current
                        if leg['side'] == 'BUY':
                            unrealized_pl = current_value - entry_value
                        else:
                            unrealized_pl = entry_value - current_value
                        break

            c.execute('''INSERT OR REPLACE INTO single_legs
                (leg_id, underlying, expiry, strike, opt_type, side, quantity, entry_price, opened_date, status, transaction_id, unrealized_pl, realized_pl)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (leg_id, leg['underlying'], leg['expiry'], leg['strike'], leg['opt_type'],
                 leg['side'], leg['quantity'], leg['price'], leg['timestamp'], status,
                 leg['transaction_id'], unrealized_pl, realized_pl))

            if status == 'open':
                entry_sign = '+' if leg['total_amount'] >= 0 else '-'
                print(f"  {leg['underlying']} {leg_type} @ ${leg['strike']}: OPEN - Entry {entry_sign}${abs(leg['total_amount']):.2f}, Unrealized: ${unrealized_pl:+.2f}")
            else:
                print(f"  {leg['underlying']} {leg_type} @ ${leg['strike']}: CLOSED")

        conn.commit()
        conn.close()

        print(f"Identified {len(spreads)} spreads, {len(single_legs)} single legs, {len(stock_trades)} stock trades")
        return {'status': 'success', 'spreads': len(spreads), 'legs': len(single_legs), 'stocks': len(stock_trades)}

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return {'status': 'error', 'message': str(e)}

def get_stats():
    """Get trading statistics with spread grouping"""
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    c = conn.cursor()

    # Get all spreads
    c.execute('''SELECT * FROM spreads ORDER BY opened_date DESC''')
    columns = ['id', 'spread_id', 'spread_type', 'underlying', 'expiry', 'opened_date', 'closed_date', 'status', 'legs', 'entry_credit', 'exit_debit', 'realized_pl', 'unrealized_pl']
    spreads = [dict(zip(columns, row)) for row in c.fetchall()]

    # Get all single legs
    c.execute('''SELECT * FROM single_legs ORDER BY opened_date DESC''')
    leg_columns = ['id', 'leg_id', 'underlying', 'expiry', 'strike', 'opt_type', 'side', 'quantity', 'entry_price', 'opened_date', 'closed_date', 'status', 'transaction_id', 'realized_pl', 'unrealized_pl']
    single_legs = [dict(zip(leg_columns, row)) for row in c.fetchall()]

    # Calculate stats
    closed_spreads = [s for s in spreads if s['status'] == 'closed']
    open_spreads = [s for s in spreads if s['status'] == 'open']

    open_single_legs = [l for l in single_legs if l['status'] == 'open']
    closed_single_legs = [l for l in single_legs if l['status'] == 'closed']

    total_realized_pl = sum(s['realized_pl'] for s in closed_spreads) + sum(l['realized_pl'] for l in closed_single_legs)
    total_unrealized_pl = sum(s.get('unrealized_pl', 0) for s in open_spreads) + sum(l.get('unrealized_pl', 0) for l in open_single_legs)

    conn.close()

    return {
        'total_spreads': len(spreads),
        'open_spreads': len(open_spreads),
        'closed_spreads': len(closed_spreads),
        'total_single_legs': len(single_legs),
        'open_single_legs': len(open_single_legs),
        'closed_single_legs': len(closed_single_legs),
        'total_realized_pl': total_realized_pl,
        'total_unrealized_pl': total_unrealized_pl,
        'spreads': spreads[:50],
        'single_legs': single_legs[:50]
    }

def get_trades(days=7):
    """Get recent trades grouped by spread"""
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    c = conn.cursor()

    # Get recent spreads
    since = (datetime.now() - timedelta(days=days)).isoformat()
    c.execute('''SELECT * FROM spreads WHERE opened_date >= ? ORDER BY opened_date DESC''', (since,))

    columns = ['id', 'spread_id', 'spread_type', 'underlying', 'expiry', 'opened_date', 'closed_date', 'status', 'legs', 'entry_credit', 'exit_debit', 'realized_pl', 'unrealized_pl']
    spreads = [dict(zip(columns, row)) for row in c.fetchall()]

    # Get leg details for each spread
    for spread in spreads:
        leg_ids = json.loads(spread['legs'])
        placeholders = ','.join('?' * len(leg_ids))
        c.execute(f'''SELECT symbol, side, price, total_amount, realized_pl FROM trades WHERE transaction_id IN ({placeholders})''', leg_ids)
        spread['legs_detail'] = [dict(zip(['symbol', 'side', 'price', 'total_amount', 'realized_pl'], row)) for row in c.fetchall()]

    conn.close()
    return spreads

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

@app.route('/api/reset')
def reset():
    """Reset database"""
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    c = conn.cursor()
    c.execute('DELETE FROM trades')
    c.execute('DELETE FROM spreads')
    c.execute('DELETE FROM single_legs')
    conn.commit()
    conn.close()
    return jsonify({'status': 'reset'})

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=8080)
