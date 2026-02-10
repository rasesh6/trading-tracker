"""
Trading Tracker - Simple History-based P&L Tracker

Uses Public.com History API for exact P&L calculation.
- Sums netAmount from all "Completed" trades
- No FIFO matching, no grouping - just add up the numbers
"""

import os
import json
import re
import copy
import functools
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, send_file, request
from requests import post, get

app = Flask(__name__)

# ============================================================================
# CORS ENABLEMENT
# ============================================================================

@app.after_request
def after_request(response):
    """Add CORS headers to all responses"""
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type, X-API-Key')
    response.headers.add('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
    return response

# ============================================================================
# Cache
_history_cache = None
_cache_time = None

# ============================================================================
# API KEY AUTHENTICATION
# ============================================================================

def require_api_key(f):
    """Decorator to require API key for endpoint access"""
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        # Get expected API key from environment
        expected_key = os.environ.get('TRACKER_API_KEY')

        # If no API key is configured, allow all requests (development mode)
        if not expected_key:
            return f(*args, **kwargs)

        # Check for API key in header
        provided_key = request.headers.get('X-API-Key', '')

        if not provided_key or provided_key != expected_key:
            return jsonify({
                'error': 'Unauthorized',
                'message': 'Valid API key required'
            }), 401

        return f(*args, **kwargs)

    return decorated_function

# ============================================================================

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
            # Use end of current day (23:59:59 UTC) to capture all transactions today
            end_date = datetime(now.year, now.month, now.day, 23, 59, 59).strftime('%Y-%m-%dT%H:%M:%SZ')

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

        # Separate options (track each FULL CONTRACT individually) and stocks (FIFO)
        # KEY: Each option contract is identified by its FULL OCC symbol
        # Example: NFLX260320P00074000 and NFLX260320P00080000 are TWO DIFFERENT contracts
        option_contracts = {}
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
                # Use FULL CONTRACT symbol as key (this is the unique identifier)
                contract = match.group(1)

                if contract not in option_contracts:
                    option_contracts[contract] = {
                        'buy': 0,
                        'sell': 0,
                        'transactions': [],
                        'in_portfolio': False,
                        'has_buy': False,
                        'has_sell': False
                    }

                if 'BUY' in description:
                    option_contracts[contract]['buy'] += net_amount
                    option_contracts[contract]['has_buy'] = True
                else:
                    option_contracts[contract]['sell'] += net_amount
                    option_contracts[contract]['has_sell'] = True

                option_contracts[contract]['transactions'].append({
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

        # Check which individual option contracts are currently OPEN in portfolio
        # This is critical: must check by FULL CONTRACT symbol, not grouped
        open_contracts_in_portfolio = set()
        if 'positions' in portfolio:
            for pos in portfolio['positions']:
                instrument = pos.get('instrument', {})
                symbol = instrument.get('symbol', '')
                # Extract full option contract symbol if present
                # Portfolio format: UNDERLYANDINGYYMMDD(C/P)STRIKE*1000
                # Example: NFLX260320P00074000 (no separate version digit)
                contract_match = re.search(r'([A-Z]+\d{6}[CP]\d{8})', symbol)
                if contract_match:
                    open_contracts_in_portfolio.add(contract_match.group(1))

        # Mark each contract as open/closed based on portfolio
        for contract in option_contracts:
            # Check if exact contract is in portfolio
            option_contracts[contract]['in_portfolio'] = contract in open_contracts_in_portfolio

        # FIXED: Do NOT mark expired options as "open" just because underlying stock exists
        # The portfolio check at lines 165-176 already correctly identifies which option
        # contracts are still in portfolio. Expired options that are NOT in portfolio
        # should be counted as CLOSED with realized P&L, regardless of underlying stock.
        # This bug was causing expired options like SOXL260130P00065000 (+$1302.63)
        # to be incorrectly excluded from realized P&L.

        # FIRST: Calculate assignment adjustments (needed for options P&L exclusion)
        # Track assignment cost basis adjustments from short option assignments
        # Format: {symbol: {quantity: shares, premium_total: float, strike: float, premium_per_share: float, option_keys: [keys]}}
        assignment_adjustments = {}
        # Track which option groups were assigned (for exclusion from options P&L)
        assigned_option_keys = set()

        # Analyze sell-only option contracts to detect potential assignments (only in YTD)
        for contract, data in option_contracts.items():
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
                            # Format: UNDERLYINGYYMMDD(C/P)STRIKE*1000 (YYMMDD is 6 digits, NO separate version digit)
                            m = re.match(r'([A-Z]+)(\d{6})([CP])(\d{8})', option_symbol)
                            if m:
                                underlying = m.group(1)
                                strike = int(m.group(4)) / 1000  # Convert from cents
                                contracts = qty
                                shares = contracts * 100
                                premium = price * shares

                                # Store assignment data using full contract (including expiry) as key
                                # This prevents lumping together different expiry dates
                                assignment_key = option_symbol  # Full contract symbol
                                if assignment_key not in assignment_adjustments:
                                    assignment_adjustments[assignment_key] = {
                                        'quantity': 0,
                                        'premium_total': 0,
                                        'strike': strike,
                                        'premium_per_share': 0,
                                        'option_keys': [],
                                        'underlying': underlying  # Keep underlying for stock matching
                                    }

                                assignment_adjustments[assignment_key]['quantity'] += shares
                                assignment_adjustments[assignment_key]['premium_total'] += premium
                                assignment_adjustments[assignment_key]['option_keys'].append(contract)
                                # Track this option group as assigned (will be excluded from options P&L)
                                assigned_option_keys.add(contract)
                        except (ValueError, IndexError):
                            continue

        # Calculate premium per share for each assignment
        for contract_key in assignment_adjustments:
            adj = assignment_adjustments[contract_key]
            if adj['quantity'] > 0:
                adj['premium_per_share'] = adj['premium_total'] / adj['quantity']

        # Reorganize by underlying symbol for easier stock matching
        # Create a mapping: underlying -> list of assignments
        assignments_by_symbol = {}
        for contract_key, adj in assignment_adjustments.items():
            underlying = adj['underlying']
            if underlying not in assignments_by_symbol:
                assignments_by_symbol[underlying] = []
            assignments_by_symbol[underlying].append({
                'quantity': adj['quantity'],
                'premium_total': adj['premium_total'],
                'strike': adj['strike'],
                'contract_key': contract_key
            })

        # Calculate options P&L (tracking each contract individually)
        options_pl = 0
        completed_transactions = []
        closed_option_contracts = 0
        open_option_contracts = 0

        for contract, data in option_contracts.items():
            has_buy = data['buy'] != 0
            has_sell = data['sell'] != 0
            is_in_portfolio = data['in_portfolio']

            # CRITICAL: Exclude assigned options from options P&L
            # Assigned options create stock positions and are accounted for in stock P&L
            if contract in assigned_option_keys:
                continue  # Skip this contract - it's handled in stock P&L

            # Count as CLOSED if:
            # 1. Both buy and sell in YTD, OR
            # 2. Only buy or only sell BUT NOT in portfolio (expired/worthless or closed early)
            if not is_in_portfolio:
                # Contract is closed (both sides in YTD, or expired/closed early)
                options_pl += data['buy'] + data['sell']
                completed_transactions.extend(data['transactions'])
                if has_buy or has_sell:
                    closed_option_contracts += 1
            elif is_in_portfolio:
                # Contract is still open in portfolio
                open_option_contracts += 1

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
                # Check if this BUY matches an assignment
                amount = trade['amount']
                original_amount = amount
                cost_basis_from_portfolio = None

                # Check assignments for this symbol
                if symbol in assignments_by_symbol:
                    for adj in assignments_by_symbol[symbol]:
                        # Match by exact quantity
                        if trade['quantity'] == adj['quantity']:
                            # Adjust cost basis by premium received
                            cost_adjustment = adj['premium_total']
                            amount = amount + cost_adjustment  # Add premium to reduce negative amount
                            cost_basis_from_portfolio = 'ytd_assignment'
                            break  # Use first matching assignment

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

                    # Check if this stock position came from a YTD assignment
                    # If so, subtract the premium from stock P&L (it's option P&L, not stock P&L)
                    assignment_premium = 0
                    if buy_trade.get('cost_basis_source') == 'ytd_assignment' and symbol in assignments_by_symbol:
                        for adj in assignments_by_symbol[symbol]:
                            if buy_trade['quantity'] == adj['quantity']:
                                premium_ratio = match_qty / buy_trade['quantity']
                                assignment_premium = adj['premium_total'] * premium_ratio
                                break

                    stocks_pl += match_pl - assignment_premium

                    # Add synthetic P&L transaction with closing date for chart
                    # This ensures stock P&L is included in cumulative chart
                    description = f'Stock P&L: {symbol} {match_qty} shares'

                    completed_transactions.append({
                        'netAmount': match_pl - assignment_premium,  # Subtract YTD assignment premium from stock P&L
                        'description': description,
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
            'total_positions': closed_option_contracts + closed_stock_positions,
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
    # Each transaction in completed_transactions represents a closed position
    # Use the transaction's timestamp as the closing date
    now = datetime.now(timezone.utc)
    current_month = now.month
    current_year = now.year

    mtd_realized_pl = 0
    mtd_closed = 0

    transactions = ytd_data.get('transactions', [])

    # Simply sum all transactions that closed in the current month
    # Each transaction already represents a complete closed position
    for tx in transactions:
        timestamp = tx.get('timestamp', '')
        net_amount = tx.get('netAmount', 0)

        if timestamp:
            try:
                dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                if dt.month == current_month and dt.year == current_year:
                    mtd_realized_pl += net_amount
                    mtd_closed += 1
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
        'ytd_closed': ytd_data['total_positions'] - ytd_data['open_positions']
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
@require_api_key
def index():
    return send_file('dashboard.html')

@app.route('/api/stats')
@require_api_key
def stats():
    """Get trading statistics"""
    return jsonify(get_stats())

@app.route('/api/trades')
@require_api_key
def trades():
    """Get transactions"""
    days = int(request.args.get('days', 7))
    return jsonify(get_trades(days))

@app.route('/api/update')
@require_api_key
def update():
    """Force refresh"""
    global _history_cache, _cache_time
    _history_cache = None
    _cache_time = None
    return jsonify(calculate_pl_from_history())

@app.route('/api/reset')
@require_api_key
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
        'version': '3.22 (FIX: Use end-of-day UTC as end time for history queries. Previously used current time which missed same-day transactions. Now captures all transactions through 23:59:59 UTC. SOXL Feb 6 puts now correctly included: +$582.69)'
    })

@app.route('/api/debug/stock_trades')
@require_api_key
def debug_stock_trades():
    """Debug endpoint to trace through stock FIFO matching with assignment adjustments"""
    print("DEBUG: Endpoint called!")
    try:
        print("DEBUG: About to get token")
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

                            # Format: UNDERLYINGYYMMDD(C/P)STRIKE*1000 (YYMMDD is 6 digits, NO separate version digit)
                            m = re.match(r'([A-Z]+)(\d{6})([CP])(\d{8})', option_symbol)
                            if m:
                                underlying = m.group(1)
                                strike = int(m.group(4)) / 1000  # Convert from cents
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

                # Skip raw BUY trades that correspond to assignments.
                # When a put is assigned, Schwab API creates both:
                # 1. An option assignment record (used to create synthetic trades)
                # 2. An actual stock BUY record (this raw trade at strike price)
                # We skip the raw BUY since the synthetic trade already represents it correctly.
                if side == 'BUY' and symbol in assignment_adjustments:
                    adj = assignment_adjustments[symbol]
                    # Calculate price from this raw trade
                    price_per_share = abs(amount / qty) if qty > 0 else 0
                    # Check if this raw trade matches the assignment parameters
                    if (qty == adj['quantity'] and
                        abs(price_per_share - adj['strike']) < 0.01):  # Allow small floating point diff
                        print(f"DEBUG: Skipping raw BUY trade for {symbol} assignment: {qty} shares @ ${price_per_share:.2f} matches strike ${adj['strike']:.2f}")
                        continue  # Skip this raw BUY trade

                # NOTE: Don't apply assignment adjustment to remaining raw BUY trades here.
                # The synthetic trade generation below will create the correct assignment trades.
                # Applying adjustment here would incorrectly mark existing BUY trades as adjusted.

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

        # Generate synthetic BUY trades for assignments with correct quantity
        # When a put is assigned, Schwab doesn't create a proper "BUY X shares" transaction,
        # so we directly create synthetic trades from assignment_adjustments data
        # Find the timestamp of the corresponding SELL trade to place the synthetic BUY nearby
        print(f"DEBUG: assignment_adjustments = {assignment_adjustments}")
        for symbol, adj in assignment_adjustments.items():
            print(f"DEBUG: Creating synthetic BUY trade for {symbol} assignment: {adj}")

            # Find the first SELL trade for this symbol to get a nearby timestamp
            nearby_timestamp = None
            for trade in stock_trades:
                if trade['symbol'] == symbol and trade['side'] == 'SELL':
                    nearby_timestamp = trade['timestamp']
                    break

            # If no SELL trade found, use current time
            if not nearby_timestamp:
                nearby_timestamp = datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ')

            # Create the synthetic BUY trade with correct quantity and premium adjustment
            original_cost = adj['quantity'] * adj['strike']  # Cost at strike price
            adjusted_cost = original_cost - adj['premium_total']  # Premium reduces cost basis

            synthetic_trade = {
                'symbol': symbol,
                'side': 'BUY',
                'quantity': adj['quantity'],
                'amount': -adjusted_cost,  # BUY trades have negative amounts (cash outflow)
                'original_amount': -original_cost,
                'cost_adjustment': adj['premium_total'],
                'adjusted': True,
                'timestamp': nearby_timestamp,
                'description': f"BUY {adj['quantity']} {symbol} at ${adj['strike']:.2f} (assignment from put, adj=${adj['premium_total']:.2f})"
            }

            print(f"DEBUG: Created synthetic trade: qty={synthetic_trade['quantity']}, amount={synthetic_trade['amount']}, adj=${synthetic_trade['cost_adjustment']}")
            stock_trades.append(synthetic_trade)
            # Verify the trade was added correctly
            print(f"DEBUG: After append, last trade in stock_trades has qty={stock_trades[-1]['quantity']}")

        # Re-sort after adding synthetic trades
        stock_trades.sort(key=lambda x: x['timestamp'])

        # FIFO matching - use a deep copy for processing to preserve original trade quantities for display
        stock_trades_copy = copy.deepcopy(stock_trades)
        stock_positions = {}
        fifo_log = []
        stocks_pl = 0

        # DEBUG: Log all trade quantities before FIFO
        print(f"DEBUG: Before FIFO - trade quantities:")
        for i, t in enumerate(stock_trades):
            is_synth = " [SYNTHETIC]" if t.get('adjusted') else ""
            print(f"  {i}. {t['side']} {t['symbol']}: qty={t['quantity']}{is_synth}")

        for trade in stock_trades_copy:
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
                # Debug SOXL assignment
                if symbol == 'SOXL' and trade['quantity'] == 2000:
                    print(f"DEBUG SOXL BUY: Added to queue")
                    print(f"  Amount: ${trade['amount']}")
                    print(f"  Cost basis source: {trade.get('cost_basis_source', 'None')}")
            else:
                remaining_qty = trade['quantity']
                sell_price = abs(trade['amount'] / trade['quantity']) if trade['quantity'] > 0 else 0
                print(f"DEBUG: FIFO - SELL {trade['quantity']} {symbol} @ ${sell_price:.2f} -> matching against {len(stock_positions[symbol])} BUY positions")

                while remaining_qty > 0 and stock_positions[symbol]:
                    buy_trade = stock_positions[symbol][0]
                    match_qty = min(remaining_qty, buy_trade['quantity'])
                    buy_price = abs(buy_trade['amount'] / buy_trade['quantity']) if buy_trade['quantity'] > 0 else 0
                    match_pl = (sell_price - buy_price) * match_qty
                    stocks_pl += match_pl
                    is_synth = " [SYNTHETIC]" if buy_trade.get('adjusted') else ""
                    print(f"  MATCH: {match_qty} shares @ sell=${sell_price:.2f} vs buy=${buy_price:.2f}{is_synth} -> P&L=${match_pl:.2f} (running total: ${stocks_pl:.2f})")

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

        # DEBUG: Log all trade quantities after FIFO
        print(f"DEBUG: After FIFO - trade quantities:")
        for i, t in enumerate(stock_trades):
            is_synth = " [SYNTHETIC]" if t.get('adjusted') else ""
            print(f"  {i}. {t['side']} {t['symbol']}: qty={t['quantity']}{is_synth}")

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
            'stocks_pl': stocks_pl,
            'option_trades': option_trades  # DEBUG: include option_trades
        })

    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()})

@app.route('/api/debug/raw_history')
@require_api_key
def debug_raw_history():
    """Debug endpoint to show raw Public API history transactions"""
    try:
        token = get_access_token()
        account_id = get_account_id(token)

        now = datetime.now()
        year_start = datetime(now.year, 1, 1).strftime('%Y-%m-%dT%H:%M:%SZ')
        end_date = now.strftime('%Y-%m-%dT%H:%M:%SZ')

        # Fetch raw History API
        history = fetch_order_history(token, account_id, year_start, end_date)
        all_transactions = history.get('transactions', [])

        # Filter to only TRADE transactions
        trade_transactions = []
        for tx in all_transactions:
            tx_type = tx.get('type', '')
            sub_type = tx.get('subType', '')
            if tx_type == 'TRADE' and sub_type == 'TRADE':
                trade_transactions.append(tx)

        # Group by symbol/contract
        by_symbol = {}
        for tx in trade_transactions:
            desc = tx.get('description', '')
            net_amount = float(tx.get('netAmount') or 0)

            # Try to match option
            match = re.search(r'([A-Z]+2\d{2}\d{3}[CP]\d{8})', desc)
            if match:
                key = match.group(1)
            else:
                # Stock
                parts = desc.split()
                key = parts[2] if len(parts) > 2 else 'UNKNOWN'

            if key not in by_symbol:
                by_symbol[key] = {'buy': 0, 'sell': 0, 'count': 0, 'txs': []}
            if 'BUY' in desc:
                by_symbol[key]['buy'] += net_amount
            else:
                by_symbol[key]['sell'] += net_amount
            by_symbol[key]['count'] += 1
            by_symbol[key]['txs'].append({'desc': desc[:60], 'amount': net_amount})

        return jsonify({
            'total_transactions': len(all_transactions),
            'trade_transactions': len(trade_transactions),
            'by_symbol': by_symbol,
            'all_trade_txs': [{'desc': tx.get('description', '')[:80], 'amount': float(tx.get('netAmount', 0))} for tx in trade_transactions]
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()})

@app.route('/api/debug/all_positions')
@require_api_key
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
                # Option symbols have format like "NVDA260130P00065000"
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
