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
    """Calculate P&L using FIFO matching for closed positions"""
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

        # Separate options (exact contract matching) and stocks (FIFO matching)
        option_trades = {}  # contract -> {buy: total, sell: total, transactions: []}
        stock_trades = []   # list of {symbol, side, quantity, amount, timestamp}

        for tx in transactions:
            tx_type = tx.get('type', '')
            sub_type = tx.get('subType', '')

            if tx_type != 'TRADE' or sub_type != 'TRADE':
                continue

            net_amount = float(tx.get('netAmount') or 0)
            description = tx.get('description', '')
            timestamp = tx.get('timestamp', '')

            # Check if it's an option (2026 expiry: 260XXX)
            match = re.search(r'([A-Z]+260\d{3}[CP]\d{8})', description)
            if match:
                # Option trade - exact contract matching
                contract = match.group(1)
                if contract not in option_trades:
                    option_trades[contract] = {'buy': 0, 'sell': 0, 'transactions': []}

                if 'BUY' in description:
                    option_trades[contract]['buy'] += net_amount
                else:
                    option_trades[contract]['sell'] += net_amount

                option_trades[contract]['transactions'].append({
                    'description': description,
                    'netAmount': net_amount,
                    'timestamp': timestamp
                })
            else:
                # Stock trade - extract symbol and quantity
                # "BUY 100 NVDA at 179.00" -> symbol: NVDA, qty: 100
                parts = description.split()
                if len(parts) >= 3:
                    side = 'BUY' if 'BUY' in description else 'SELL'
                    qty = int(parts[1]) if parts[1].isdigit() else 0
                    symbol = parts[2]
                    stock_trades.append({
                        'symbol': symbol,
                        'side': side,
                        'quantity': qty,
                        'amount': net_amount,
                        'timestamp': timestamp,
                        'description': description
                    })

        # Calculate options P&L (exact contract matching - both buy and sell required)
        options_pl = 0
        completed_transactions = []
        closed_option_positions = 0
        open_option_positions = 0

        for contract, data in option_trades.items():
            if data['buy'] != 0 and data['sell'] != 0:
                options_pl += data['buy'] + data['sell']
                completed_transactions.extend(data['transactions'])
                closed_option_positions += 1
            else:
                open_option_positions += 1

        # Calculate stock P&L using FIFO matching
        # Sort by timestamp and match buys with sells
        stock_trades.sort(key=lambda x: x['timestamp'])

        # Group by symbol and track open positions
        stock_positions = {}  # symbol -> queue of open buy trades
        stocks_pl = 0
        closed_stock_positions = 0
        open_stock_positions = 0

        for trade in stock_trades:
            symbol = trade['symbol']
            if symbol not in stock_positions:
                stock_positions[symbol] = []

            if trade['side'] == 'BUY':
                # Add to open positions queue
                stock_positions[symbol].append({
                    'quantity': trade['quantity'],
                    'amount': trade['amount'],
                    'description': trade['description'],
                    'timestamp': trade['timestamp']
                })
            else:  # SELL
                # Match with FIFO buys
                remaining_qty = trade['quantity']
                sell_amount_per_share = abs(trade['amount'] / trade['quantity'])

                while remaining_qty > 0 and stock_positions[symbol]:
                    buy_trade = stock_positions[symbol][0]

                    match_qty = min(remaining_qty, buy_trade['quantity'])
                    buy_amount_per_share = abs(buy_trade['amount'] / buy_trade['quantity'])

                    # Calculate P&L for this match
                    match_pl = (sell_amount_per_share - buy_amount_per_share) * match_qty
                    stocks_pl += match_pl

                    completed_transactions.append(buy_trade)
                    completed_transactions.append(trade)

                    # Update quantities
                    remaining_qty -= match_qty
                    buy_trade['quantity'] -= match_qty

                    # Remove fully matched buy from queue
                    if buy_trade['quantity'] == 0:
                        stock_positions[symbol].pop(0)
                        closed_stock_positions += 1

                if remaining_qty > 0:
                    # Unmatched sell (short position opened)
                    stock_positions[symbol].append({
                        'quantity': remaining_qty,
                        'amount': 0,  # Will be matched when buy-to-close happens
                        'description': f"SHORT {remaining_qty} {symbol}",
                        'timestamp': trade['timestamp']
                    })

        # Count remaining open positions
        for symbol, queue in stock_positions.items():
            open_stock_positions += len(queue)

        total_realized_pl = options_pl + stocks_pl

        result = {
            'total_realized_pl': total_realized_pl,
            'short_term_pl': total_realized_pl,
            'long_term_pl': 0,
            'total_unrealized_pl': 0,
            'total_positions': closed_option_positions + closed_stock_positions,
            'open_positions': open_option_positions + open_stock_positions,
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
        'version': '2.3 (FIFO Stock Matching)'
    })

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
