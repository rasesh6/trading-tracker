"""
Trading Tracker - Simple History-based P&L Tracker

Uses Public.com History API for exact P&L calculation.
- Sums netAmount from all "Completed" trades
- No FIFO matching, no grouping - just add up the numbers
"""

import os
import json
import re
from datetime import datetime, timedelta
from flask import Flask, jsonify, send_file, request
from requests import post, get

app = Flask(__name__)

# Cache
_history_cache = None
_cache_time = None

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

def calculate_pl_from_history(start_date=None, end_date=None):
    """Calculate P&L for given date range (or YTD if not specified)"""
    global _history_cache, _cache_time

    # Cache only for default YTD call
    if start_date is None and end_date is None:
        if _history_cache and _cache_time:
            age = (datetime.now() - _cache_time).total_seconds()
            if age < 300:
                return _history_cache

    try:
        token = get_access_token()
        account_id = get_account_id(token)

        now = datetime.now()
        if start_date is None:
            start_date = datetime(now.year, 1, 1).strftime('%Y-%m-%dT%H:%M:%SZ')
        if end_date is None:
            end_date = now.strftime('%Y-%m-%dT%H:%M:%SZ')

        history = fetch_order_history(token, account_id, start_date, end_date)
        transactions = history.get('transactions', [])

        # Fetch Portfolio API to identify currently OPEN positions
        portfolio_response = get(
            f'https://api.public.com/userapigateway/trading/{account_id}/portfolio',
            headers={'Authorization': f'Bearer {token}'}
        )
        portfolio = portfolio_response.json()

        # Extract currently open option positions from Portfolio
        open_in_portfolio = set()
        if 'positions' in portfolio:
            for pos in portfolio['positions']:
                symbol = pos.get('symbol', '')
                if re.match(r'[A-Z]+2\d{2}\d{3}[CP]\d{8}', symbol):
                    open_in_portfolio.add(symbol)

        # Separate options (group by underlying/expiry) and stocks (FIFO)
        option_trades = {}
        stock_trades = []

        for tx in transactions:
            tx_type = tx.get('type', '')
            sub_type = tx.get('subType', '')

            if tx_type != 'TRADE' or sub_type != 'TRADE':
                continue

            net_amount = float(tx.get('netAmount') or 0)
            description = tx.get('description', '')
            timestamp = tx.get('timestamp', '')

            match = re.search(r'([A-Z]+2\d{2}\d{3}[CP]\d{8})', description)
            if match:
                contract = match.group(1)
                # Extract underlying and expiry for grouping
                m2 = re.match(r'([A-Z]+)(\d{6})([CP])(\d{8})', contract)
                if m2:
                    key = f"{m2.group(1)}_{m2.group(2)}"  # underlying_expiry
                else:
                    key = contract

                if key not in option_trades:
                    option_trades[key] = {'buy': 0, 'sell': 0, 'transactions': [], 'any_in_portfolio': False}

                if 'BUY' in description:
                    option_trades[key]['buy'] += net_amount
                else:
                    option_trades[key]['sell'] += net_amount

                option_trades[key]['transactions'].append({
                    'description': description,
                    'netAmount': net_amount,
                    'timestamp': timestamp
                })
            else:
                parts = description.split()
                if len(parts) >= 3:
                    side = 'BUY' if 'BUY' in description else 'SELL'
                    try:
                        qty = int(parts[1])
                    except (ValueError, IndexError):
                        qty = 0
                    symbol = parts[2]
                    stock_trades.append({
                        'symbol': symbol,
                        'side': side,
                        'quantity': qty,
                        'amount': net_amount,
                        'timestamp': timestamp,
                        'description': description
                    })

        # Check which option groups have any contract still in portfolio
        for key, data in option_trades.items():
            # Check if any contract in this group is in portfolio
            data['any_in_portfolio'] = False

        # Re-check portfolio more carefully - extract full option contracts
        # Also check for STOCK positions that might be from assignment
        stock_symbols_in_portfolio = set()
        if 'positions' in portfolio:
            for pos in portfolio['positions']:
                instrument = pos.get('instrument', {})
                symbol = instrument.get('symbol', '')
                # Check if it's a stock (not an option)
                if not re.match(r'[A-Z]+2\d{2}\d{3}[CP]\d{8}', symbol):
                    stock_symbols_in_portfolio.add(symbol)

        if 'positions' in portfolio:
            for pos in portfolio['positions']:
                instrument = pos.get('instrument', {})
                symbol = instrument.get('symbol', '')
                # Check each option trade against portfolio
                for key, data in option_trades.items():
                    for tx in data['transactions']:
                        # Extract contract from transaction description
                        tx_match = re.search(r'([A-Z]+2\d{2}\d{3}[CP]\d{8})', tx['description'])
                        if tx_match and tx_match.group(1) == symbol:
                            data['any_in_portfolio'] = True
                            break

        # Check for sell-only options that might have been assigned to stock
        for key, data in option_trades.items():
            has_buy = data['buy'] != 0
            has_sell = data['sell'] != 0

            # If it's sell-only and the underlying stock is in portfolio, it was likely assigned
            if has_sell and not has_buy:
                # Extract underlying symbol from key
                underlying = key.split('_')[0] if '_' in key else key[:key.index('2')]
                if underlying in stock_symbols_in_portfolio:
                    data['any_in_portfolio'] = True  # Mark as "open" since stock position is open

        # NO HARDCODED POSITIONS - rely entirely on portfolio API check above
        # The portfolio check correctly identifies which positions are still open

        # Calculate options P&L (realized only)
        options_pl = 0
        completed_transactions = []
        closed_option_positions = 0
        open_option_positions = 0

        for key, data in option_trades.items():
            has_buy = data['buy'] != 0
            has_sell = data['sell'] != 0
            is_in_portfolio = data['any_in_portfolio']

            # Count as CLOSED if:
            # 1. Both buy and sell in YTD, OR
            # 2. Only buy or only sell BUT NOT in portfolio AND not assigned to stock
            if not is_in_portfolio:
                # Position is closed (both sides in YTD, or expired/assignment)
                options_pl += data['buy'] + data['sell']
                completed_transactions.extend(data['transactions'])
                if has_buy or has_sell:
                    closed_option_positions += 1
            else:
                # Still open in portfolio (or assigned to stock that's still open)
                open_option_positions += 1

        # Calculate unrealized P&L from portfolio API (using cost basis)
        total_unrealized_pl = 0
        if 'positions' in portfolio:
            for pos in portfolio['positions']:
                current_value = float(pos.get('currentValue', 0))
                cost_basis_dict = pos.get('costBasis', {})

                # Parse cost basis (it's a string representation of dict)
                if isinstance(cost_basis_dict, str):
                    import ast
                    try:
                        cost_basis_dict = ast.literal_eval(cost_basis_dict)
                    except:
                        cost_basis_dict = {}

                total_cost = float(cost_basis_dict.get('totalCost', 0))

                # Unrealized P&L = current value - cost basis
                # For short positions (negative quantity): cost is negative, so (value - cost) works correctly
                unrealized = current_value - total_cost
                total_unrealized_pl += unrealized

        # Calculate stock P&L using FIFO matching
        # Track assignment cost basis adjustments from short option assignments
        # Format: {symbol: {quantity: shares, premium_total: float, strike: float, premium_per_share: float}}
        assignment_adjustments = {}

        # Analyze sell-only option groups to detect potential assignments (only in YTD)
        for key, data in option_trades.items():
            has_buy = data['buy'] != 0
            has_sell = data['sell'] != 0

            # If sell-only (short option), it may have been assigned
            if has_sell and not has_buy:
                # Parse contract details from transactions
                for tx in data['transactions']:
                    desc = tx['description']
                    # Parse format: "SELL 20 SOXL260130P00065000 at 0.65"
                    # Extract: quantity(20), symbol(SOXL260130P00065000), price(0.65)
                    parts = desc.split()
                    if len(parts) >= 4 and parts[0] == 'SELL':
                        try:
                            qty = int(parts[1])
                            option_symbol = parts[2]
                            price_str = parts[4].replace('$', '').replace(',', '')
                            price = float(price_str)

                            # Parse option symbol to get underlying and strike
                            # Format: UNDERLYING(2)YYMMDD(C/P)STRIKE*1000
                            m = re.match(r'([A-Z]+)2(\d{2})(\d{2})(\d{2})([CP])(\d{6})', option_symbol)
                            if m:
                                underlying = m.group(1)
                                strike = int(m.group(6)) / 1000  # Convert from cents
                                contracts = qty
                                shares = contracts * 100
                                premium = price * shares

                                # Store assignment data
                                if underlying not in assignment_adjustments:
                                    assignment_adjustments[underlying] = {
                                        'quantity': 0,
                                        'premium_total': 0,
                                        'strike': strike,
                                        'premium_per_share': 0
                                    }

                                assignment_adjustments[underlying]['quantity'] += shares
                                assignment_adjustments[underlying]['premium_total'] += premium
                        except (ValueError, IndexError):
                            continue

        # Calculate premium per share for each assignment
        for symbol in assignment_adjustments:
            adj = assignment_adjustments[symbol]
            if adj['quantity'] > 0:
                adj['premium_per_share'] = adj['premium_total'] / adj['quantity']

        # Build a map of portfolio cost basis for stock positions
        # This helps detect assignments that happened before YTD
        portfolio_cost_basis = {}
        for pos in portfolio.get('positions', []):
            instrument = pos.get('instrument', {})
            inst_type = instrument.get('type', '')
            if inst_type == 'EQUITY':
                symbol = instrument.get('symbol', '')
                cost_basis_dict = pos.get('costBasis', {})
                if isinstance(cost_basis_dict, str):
                    import ast
                    try:
                        cost_basis_dict = ast.literal_eval(cost_basis_dict)
                    except:
                        cost_basis_dict = {}

                total_cost = float(cost_basis_dict.get('totalCost', 0))
                quantity = float(pos.get('quantity', 0))

                if quantity > 0 and total_cost > 0:
                    portfolio_cost_basis[symbol] = {
                        'total_cost': total_cost,
                        'quantity': quantity,
                        'cost_per_share': total_cost / quantity
                    }

        stock_trades.sort(key=lambda x: x['timestamp'])
        stock_positions = {}
        stocks_pl = 0
        closed_stock_positions = 0
        open_stock_positions = 0

        for trade in stock_trades:
            symbol = trade['symbol']
            if symbol not in stock_positions:
                stock_positions[symbol] = []

            if trade['side'] == 'BUY':
                # Check if this BUY matches an assignment (quantity matches exactly)
                amount = trade['amount']
                original_amount = amount
                cost_basis_from_portfolio = None

                # First, check YTD assignment adjustments
                if symbol in assignment_adjustments:
                    adj = assignment_adjustments[symbol]
                    # Match by exact quantity (assignment creates specific share count)
                    if trade['quantity'] == adj['quantity']:
                        # Adjust cost basis: original cost - premium received
                        # This prevents double-counting the option premium
                        cost_adjustment = adj['premium_total']
                        amount = amount - cost_adjustment
                        cost_basis_from_portfolio = 'ytd_assignment'

                # Second, check if this BUY matches a portfolio position (pre-YTD assignment)
                if cost_basis_from_portfolio is None and symbol in portfolio_cost_basis:
                    pf = portfolio_cost_basis[symbol]
                    # Check if the quantity matches exactly (assignment creates specific lot)
                    if trade['quantity'] == pf['quantity']:
                        # Use portfolio cost basis instead of transaction amount
                        # The portfolio already includes the assignment adjustment
                        amount = pf['total_cost']
                        cost_basis_from_portfolio = 'portfolio'

                stock_positions[symbol].append({
                    'quantity': trade['quantity'],
                    'amount': amount,
                    'original_amount': original_amount,
                    'description': trade['description'],
                    'timestamp': trade['timestamp'],
                    'side': 'BUY',  # These are BUY positions for FIFO matching
                    'cost_basis_source': cost_basis_from_portfolio,
                    'original_quantity': trade['quantity']  # Preserve for completed_transactions
                })
            else:
                remaining_qty = trade['quantity']
                sell_amount_per_share = abs(trade['amount'] / trade['quantity'])

                while remaining_qty > 0 and stock_positions[symbol]:
                    buy_trade = stock_positions[symbol][0]
                    match_qty = min(remaining_qty, buy_trade['quantity'])
                    buy_amount_per_share = abs(buy_trade['amount'] / buy_trade['quantity'])
                    match_pl = (sell_amount_per_share - buy_amount_per_share) * match_qty
                    stocks_pl += match_pl

                    # Add synthetic P&L transaction with closing date for chart
                    # This ensures stock P&L is included in cumulative chart
                    completed_transactions.append({
                        'netAmount': match_pl,
                        'description': f'Stock P&L: {symbol} {match_qty} shares',
                        'timestamp': trade['timestamp'],  # Closing date (SELL date)
                        'type': 'stock_pnl',
                        'symbol': symbol
                    })

                    remaining_qty -= match_qty
                    buy_trade['quantity'] -= match_qty

                    if buy_trade['quantity'] == 0:
                        stock_positions[symbol].pop(0)
                        closed_stock_positions += 1

                if remaining_qty > 0:
                    stock_positions[symbol].append({
                        'quantity': remaining_qty,
                        'amount': 0,
                        'description': f"SHORT {remaining_qty} {symbol}",
                        'timestamp': trade['timestamp']
                    })

        for symbol, queue in stock_positions.items():
            open_stock_positions += len(queue)

        # Count ALL open positions from portfolio (including those opened before YTD)
        # Portfolio already has current open positions regardless of when they were opened
        portfolio_open_count = 0
        if 'positions' in portfolio:
            # Group option positions by underlying_expiry to count spreads as 1
            portfolio_option_groups = set()
            portfolio_stock_count = 0

            for pos in portfolio['positions']:
                instrument = pos.get('instrument', {})
                symbol = instrument.get('symbol', '')
                inst_type = instrument.get('type', '')

                if inst_type == 'OPTION':
                    # Group by underlying_expiry (e.g., "WMT_260206")
                    m = re.match(r'([A-Z]+)(\d{6})([CP])(\d{8})', symbol)
                    if m:
                        group_key = f'{m.group(1)}_{m.group(2)}'
                        portfolio_option_groups.add(group_key)
                elif inst_type == 'EQUITY':
                    # Stock position
                    portfolio_stock_count += 1

            portfolio_open_count = len(portfolio_option_groups) + portfolio_stock_count

        total_realized_pl = options_pl + stocks_pl
        # total_unrealized_pl already calculated from portfolio API above

        result = {
            'total_realized_pl': total_realized_pl,
            'short_term_pl': total_realized_pl,
            'long_term_pl': 0,
            'total_unrealized_pl': total_unrealized_pl,
            'total_positions': closed_option_positions + closed_stock_positions,
            'open_positions': portfolio_open_count,  # Use actual portfolio count
            'transactions': completed_transactions,
            'last_updated': now.isoformat()
        }

        _history_cache = result
        _cache_time = datetime.now()

        return result

    except Exception as e:
        import traceback
        return {
            'error': str(e),
            'traceback': traceback.format_exc()
        }

def get_stats():
    """Get trading statistics with separate MTD and YTD calculations"""
    # Get YTD data (Jan 1 to now) - this has all completed transactions
    ytd_data = calculate_pl_from_history()

    if 'error' in ytd_data:
        return ytd_data

    # Calculate MTD based on positions that CLOSED in the current month
    # not transactions that occurred in the current month
    now = datetime.now()
    current_month = now.month
    current_year = now.year

    # Group completed transactions by position to find closing dates
    # For options: group by underlying/expiry (key format: "UNDERLYING_YYMMDD")
    # For stocks: FIFO pairs are already matched in completed_transactions
    mtd_realized_pl = 0
    mtd_closed = 0

    transactions = ytd_data.get('transactions', [])

    # Group transactions to identify positions and their closing dates
    option_positions = {}  # key -> {transactions: [], closing_date: None}
    stock_positions = {}   # symbol -> {transactions: [], closing_date: None}

    for tx in transactions:
        desc = tx.get('description', '')
        timestamp = tx.get('timestamp', '')
        net_amount = tx.get('netAmount', 0)

        # Check if it's an option transaction
        option_match = re.search(r'([A-Z]+2\d{2}\d{3}[CP]\d{8})', desc)
        if option_match:
            # Extract underlying and expiry for grouping
            m = re.match(r'([A-Z]+)(\d{6})([CP])(\d{8})', option_match.group(1))
            if m:
                key = f"{m.group(1)}_{m.group(2)}"  # underlying_expiry
            else:
                key = option_match.group(1)

            if key not in option_positions:
                option_positions[key] = {'transactions': [], 'total_pl': 0}
            option_positions[key]['transactions'].append(tx)
            option_positions[key]['total_pl'] += net_amount
            # Update closing date (latest timestamp for this position)
            if timestamp:
                if option_positions[key].get('closing_date') is None or timestamp > option_positions[key]['closing_date']:
                    option_positions[key]['closing_date'] = timestamp
        else:
            # Stock transaction - group by symbol
            # Extract symbol from description (e.g., "BUY 100 SOXL")
            parts = desc.split()
            if len(parts) >= 3:
                symbol = parts[2]
                if symbol not in stock_positions:
                    stock_positions[symbol] = {'transactions': [], 'total_pl': 0, 'trade_count': 0}
                stock_positions[symbol]['transactions'].append(tx)
                stock_positions[symbol]['total_pl'] += net_amount
                stock_positions[symbol]['trade_count'] += 1
                # Update closing date (latest timestamp for this symbol)
                if timestamp:
                    if stock_positions[symbol].get('closing_date') is None or timestamp > stock_positions[symbol]['closing_date']:
                        stock_positions[symbol]['closing_date'] = timestamp

    # Sum P&L for positions that closed in current month
    # For options: each key is a position
    for key, pos_data in option_positions.items():
        closing_date = pos_data.get('closing_date')
        if closing_date:
            try:
                # Parse timestamp like "2026-02-02T15:30:00Z"
                dt = datetime.fromisoformat(closing_date.replace('Z', '+00:00'))
                if dt.month == current_month and dt.year == current_year:
                    mtd_realized_pl += pos_data['total_pl']
                    mtd_closed += 1
            except:
                pass

    # For stocks: each FIFO pair closed in current month
    # Stocks are matched as BUY+SELL pairs, so we count positions closed
    for symbol, pos_data in stock_positions.items():
        closing_date = pos_data.get('closing_date')
        if closing_date:
            try:
                dt = datetime.fromisoformat(closing_date.replace('Z', '+00:00'))
                if dt.month == current_month and dt.year == current_year:
                    # For stocks, trade_count should be even (BUY+SELL pairs)
                    # Each pair is one position
                    pairs = pos_data['trade_count'] // 2
                    mtd_realized_pl += pos_data['total_pl']
                    mtd_closed += pairs
            except:
                pass

    ytd_realized_pl = ytd_data['total_realized_pl']

    # Return combined stats (use YTD for transactions, portfolio counts, etc.)
    ytd_data.update({
        'mtd_realized_pl': mtd_realized_pl,
        'mtd_short_term': mtd_realized_pl,
        'mtd_long_term': 0,
        'mtd_closed': mtd_closed,
        'ytd_realized_pl': ytd_realized_pl,
        'ytd_short_term': ytd_realized_pl,
        'ytd_long_term': 0,
        'ytd_closed': ytd_data['total_positions']
    })

    return ytd_data

def get_trades(days=7):
    """Get recent transactions"""
    data = calculate_pl_from_history()
    if 'transactions' in data:
        return data['transactions'][:50]
    return []

# API Routes
@app.route('/')
def index():
    return send_file('dashboard.html')

@app.route('/api/stats')
def stats():
    """Get trading statistics"""
    return jsonify(get_stats())

@app.route('/api/trades')
def trades():
    """Get transactions"""
    days = int(request.args.get('days', 7))
    return jsonify(get_trades(days))

@app.route('/api/update')
def update():
    """Force refresh"""
    global _history_cache, _cache_time
    _history_cache = None
    _cache_time = None
    return jsonify(calculate_pl_from_history())

@app.route('/api/reset')
def reset():
    """Reset cache"""
    global _history_cache, _cache_time
    _history_cache = None
    _cache_time = None
    return jsonify({'status': 'reset'})

@app.route('/api/health')
def health():
    """Health check"""
    return jsonify({
        'status': 'ok',
        'timestamp': datetime.now().isoformat(),
        'version': '3.11 (FIX: Stock P&L now included in cumulative chart via synthetic transactions)'
    })

@app.route('/api/debug/stock_trades')
def debug_stock_trades():
    """Debug endpoint to trace through stock FIFO matching with assignment adjustments"""
    try:
        token = get_access_token()
        account_id = get_account_id(token)

        now = datetime.now()
        year_start = datetime(now.year, 1, 1).strftime('%Y-%m-%dT%H:%M:%SZ')
        end_date = now.strftime('%Y-%m-%dT%H:%M:%SZ')

        history = fetch_order_history(token, account_id, year_start, end_date)
        transactions = history.get('transactions', [])

        # Get portfolio
        portfolio_response = get(
            f'https://api.public.com/userapigateway/trading/{account_id}/portfolio',
            headers={'Authorization': f'Bearer {token}'}
        )
        portfolio = portfolio_response.json()

        # Check for stock symbols in portfolio
        stock_symbols_in_portfolio = set()
        if 'positions' in portfolio:
            for pos in portfolio['positions']:
                instrument = pos.get('instrument', {})
                symbol = instrument.get('symbol', '')
                inst_type = instrument.get('type', '')
                if inst_type == 'EQUITY':
                    stock_symbols_in_portfolio.add(symbol)

        # Parse option trades to find assignments
        option_trades = {}
        for tx in transactions:
            tx_type = tx.get('type', '')
            sub_type = tx.get('subType', '')
            if tx_type != 'TRADE' or sub_type != 'TRADE':
                continue

            net_amount = float(tx.get('netAmount') or 0)
            description = tx.get('description', '')
            timestamp = tx.get('timestamp', '')

            match = re.search(r'([A-Z]+2\d{2}\d{3}[CP]\d{8})', description)
            if match:
                contract = match.group(1)
                m2 = re.match(r'([A-Z]+)(\d{6})([CP])(\d{8})', contract)
                if m2:
                    key = f"{m2.group(1)}_{m2.group(2)}"
                else:
                    key = contract

                if key not in option_trades:
                    option_trades[key] = {'buy': 0, 'sell': 0, 'transactions': []}

                if 'BUY' in description:
                    option_trades[key]['buy'] += net_amount
                else:
                    option_trades[key]['sell'] += net_amount

                option_trades[key]['transactions'].append({
                    'description': description,
                    'netAmount': net_amount,
                    'timestamp': timestamp
                })

        # Detect assignment adjustments
        assignment_adjustments = {}
        for key, data in option_trades.items():
            has_buy = data['buy'] != 0
            has_sell = data['sell'] != 0

            if has_sell and not has_buy:
                for tx in data['transactions']:
                    desc = tx['description']
                    parts = desc.split()
                    if len(parts) >= 4 and parts[0] == 'SELL':
                        try:
                            qty = int(parts[1])
                            option_symbol = parts[2]
                            price_str = parts[4].replace('$', '').replace(',', '')
                            price = float(price_str)

                            m = re.match(r'([A-Z]+)2(\d{2})(\d{2})(\d{2})([CP])(\d{6})', option_symbol)
                            if m:
                                underlying = m.group(1)
                                strike = int(m.group(6)) / 1000
                                contracts = qty
                                shares = contracts * 100
                                premium = price * shares

                                if underlying not in assignment_adjustments:
                                    assignment_adjustments[underlying] = {
                                        'quantity': 0,
                                        'premium_total': 0,
                                        'strike': strike,
                                        'premium_per_share': 0,
                                        'source_tx': desc
                                    }

                                assignment_adjustments[underlying]['quantity'] += shares
                                assignment_adjustments[underlying]['premium_total'] += premium
                        except (ValueError, IndexError) as e:
                            continue

        # Calculate premium per share
        for symbol in assignment_adjustments:
            adj = assignment_adjustments[symbol]
            if adj['quantity'] > 0:
                adj['premium_per_share'] = adj['premium_total'] / adj['quantity']

        # Parse stock trades
        stock_trades = []
        for tx in transactions:
            tx_type = tx.get('type', '')
            sub_type = tx.get('subType', '')
            if tx_type != 'TRADE' or sub_type != 'TRADE':
                continue

            net_amount = float(tx.get('netAmount') or 0)
            description = tx.get('description', '')
            timestamp = tx.get('timestamp', '')

            # Skip options
            if re.search(r'([A-Z]+2\d{2}\d{3}[CP]\d{8})', description):
                continue

            parts = description.split()
            if len(parts) >= 3 and ('BUY' in description or 'SELL' in description):
                side = 'BUY' if 'BUY' in description else 'SELL'
                try:
                    qty = int(parts[1])
                except:
                    continue
                symbol = parts[2]

                amount = net_amount
                original_amount = amount
                cost_adjustment = 0
                adjusted = False

                # Apply assignment adjustment
                if side == 'BUY' and symbol in assignment_adjustments:
                    adj = assignment_adjustments[symbol]
                    if qty == adj['quantity']:
                        cost_adjustment = adj['premium_total']
                        amount = amount - cost_adjustment
                        adjusted = True

                stock_trades.append({
                    'symbol': symbol,
                    'side': side,
                    'quantity': qty,
                    'amount': amount,
                    'original_amount': original_amount,
                    'cost_adjustment': cost_adjustment,
                    'adjusted': adjusted,
                    'timestamp': timestamp,
                    'description': description
                })

        # Sort by timestamp
        stock_trades.sort(key=lambda x: x['timestamp'])

        # FIFO matching
        stock_positions = {}
        fifo_log = []
        stocks_pl = 0

        for trade in stock_trades:
            symbol = trade['symbol']
            if symbol not in stock_positions:
                stock_positions[symbol] = []

            log_entry = {
                'trade': trade,
                'action': 'added_to_queue' if trade['side'] == 'BUY' else 'matching',
                'before_queue': len(stock_positions.get(symbol, [])),
                'matches': []
            }

            if trade['side'] == 'BUY':
                stock_positions[symbol].append(trade)
                log_entry['after_queue'] = len(stock_positions[symbol])
            else:
                remaining_qty = trade['quantity']
                sell_price = abs(trade['amount'] / trade['quantity']) if trade['quantity'] > 0 else 0

                while remaining_qty > 0 and stock_positions[symbol]:
                    buy_trade = stock_positions[symbol][0]
                    match_qty = min(remaining_qty, buy_trade['quantity'])
                    buy_price = abs(buy_trade['amount'] / buy_trade['quantity']) if buy_trade['quantity'] > 0 else 0
                    match_pl = (sell_price - buy_price) * match_qty
                    stocks_pl += match_pl

                    log_entry['matches'].append({
                        'match_qty': match_qty,
                        'sell_price': sell_price,
                        'buy_price': buy_price,
                        'match_pl': match_pl,
                        'buy_description': buy_trade['description']
                    })

                    remaining_qty -= match_qty
                    buy_trade['quantity'] -= match_qty

                    if buy_trade['quantity'] == 0:
                        stock_positions[symbol].pop(0)

                if remaining_qty > 0:
                    log_entry['unmatched'] = remaining_qty

            log_entry['after_queue'] = len(stock_positions.get(symbol, []))
            fifo_log.append(log_entry)

        # Show remaining open positions
        open_positions = {}
        for symbol, queue in stock_positions.items():
            if queue:
                open_positions[symbol] = [
                    {
                        'quantity': t['quantity'],
                        'amount': t['amount'],
                        'original_amount': t.get('original_amount', t['amount']),
                        'cost_adjustment': t.get('cost_adjustment', 0),
                        'description': t['description']
                    }
                    for t in queue
                ]

        return jsonify({
            'assignment_adjustments': assignment_adjustments,
            'stock_symbols_in_portfolio': list(stock_symbols_in_portfolio),
            'stock_trades': stock_trades,
            'fifo_log': fifo_log,
            'open_positions': open_positions,
            'stocks_pl': stocks_pl
        })

    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()})

@app.route('/api/debug/all_positions')
def debug_all_positions():
    """Debug endpoint to show all positions and check Portfolio API for open positions"""
    try:
        token = get_access_token()
        account_id = get_account_id(token)

        now = datetime.now()
        year_start = datetime(now.year, 1, 1).strftime('%Y-%m-%dT%H:%M:%SZ')
        end_date = now.strftime('%Y-%m-%dT%H:%M:%SZ')

        # Fetch History API (YTD transactions)
        history = fetch_order_history(token, account_id, year_start, end_date)
        transactions = history.get('transactions', [])

        # Fetch Portfolio API (current open positions)
        portfolio_response = get(
            f'https://api.public.com/userapigateway/trading/{account_id}/portfolio',
            headers={'Authorization': f'Bearer {token}'}
        )
        portfolio = portfolio_response.json()

        # Extract currently open option positions from Portfolio
        open_in_portfolio = set()
        if 'positions' in portfolio:
            for pos in portfolio['positions']:
                symbol = pos.get('symbol', '')
                # Option symbols have format like "NVDA260123P00175000"
                if re.match(r'[A-Z]+2\d{2}\d{3}[CP]\d{8}', symbol):
                    open_in_portfolio.add(symbol)

        # Group all trades by contract
        all_trades = {}
        for tx in transactions:
            tx_type = tx.get('type', '')
            sub_type = tx.get('subType', '')

            if tx_type != 'TRADE' or sub_type != 'TRADE':
                continue

            net_amount = float(tx.get('netAmount') or 0)
            description = tx.get('description', '')

            # Try to match any option (not just 260)
            match = re.search(r'([A-Z]+2\d{2}\d{3}[CP]\d{8})', description)
            if match:
                contract = match.group(1)  # Option contract
            else:
                # Stock symbol
                parts = description.split()
                contract = parts[2] if len(parts) > 2 else 'UNKNOWN'

            if contract not in all_trades:
                all_trades[contract] = {'buy': 0, 'sell': 0, 'count': 0, 'sample': '', 'in_portfolio': contract in open_in_portfolio}

            if 'BUY' in description:
                all_trades[contract]['buy'] += net_amount
            else:
                all_trades[contract]['sell'] += net_amount

            all_trades[contract]['count'] += 1
            all_trades[contract]['sample'] = description

        # Categorize
        closed = {k: v for k, v in all_trades.items() if v['buy'] != 0 and v['sell'] != 0}
        only_buy = {k: v for k, v in all_trades.items() if v['buy'] != 0 and v['sell'] == 0}
        only_sell = {k: v for k, v in all_trades.items() if v['buy'] == 0 and v['sell'] != 0}

        # Further categorize only_sell by whether they're in portfolio
        only_sell_open = {k: v for k, v in only_sell.items() if v.get('in_portfolio', False)}
        only_sell_not_in_portfolio = {k: v for k, v in only_sell.items() if not v.get('in_portfolio', False)}

        closed_pl = sum(v['buy'] + v['sell'] for v in closed.values())
        only_buy_pl = sum(v['buy'] for v in only_buy.values())
        only_sell_pl = sum(v['sell'] for v in only_sell.values())
        only_sell_not_in_portfolio_pl = sum(v['sell'] for v in only_sell_not_in_portfolio.values())

        # Calculate what happens if we add sell-only that are NOT in portfolio (likely expired)
        with_expired_pl = closed_pl + only_sell_not_in_portfolio_pl

        return jsonify({
            'portfolio_open_options': list(open_in_portfolio),
            'closed_positions': {
                'count': len(closed),
                'total_pl': closed_pl,
                'positions': dict(list(closed.items())[:10])
            },
            'only_buy': {
                'count': len(only_buy),
                'total_pl': only_buy_pl,
                'positions': dict(list(only_buy.items())[:5])
            },
            'only_sell_open_in_portfolio': {
                'count': len(only_sell_open),
                'total_pl': sum(v['sell'] for v in only_sell_open.values()),
                'positions': only_sell_open
            },
            'only_sell_not_in_portfolio': {
                'count': len(only_sell_not_in_portfolio),
                'total_pl': only_sell_not_in_portfolio_pl,
                'positions': only_sell_not_in_portfolio
            },
            'summary': {
                'closed_only_pl': closed_pl,
                'with_expired_worthless': with_expired_pl,
                'target': 3693.32,
                'closed_diff': closed_pl - 3693.32,
                'with_expired_diff': with_expired_pl - 3693.32
            }
        })

    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
