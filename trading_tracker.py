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

def calculate_pl_from_history():
    """Calculate P&L by summing netAmount from only CLOSED positions (completed round-trips)"""
    global _history_cache, _cache_time

    # Cache for 5 minutes
    if _history_cache and _cache_time:
        age = (datetime.now() - _cache_time).total_seconds()
        if age < 300:
            return _history_cache

    try:
        token = get_access_token()
        account_id = get_account_id(token)

        now = datetime.now()
        year_start = datetime(now.year, 1, 1).strftime('%Y-%m-%dT%H:%M:%SZ')
        end_date = now.strftime('%Y-%m-%dT%H:%M:%SZ')

        history = fetch_order_history(token, account_id, year_start, end_date)
        transactions = history.get('transactions', [])

        # Group trades by contract/symbol to identify closed positions
        trades_by_contract = {}
        for tx in transactions:
            tx_type = tx.get('type', '')
            sub_type = tx.get('subType', '')

            if tx_type != 'TRADE' or sub_type != 'TRADE':
                continue

            net_amount = float(tx.get('netAmount') or 0)
            description = tx.get('description', '')
            timestamp = tx.get('timestamp', '')

            # Extract contract identifier
            # For options: "BUY 1 NVDA260109P00185000 at 1.00" -> "NVDA260109P00185000"
            # For stocks: "BUY 100 NVDA at 179.00" -> "NVDA"
            match = re.search(r'([A-Z]+260\d{3}[CP]\d{8})', description)
            if match:
                contract = match.group(1)  # Option contract
            else:
                # Stock symbol
                parts = description.split()
                contract = parts[2] if len(parts) > 2 else 'UNKNOWN'

            if contract not in trades_by_contract:
                trades_by_contract[contract] = {'buy': 0, 'sell': 0, 'transactions': []}

            if 'BUY' in description:
                trades_by_contract[contract]['buy'] += net_amount
            else:
                trades_by_contract[contract]['sell'] += net_amount

            trades_by_contract[contract]['transactions'].append({
                'description': description,
                'netAmount': net_amount,
                'timestamp': timestamp
            })

        # Only count positions that have BOTH buy and sell (closed)
        total_realized_pl = 0
        completed_transactions = []
        closed_positions = 0
        open_positions = 0

        for contract, data in trades_by_contract.items():
            has_buy = data['buy'] != 0
            has_sell = data['sell'] != 0

            # Position is closed if it has both buy and sell
            if has_buy and has_sell:
                position_pl = data['buy'] + data['sell']
                total_realized_pl += position_pl
                completed_transactions.extend(data['transactions'])
                closed_positions += 1
            else:
                open_positions += 1

        result = {
            'total_realized_pl': total_realized_pl,
            'short_term_pl': total_realized_pl,  # All YTD trades are short-term
            'long_term_pl': 0,
            'total_unrealized_pl': 0,
            'total_positions': closed_positions,
            'open_positions': open_positions,
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
    """Get trading statistics"""
    data = calculate_pl_from_history()

    if 'error' in data:
        return data

    # MTD/YTD (all YTD for now)
    now = datetime.now()
    ytd_realized_pl = data['total_realized_pl']
    mtd_realized_pl = ytd_realized_pl  # Simplified

    data.update({
        'mtd_realized_pl': mtd_realized_pl,
        'mtd_short_term': mtd_realized_pl,
        'mtd_long_term': 0,
        'mtd_closed': data['total_positions'],
        'ytd_realized_pl': ytd_realized_pl,
        'ytd_short_term': ytd_realized_pl,
        'ytd_long_term': 0,
        'ytd_closed': data['total_positions']
    })

    return data

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
        'version': '2.2 (Closed Positions Only)'
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
