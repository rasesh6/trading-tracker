"""
Trading Tracker - Simple Cost Basis P&L Tracker

Uses Public.com data for accurate P&L calculation.
- Simple summation of netAmount per position
- No FIFO matching
- Handles option assignments correctly
"""

import os
import json
import re
from datetime import datetime, timedelta
from flask import Flask, jsonify, send_file, request
from requests import post, get

app = Flask(__name__)

# Cache for cost basis data
_cost_basis_cache = None
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

def fetch_portfolio(token, account_id):
    """Fetch current portfolio to determine open vs closed positions"""
    url = f"https://api.public.com/userapigateway/trading/{account_id}/portfolio/v2"
    response = get(url, headers={'Authorization': f'Bearer {token}'})
    return response.json()

def parse_option_symbol(description):
    """Extract option symbol from transaction description"""
    match = re.search(r'([A-Z]+\d{6}[PC]\d{8})', description)
    return match.group(1) if match else None

def calculate_cost_basis():
    """Calculate P&L by summing netAmount per symbol (no FIFO)"""
    global _cost_basis_cache, _cache_time

    # Cache for 5 minutes
    if _cost_basis_cache and _cache_time:
        age = (datetime.now() - _cache_time).total_seconds()
        if age < 300:
            return _cost_basis_cache

    try:
        token = get_access_token()
        account_id = get_account_id(token)

        now = datetime.now()
        year_start = datetime(now.year, 1, 1).strftime('%Y-%m-%dT%H:%M:%SZ')
        end_date = now.strftime('%Y-%m-%dT%H:%M:%SZ')

        history = fetch_order_history(token, account_id, year_start, end_date)
        transactions = history.get('transactions', [])

        # Get current portfolio
        portfolio_data = fetch_portfolio(token, account_id)
        portfolio_positions = portfolio_data.get('positions', [])

        # Extract symbols from portfolio
        portfolio_option_symbols = set()
        portfolio_stock_symbols = set()

        for pos in portfolio_positions:
            symbol = pos.get('instrument', {}).get('symbol', '')
            if symbol:
                if '-OPTION' in symbol:
                    clean_symbol = symbol.replace('-OPTION', '')
                    portfolio_option_symbols.add(clean_symbol)
                else:
                    portfolio_stock_symbols.add(symbol)

        # Group by option symbol and sum netAmount
        by_symbol = {}

        for tx in transactions:
            if tx.get('type') == 'TRADE' and tx.get('subType') == 'TRADE':
                description = tx.get('description', '')
                opt_symbol = parse_option_symbol(description)

                if opt_symbol:
                    net = float(tx.get('netAmount') or 0)

                    if opt_symbol not in by_symbol:
                        by_symbol[opt_symbol] = {'net': 0, 'underlying': None, 'is_put': 'P' in opt_symbol}
                    by_symbol[opt_symbol]['net'] += net

                    # Extract underlying
                    underlying_match = re.match(r'([A-Z]+)', opt_symbol)
                    if underlying_match:
                        by_symbol[opt_symbol]['underlying'] = underlying_match.group(1)

        # Calculate realized (closed) and unrealized (open) P&L
        short_term_realized = 0
        long_term_realized = 0
        unrealized_pl = 0

        positions_list = []

        for symbol, data in sorted(by_symbol.items()):
            symbol_pl = data['net']
            underlying = data['underlying']
            is_put = data['is_put']

            # Check if this option was assigned (not in portfolio as option, but underlying stock is held)
            # For assigned puts: the premium received is NOT profit, it reduces stock cost basis
            was_assigned = (
                symbol not in portfolio_option_symbols and
                underlying in portfolio_stock_symbols and
                is_put  # Only applies to PUTs
            )

            if symbol in portfolio_option_symbols:
                # Still open as option
                unrealized_pl += symbol_pl
                positions_list.append({
                    'symbol': symbol,
                    'status': 'open',
                    'unrealized_pl': symbol_pl,
                    'realized_pl': 0,
                    'note': 'Open position'
                })
            elif was_assigned:
                # Assigned - do NOT count premium as profit
                positions_list.append({
                    'symbol': symbol,
                    'status': 'assigned',
                    'unrealized_pl': 0,
                    'realized_pl': 0,
                    'note': 'Assigned - premium reduces stock cost basis',
                    'excluded_amount': symbol_pl
                })
            else:
                # Closed (expired or sold) - count as short-term capital gains
                short_term_realized += symbol_pl
                positions_list.append({
                    'symbol': symbol,
                    'status': 'closed',
                    'unrealized_pl': 0,
                    'realized_pl': symbol_pl,
                    'term': 'short'
                })

        # Also calculate stock P&L (closed stock trades)
        stock_by_symbol = {}
        for tx in transactions:
            if tx.get('type') == 'TRADE' and tx.get('subType') == 'TRADE':
                description = tx.get('description', '')
                # Check if it's NOT an option (stock trade)
                if not parse_option_symbol(description) and ('BUY' in description or 'SELL' in description):
                    parts = description.split()
                    if len(parts) >= 3:
                        stock_symbol = parts[2]
                        net = float(tx.get('netAmount') or 0)

                        if stock_symbol not in stock_by_symbol:
                            stock_by_symbol[stock_symbol] = 0
                        stock_by_symbol[stock_symbol] += net

        # Stock trades for symbols not currently held are realized
        for stock_symbol, pl in stock_by_symbol.items():
            if stock_symbol not in portfolio_stock_symbols:
                short_term_realized += pl
                positions_list.append({
                    'symbol': stock_symbol,
                    'status': 'closed',
                    'unrealized_pl': 0,
                    'realized_pl': pl,
                    'term': 'short',
                    'type': 'stock'
                })

        total_realized_pl = short_term_realized + long_term_realized

        result = {
            'total_realized_pl': total_realized_pl,
            'short_term_pl': short_term_realized,
            'long_term_pl': long_term_realized,
            'total_unrealized_pl': unrealized_pl,
            'total_positions': len(positions_list),
            'positions': positions_list,
            'last_updated': now.isoformat(),
            'portfolio_option_symbols': list(portfolio_option_symbols),
            'portfolio_stock_symbols': list(portfolio_stock_symbols)
        }

        _cost_basis_cache = result
        _cache_time = datetime.now()

        return result

    except Exception as e:
        import traceback
        return {
            'error': str(e),
            'traceback': traceback.format_exc()
        }

def get_stats():
    """Get trading statistics"""
    data = calculate_cost_basis()

    if 'error' in data:
        return data

    # Calculate MTD/YTD
    now = datetime.now()
    month_start = datetime(now.year, now.month, 1)
    year_start = datetime(now.year, 1, 1)

    # For MTD/YTD, we'd need to extract dates from transaction history
    # For now, YTD = all of 2026, MTD = current month
    positions = data.get('positions', [])

    mtd_realized_pl = 0
    ytd_realized_pl = data['short_term_pl'] + data['long_term_pl']  # All YTD positions are from 2026
    mtd_closed = 0
    ytd_closed = 0

    for pos in positions:
        if pos.get('status') == 'closed':
            ytd_closed += 1
            mtd_closed += 1  # Assuming all are current month for now

    mtd_realized_pl = ytd_realized_pl  # Simplified

    data.update({
        'mtd_realized_pl': mtd_realized_pl,
        'mtd_short_term': data['short_term_pl'],
        'mtd_long_term': data['long_term_pl'],
        'mtd_closed': mtd_closed,
        'ytd_realized_pl': ytd_realized_pl,
        'ytd_short_term': data['short_term_pl'],
        'ytd_long_term': data['long_term_pl'],
        'ytd_closed': ytd_closed
    })

    return data

def get_trades(days=7):
    """Get recent positions"""
    data = calculate_cost_basis()
    if 'positions' in data:
        return data['positions']
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
    """Get positions/transactions"""
    days = int(request.args.get('days', 7))
    return jsonify(get_trades(days))

@app.route('/api/update')
def update():
    """Force refresh data from API"""
    global _cost_basis_cache, _cache_time
    _cost_basis_cache = None
    _cache_time = None
    return jsonify(calculate_cost_basis())

@app.route('/api/reset')
def reset():
    """Reset cache"""
    global _cost_basis_cache, _cache_time
    _cost_basis_cache = None
    _cache_time = None
    return jsonify({'status': 'reset'})

@app.route('/api/debug/history')
def debug_history():
    """Debug endpoint to see full transaction history from Public API"""
    try:
        token = get_access_token()
        account_id = get_account_id(token)

        now = datetime.now()
        year_start = datetime(now.year, 1, 1).strftime('%Y-%m-%dT%H:%M:%SZ')
        end_date = now.strftime('%Y-%m-%dT%H:%M:%SZ')

        history = fetch_order_history(token, account_id, year_start, end_date)
        transactions = history.get('transactions', [])

        # Group by transaction type to see all available types
        by_type = {}
        for tx in transactions:
            tx_type = tx.get('type', 'UNKNOWN')
            sub_type = tx.get('subType', 'UNKNOWN')
            key = f"{tx_type}/{sub_type}"

            if key not in by_type:
                by_type[key] = []

            # Store a sample of each type
            if len(by_type[key]) < 5:
                by_type[key].append({
                    'id': tx.get('id'),
                    'description': tx.get('description'),
                    'type': tx_type,
                    'subType': sub_type,
                    'amount': tx.get('amount'),
                    'netAmount': tx.get('netAmount'),
                    'principalAmount': tx.get('principalAmount'),
                    'quantity': tx.get('quantity'),
                    'symbol': tx.get('symbol'),
                    'timestamp': tx.get('timestamp'),
                    'all_fields': list(tx.keys())
                })

        return jsonify({
            'total_transactions': len(transactions),
            'types_found': list(by_type.keys()),
            'samples_by_type': by_type
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500

@app.route('/api/health')
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'ok',
        'timestamp': datetime.now().isoformat(),
        'version': '2.0 (Simple Cost Basis)'
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
