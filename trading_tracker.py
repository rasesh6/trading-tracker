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

def expiry_to_iso(expiry_yymmdd):
    """Convert YYMMDD expiry to ISO format datetime (at 23:59:59 ET)"""
    if not expiry_yymmdd or len(expiry_yymmdd) != 6:
        return None
    try:
        yy = int(expiry_yymmdd[0:2])
        mm = int(expiry_yymmdd[2:4])
        dd = int(expiry_yymmdd[4:6])
        # Convert 2-digit year to 4-digit (2000-2099)
        yyyy = 2000 + yy
        # Return as end of day on expiration date
        return datetime(yyyy, mm, dd, 23, 59, 59).isoformat()
    except:
        return None

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

            if len(group) == 2:
                # Found a potential spread (exactly 2 legs)
                spread_type = identify_spread_type(group)

                # Only treat as spread if successfully identified
                if spread_type != "Unknown Spread":
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
                    # Unknown spread type - treat as single legs
                    print(f"  Group of {len(group)} legs with unknown type, treating as single legs")
                    for leg in group:
                        single_legs.append(leg)
                        processed.add(leg['transaction_id'])
            else:
                # Not exactly 2 legs (could be 1, 3, 4, etc.) - treat each as single leg
                if len(group) > 2:
                    print(f"  Group of {len(group)} legs, treating each as single leg")
                for leg in group:
                    single_legs.append(leg)
                    processed.add(leg['transaction_id'])

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
            # Difference between net and principal includes:
            # - Positive value = rebate (net > principal, e.g., -99.82 vs -100.00 = +0.18 rebate)
            # - Negative value = fee (net < principal)
            fee_or_rebate = net_amount - principal_amount

            underlying, expiry, opt_type, strike = parse_option_symbol(symbol)

            c.execute('''INSERT OR REPLACE INTO trades
                (transaction_id, symbol, underlying, expiry, strike, opt_type, side, quantity, price, total_amount, timestamp, fee_amount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (tx_id, symbol, underlying or '', expiry or '', strike or 0, opt_type or 'STOCK', side, quantity, price, total_amount, timestamp, fee_or_rebate))

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

        # Debug: print first portfolio position structure
        if portfolio_positions:
            first_symbol = portfolio_positions[0].get('instrument', {}).get('symbol', '')
            print(f"First portfolio symbol: {first_symbol}")

        # Extract symbols and strip -OPTION suffix if present
        portfolio_symbols = set()
        for pos in portfolio_positions:
            symbol = pos.get('instrument', {}).get('symbol', '')
            if symbol:
                # Strip -OPTION suffix to match trade symbols
                clean_symbol = symbol.replace('-OPTION', '')
                portfolio_symbols.add(clean_symbol)
                print(f"  Portfolio position: {symbol} -> {clean_symbol}")

        print(f"Portfolio has {len(portfolio_positions)} positions, {len(portfolio_symbols)} unique symbols: {sorted(portfolio_symbols)}")

        # Determine which spreads are open vs closed
        # A spread is open if any of its leg symbols are in the current portfolio
        for spread in spreads:
            leg_symbols = set(leg['symbol'] for leg in spread['legs'])
            is_open = bool(leg_symbols & portfolio_symbols)  # Intersection non-empty = open
            status = 'open' if is_open else 'closed'

            # For closed spreads, calculate realized P&L by matching opening and closing trades
            realized_pl = 0
            closed_date = None
            unrealized_pl = 0
            exit_debit = 0

            if status == 'closed':
                # Calculate realized P&L by matching individual opening/closing legs
                # Track which closing trades we've used to avoid double-matching
                used_closing_tx_ids = set()
                realized_pl = 0
                closed_date = None
                exit_debit = 0

                for leg in spread['legs']:
                    leg_symbol = leg['symbol']
                    leg_side = leg['side']
                    leg_qty = leg['quantity']
                    entry_amount = leg['total_amount']

                    # Find closing trades for this specific leg (same symbol, opposite side, not already used)
                    closing_trades_for_leg = []
                    for t in all_trades:
                        if (t.get('symbol') == leg_symbol and
                            t.get('side') != leg_side and  # Opposite side = closing
                            t['transaction_id'] not in [l['transaction_id'] for l in spread['legs']] and  # Not part of opening
                            t['transaction_id'] not in used_closing_tx_ids):  # Not already matched to another leg
                            closing_trades_for_leg.append(t)

                    # Use the earliest closing trades first (FIFO matching)
                    closing_trades_for_leg.sort(key=lambda x: x['timestamp'])

                    # Match as much quantity as possible
                    remaining_qty = abs(leg_qty)
                    leg_exit = 0

                    for ct in closing_trades_for_leg:
                        if remaining_qty <= 0:
                            break

                        ct_qty = abs(ct['quantity'])
                        match_qty = min(remaining_qty, ct_qty)

                        # Pro-rate the exit amount based on matched quantity
                        exit_amount = ct['total_amount'] * (match_qty / ct_qty)
                        leg_exit += exit_amount

                        # Mark as used (fully or partially)
                        used_closing_tx_ids.add(ct['transaction_id'])
                        remaining_qty -= match_qty

                    if leg_exit != 0:
                        # P&L for this leg:
                        # If opened SELL (credit, positive), P&L = entry + exit (both positive = profit)
                        # If opened BUY (debit, negative), P&L = entry + exit (exit positive reduces loss)
                        leg_pl = entry_amount + leg_exit
                        realized_pl += leg_pl
                        exit_debit += leg_exit

                        # Track latest closing date
                        for ct in closing_trades_for_leg:
                            if closed_date is None or ct['timestamp'] > closed_date:
                                closed_date = ct['timestamp']

                if realized_pl != 0 or exit_debit != 0:
                    # Add rebates/fees from actual transaction data
                    total_rebates_fees = 0

                    # Opening trades rebates/fees (from spread legs)
                    for leg in spread['legs']:
                        total_rebates_fees += leg.get('fee_amount', 0)

                    # Closing trades rebates/fees (from used_closing_tx_ids)
                    for ct in all_trades:
                        if ct['transaction_id'] in used_closing_tx_ids:
                            total_rebates_fees += ct.get('fee_amount', 0)

                    realized_pl += total_rebates_fees
                    print(f"    CLOSED: Entry ${spread['entry_credit']:+.2f} → Exit ${exit_debit:+.2f} + rebates/fees ${total_rebates_fees:.2f} = P&L ${realized_pl:+.2f}")
            else:
                # Calculate unrealized P&L for open spreads using portfolio current values
                # For credit spreads (positive entry): profit = entry - current_value_to_close
                # For debit spreads (negative entry): profit = current_value - entry
                total_current_value = 0
                for leg in spread['legs']:
                    # Find this leg's current value in portfolio
                    leg_symbol = leg['symbol']
                    for pos in portfolio_positions:
                        pos_symbol = pos.get('instrument', {}).get('symbol', '')
                        if pos_symbol:
                            # Strip -OPTION suffix for matching
                            clean_pos_symbol = pos_symbol.replace('-OPTION', '')
                            if clean_pos_symbol == leg_symbol:
                                # currentValue is negative for short positions (credit)
                                # currentValue is positive for long positions (debit)
                                current_value = float(pos.get('currentValue', 0))
                                total_current_value += current_value
                                break

                # For spreads:
                # Entry credit (positive) = received premium, want it to go to 0 (profit)
                # Entry debit (negative) = paid premium, want it to increase (profit)
                # Unrealized P&L = entry_credit - current_value_to_close
                # If entry is +81 (credit) and current is -73 (cost to close), P&L = 81 - 73 = +8
                unrealized_pl = spread['entry_credit'] - abs(total_current_value)

            c.execute('''INSERT OR REPLACE INTO spreads
                (spread_id, spread_type, underlying, expiry, opened_date, closed_date, status, legs, entry_credit, exit_debit, realized_pl, unrealized_pl)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (spread['spread_id'], spread['type'], spread['underlying'], spread['expiry'],
                 spread['opened_date'], closed_date, status, json.dumps([l['transaction_id'] for l in spread['legs']]),
                 spread['entry_credit'], exit_debit, realized_pl, unrealized_pl))

            if status == 'open':
                print(f"  {spread['underlying']} {spread['type']}: OPEN - Entry ${spread['entry_credit']:+.2f}, Unrealized: ${unrealized_pl:+.2f}")
            else:
                print(f"  {spread['underlying']} {spread['type']}: CLOSED - Entry ${spread['entry_credit']:+.2f}, P&L ${realized_pl:+.2f}")

            # Link trades to spread
            for leg_id in spread['leg_ids']:
                c.execute('''UPDATE trades SET spread_id = ? WHERE transaction_id = ?''', (spread['spread_id'], leg_id))

        # Store single legs with unrealized P&L
        # Track which closing trades we've used globally to avoid double-matching
        global_used_closing_tx_ids = set()

        for leg in single_legs:
            leg_id = f"single_{leg['transaction_id']}"
            opt_type_name = 'CALL' if leg['opt_type'] == 'C' else 'PUT'
            leg_type = f"{leg['side']} {opt_type_name}"  # e.g., "BUY CALL" or "SELL PUT"

            # Check if this single leg is still open
            is_open = leg['symbol'] in portfolio_symbols
            status = 'open' if is_open else 'closed'

            # Debug logging
            print(f"  Single leg {leg['underlying']} {leg['opt_type']} @ ${leg['strike']} ({leg['side']}): symbol={leg['symbol']}, is_open={is_open}, in_portfolio={leg['symbol'] in portfolio_symbols}")

            # Calculate unrealized P&L for single leg
            unrealized_pl = 0
            realized_pl = 0
            closed_date = None

            if status == 'closed':
                # Find closing trades for this leg (same symbol, opposite side, not already used)
                closing_trades_for_leg = []
                for t in all_trades:
                    if (t.get('symbol') == leg['symbol'] and
                        t.get('side') != leg['side'] and  # Opposite side = closing
                        t['transaction_id'] != leg['transaction_id'] and  # Not this opening trade
                        t['transaction_id'] not in global_used_closing_tx_ids):  # Not already used
                        closing_trades_for_leg.append(t)

                # Debug: log if no closing trades found
                if not closing_trades_for_leg:
                    # If status is closed but no closing trades found, it likely expired worthless or was assigned
                    # For expired/assigned short options: full premium received is profit
                    # For expired/assigned long options: full premium paid is loss
                    print(f"  DEBUG {leg['underlying']} {leg['opt_type']} @ ${leg['strike']}: No closing trades found for symbol {leg['symbol']} (side: {leg['side']}) - likely expired/assigned")

                    # Calculate P&L assuming expiration/assignment
                    entry_amount = leg['total_amount']
                    if leg['side'] == 'SELL':
                        # Short option expired worthless = keep all premium as profit
                        realized_pl = abs(entry_amount)
                    else:
                        # Long option expired worthless = lose all premium
                        realized_pl = entry_amount  # already negative

                    # Add rebate/fee from the actual opening trade transaction
                    # fee_amount is positive for rebates, negative for fees
                    rebate_or_fee = leg.get('fee_amount', 0)
                    realized_pl += rebate_or_fee

                    # Set closed_date to expiration date
                    closed_date = expiry_to_iso(leg['expiry'])

                    print(f"  {leg['underlying']} {leg['opt_type']} @ ${leg['strike']}: EXPIRED/ASSIGNED - P&L ${realized_pl:+.2f} (incl ${rebate_or_fee:+.2f} rebate/fee), closed_date: {closed_date}")
                else:
                    # Use FIFO matching (earliest trades first)
                    closing_trades_for_leg.sort(key=lambda x: x['timestamp'])

                    print(f"    DEBUG FIFO: Found {len(closing_trades_for_leg)} closing trades for {leg['symbol']} {leg['side']}")
                    for ct in closing_trades_for_leg:
                        print(f"      - {ct['symbol']} {ct['side']} {ct['quantity']} @ ${ct['total_amount']:.2f}")

                    # Match quantities
                    remaining_qty = abs(leg['quantity'])
                    leg_exit = 0
                    last_close_timestamp = None

                    for ct in closing_trades_for_leg:
                        if remaining_qty <= 0:
                            break

                        ct_qty = abs(ct['quantity'])
                        match_qty = min(remaining_qty, ct_qty)

                        # Pro-rate the exit amount
                        exit_amount = ct['total_amount'] * (match_qty / ct_qty)
                        leg_exit += exit_amount
                        last_close_timestamp = ct['timestamp']

                        print(f"      Matched {match_qty} of {ct_qty}: exit_amount=${exit_amount:.2f}, total_exit=${leg_exit:.2f}")

                        # Mark as used
                        global_used_closing_tx_ids.add(ct['transaction_id'])
                        remaining_qty -= match_qty

                    if leg_exit != 0:
                        entry_amount = leg['total_amount']
                        # P&L = entry + exit
                        realized_pl = entry_amount + leg_exit

                        # Add rebates/fees from actual transaction data
                        # Opening trade rebate/fee
                        total_rebates_fees = leg.get('fee_amount', 0)

                        # Closing trades rebates/fees (sum of all matched closing trades)
                        for ct in closing_trades_for_leg:
                            # Pro-rate the rebate/fee based on matched quantity
                            ct_qty = abs(ct['quantity'])
                            ct_rebate_or_fee = ct.get('fee_amount', 0)
                            # The rebate/fee applies to the entire trade, so use full amount if any quantity matched
                            if ct_qty > 0:
                                total_rebates_fees += ct_rebate_or_fee

                        realized_pl += total_rebates_fees

                        # Use the timestamp of the last closing trade
                        closed_date = last_close_timestamp
                        print(f"      Final: entry=${entry_amount:.2f} + exit=${leg_exit:.2f} + rebates/fees=${total_rebates_fees:.2f} = P&L=${realized_pl:.2f}")
            elif status == 'open' and portfolio_positions:
                for pos in portfolio_positions:
                    pos_symbol = pos.get('instrument', {}).get('symbol', '')
                    if pos_symbol:
                        # Strip -OPTION suffix for matching
                        clean_pos_symbol = pos_symbol.replace('-OPTION', '')
                        if clean_pos_symbol == leg['symbol']:
                            # Use currentValue directly from portfolio
                            # For long (BUY): positive currentValue = current market value
                            # For short (SELL): negative currentValue = cost to buy back
                            current_value = float(pos.get('currentValue', 0))
                            entry_value = leg['total_amount']

                            # For long (BUY): P&L = current_value - entry_value
                            # For short (SELL): P&L = entry_value - |current_value|
                            if leg['side'] == 'BUY':
                                unrealized_pl = current_value - entry_value
                            else:
                                # Short position: current value is negative (cost to close)
                                # P&L = entry_credit - cost_to_close
                                unrealized_pl = entry_value - abs(current_value)
                            break

            c.execute('''INSERT OR REPLACE INTO single_legs
                (leg_id, underlying, expiry, strike, opt_type, side, quantity, entry_price, opened_date, closed_date, status, transaction_id, unrealized_pl, realized_pl)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (leg_id, leg['underlying'], leg['expiry'], leg['strike'], leg['opt_type'],
                 leg['side'], leg['quantity'], leg['price'], leg['timestamp'], closed_date, status,
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

    # Get all trades (for stock positions)
    c.execute('''SELECT * FROM trades ORDER BY timestamp DESC''')
    trade_columns = ['id', 'transaction_id', 'symbol', 'underlying', 'expiry', 'strike', 'opt_type', 'side', 'quantity', 'price', 'total_amount', 'timestamp', 'fee_amount', 'spread_id', 'realized_pl']
    all_trades = [dict(zip(trade_columns, row)) for row in c.fetchall()]

    # Fetch portfolio to get current stock positions
    stock_unrealized_pl = 0
    open_stock_positions = []
    try:
        token = get_access_token()
        account_id = get_account_id(token)
        portfolio_response = get(
            f'https://api.public.com/userapigateway/trading/{account_id}/portfolio/v2',
            headers={'Authorization': f'Bearer {token}'}
        )
        portfolio_positions = portfolio_response.json().get('positions', [])

        print(f"DEBUG portfolio: {len(portfolio_positions)} positions for unrealized P&L calculation")

        # Calculate unrealized P&L for stock positions
        for pos in portfolio_positions:
            pos_symbol = pos.get('instrument', {}).get('symbol', '')
            current_value = float(pos.get('currentValue', 0))
            quantity = float(pos.get('quantity', 0))

            # Only process stock positions (not options which end with -OPTION)
            if pos_symbol and not pos_symbol.endswith('-OPTION') and quantity != 0:
                # Try costBasis from portfolio API first (most accurate for long-term holds)
                cost_basis = pos.get('costBasis', {})
                if cost_basis and cost_basis.get('totalCost'):
                    entry_value = float(cost_basis['totalCost'])
                    unrealized_pl = current_value - entry_value
                    stock_unrealized_pl += unrealized_pl
                    open_stock_positions.append({
                        'symbol': pos_symbol,
                        'quantity': abs(quantity),
                        'entry_value': entry_value,
                        'current_value': current_value,
                        'unrealized_pl': unrealized_pl
                    })
                    print(f"  Stock {pos_symbol}: {abs(quantity)} shares, entry=${entry_value:.2f}, current=${current_value:.2f}, P&L=${unrealized_pl:+.2f}")
                else:
                    # Fallback: calculate from trades in database
                    stock_trades = [t for t in all_trades if t['symbol'] == pos_symbol and t['opt_type'] == 'STOCK']
                    if stock_trades:
                        # Calculate weighted average entry price
                        total_qty = 0
                        total_cost = 0
                        for t in stock_trades:
                            qty = t['quantity']
                            cost = abs(t['total_amount'])  # Total cost for this trade
                            if t['side'] == 'BUY':
                                total_qty += qty
                                total_cost += cost
                            else:  # SELL
                                total_qty -= qty
                                total_cost -= cost

                        if total_qty != 0:
                            avg_entry_price = total_cost / total_qty
                            # For stocks: unrealized_pl = (current_price - entry_price) * quantity
                            # currentValue is already current_price * quantity
                            entry_value = avg_entry_price * total_qty
                            unrealized_pl = current_value - entry_value
                            stock_unrealized_pl += unrealized_pl
                            open_stock_positions.append({
                                'symbol': pos_symbol,
                                'quantity': total_qty,
                                'entry_value': entry_value,
                                'current_value': current_value,
                                'unrealized_pl': unrealized_pl
                            })
                            print(f"  Stock {pos_symbol} (from trades): {total_qty} shares, entry=${entry_value:.2f}, current=${current_value:.2f}, P&L=${unrealized_pl:+.2f}")
    except Exception as e:
        print(f"DEBUG: Error fetching portfolio for unrealized P&L: {e}")

    # Calculate stats
    closed_spreads = [s for s in spreads if s['status'] == 'closed']
    open_spreads = [s for s in spreads if s['status'] == 'open']

    open_single_legs = [l for l in single_legs if l['status'] == 'open']
    closed_single_legs = [l for l in single_legs if l['status'] == 'closed']

    # Calculate realized P&L using pre-calculated values from spreads and single_legs tables
    # The update_data() function already correctly calculates realized_pl using FIFO matching
    spreads_realized_pl = sum(s['realized_pl'] for s in closed_spreads)

    # For single options: use the pre-calculated realized_pl from the single_legs table
    # This includes FIFO-matched closed positions and expired/assigned positions
    options_realized_pl = sum(l['realized_pl'] for l in closed_single_legs)

    total_realized_pl = spreads_realized_pl + options_realized_pl

    # Calculate MTD/YTD for single options using closed_date from single_legs table
    now = datetime.now()
    month_start = datetime(now.year, now.month, 1)
    year_start = datetime(now.year, 1, 1)

    options_mtd_pl = 0
    options_ytd_pl = 0
    options_mtd_count = 0
    options_ytd_count = 0

    for leg in closed_single_legs:
        if leg.get('closed_date'):
            try:
                closed_dt = datetime.fromisoformat(leg['closed_date'].replace('Z', '+00:00'))
                closed_dt_naive = closed_dt.replace(tzinfo=None)

                if closed_dt_naive >= month_start:
                    options_mtd_pl += leg['realized_pl']
                    options_mtd_count += 1
                if closed_dt_naive >= year_start:
                    options_ytd_pl += leg['realized_pl']
                    options_ytd_count += 1
            except:
                # If closed_date parsing fails, include in both
                options_mtd_pl += leg['realized_pl']
                options_ytd_pl += leg['realized_pl']
                options_mtd_count += 1
                options_ytd_count += 1

    options_unrealized_pl = sum(s.get('unrealized_pl', 0) for s in open_spreads) + sum(l.get('unrealized_pl', 0) for l in open_single_legs)
    total_unrealized_pl = options_unrealized_pl + stock_unrealized_pl

    print(f"DEBUG get_stats: Found {len(closed_spreads)} closed spreads, {len(closed_single_legs)} closed single legs")
    print(f"DEBUG get_stats: Closed spreads P&L:")
    for s in closed_spreads:
        print(f"  {s['underlying']} {s.get('spread_type', 'N/A')}: ${s['realized_pl']:+.2f}")
    print(f"DEBUG get_stats: Closed single legs P&L (sum=${options_realized_pl:+.2f}):")
    for leg in closed_single_legs:
        print(f"  {leg['underlying']} {leg['opt_type']} @ ${leg['strike']}: ${leg['realized_pl']:+.2f}")
    print(f"DEBUG get_stats: total_realized_pl = ${total_realized_pl:+.2f} (spreads: ${spreads_realized_pl:+.2f} + options: ${options_realized_pl:+.2f})")
    print(f"DEBUG get_stats: total_unrealized_pl = ${total_unrealized_pl:+.2f} (options: ${options_unrealized_pl:+.2f} + stocks: ${stock_unrealized_pl:+.2f})")

    # MTD (Month-to-Date) realized P&L
    now = datetime.now()
    month_start = datetime(now.year, now.month, 1)

    # MTD for spreads
    mtd_spreads = []
    for s in closed_spreads:
        if s.get('closed_date'):
            try:
                closed_dt = datetime.fromisoformat(s['closed_date'].replace('Z', '+00:00'))
                if closed_dt.replace(tzinfo=None) >= month_start:
                    mtd_spreads.append(s)
            except:
                pass

    mtd_spreads_pl = sum(s['realized_pl'] for s in mtd_spreads)
    mtd_realized_pl = mtd_spreads_pl + options_mtd_pl

    # YTD (Year-to-Date) realized P&L
    year_start = datetime(now.year, 1, 1)

    # YTD for spreads
    ytd_spreads = []
    for s in closed_spreads:
        if s.get('closed_date'):
            try:
                closed_dt = datetime.fromisoformat(s['closed_date'].replace('Z', '+00:00'))
                if closed_dt.replace(tzinfo=None) >= year_start:
                    ytd_spreads.append(s)
            except:
                pass

    ytd_spreads_pl = sum(s['realized_pl'] for s in ytd_spreads)
    ytd_realized_pl = ytd_spreads_pl + options_ytd_pl

    conn.close()

    return {
        'total_spreads': len(spreads),
        'open_spreads': len(open_spreads),
        'closed_spreads': len(closed_spreads),
        'total_single_legs': len(single_legs),
        'open_single_legs': len(open_single_legs),
        'closed_single_legs': len(closed_single_legs),
        'open_stocks': len(open_stock_positions),
        'total_realized_pl': total_realized_pl,
        'total_unrealized_pl': total_unrealized_pl,
        'mtd_realized_pl': mtd_realized_pl,
        'ytd_realized_pl': ytd_realized_pl,
        'mtd_closed': len(mtd_spreads) + options_mtd_count,
        'ytd_closed': len(ytd_spreads) + options_ytd_count,
        'spreads': spreads[:50],
        'single_legs': single_legs[:50],
        'stock_positions': open_stock_positions
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
