#!/usr/bin/env python3
"""Local debug script to understand P&L calculation"""

import os
import requests
import re
from datetime import datetime

# Get access token from env
secret = os.environ.get('PUBLIC_API_TOKEN')
if not secret:
    print("ERROR: PUBLIC_API_TOKEN not set")
    exit(1)

# Get access token
response = requests.post(
    'https://api.public.com/userapiauthservice/personal/access-tokens',
    json={'secret': secret, 'validityInMinutes': 120},
    headers={'Content-Type': 'application/json'}
)
token = response.json()['accessToken']

# Get account ID
response = requests.get(
    'https://api.public.com/userapigateway/trading/account',
    headers={'Authorization': f'Bearer {token}'}
)
accounts = response.json().get('accounts', [])
account_id = None
for acc in accounts:
    if acc.get('accountType') == 'BROKERAGE':
        account_id = acc['accountId']
        break
if not account_id:
    account_id = accounts[0]['accountId'] if accounts else None

print(f"Account ID: {account_id}")

# Fetch YTD history
now = datetime.now()
year_start = datetime(now.year, 1, 1).strftime('%Y-%m-%dT%H:%M:%SZ')
end_date = now.strftime('%Y-%m-%dT%H:%M:%SZ')

response = requests.get(
    f"https://api.public.com/userapigateway/trading/{account_id}/history",
    params={'start': year_start, 'end': end_date, 'pageSize': 1000},
    headers={'Authorization': f'Bearer {token}'}
)
history = response.json()
transactions = history.get('transactions', [])

print(f"\nTotal YTD transactions: {len(transactions)}")

# Fetch Portfolio to see open positions
response = requests.get(
    f'https://api.public.com/userapigateway/trading/{account_id}/portfolio',
    headers={'Authorization': f'Bearer {token}'}
)
portfolio = response.json()

# Extract open option positions
open_in_portfolio = set()
if 'positions' in portfolio:
    for pos in portfolio['positions']:
        symbol = pos.get('symbol', '')
        if re.match(r'[A-Z]+2\d{2}\d{3}[CP]\d{8}', symbol):
            open_in_portfolio.add(symbol)

print(f"Open option positions in portfolio: {len(open_in_portfolio)}")
if open_in_portfolio:
    print(f"  {list(open_in_portfolio)}")

# Process transactions - separate options and stocks
option_trades = {}  # Grouped by underlying_expiry
stock_trades = []

for tx in transactions:
    tx_type = tx.get('type', '')
    sub_type = tx.get('subType', '')

    if tx_type != 'TRADE' or sub_type != 'TRADE':
        continue

    net_amount = float(tx.get('netAmount') or 0)
    description = tx.get('description', '')
    timestamp = tx.get('timestamp', '')

    # Check if it's an option
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
            option_trades[key] = {
                'buy': 0, 'sell': 0, 'transactions': [],
                'any_in_portfolio': False, 'contracts': set()
            }

        option_trades[key]['contracts'].add(contract)

        if 'BUY' in description:
            option_trades[key]['buy'] += net_amount
        else:
            option_trades[key]['sell'] += net_amount

        option_trades[key]['transactions'].append({
            'description': description,
            'netAmount': net_amount,
            'timestamp': timestamp,
            'contract': contract
        })
    else:
        # Stock trade
        parts = description.split()
        if len(parts) >= 3:
            side = 'BUY' if 'BUY' in description else 'SELL'
            qty = int(parts[1]) if parts[1].replace('.', '').isdigit() else 1
            symbol = parts[2]
            stock_trades.append({
                'symbol': symbol,
                'side': side,
                'quantity': qty,
                'amount': net_amount,
                'timestamp': timestamp,
                'description': description
            })

# Check which option groups have any contract in portfolio
print("\nChecking portfolio for open positions...")
for key, data in option_trades.items():
    for contract in data['contracts']:
        if contract in open_in_portfolio:
            data['any_in_portfolio'] = True
            print(f"  {key}: contract {contract} is OPEN in portfolio")
            break

# Calculate options P&L
print("\n" + "="*80)
print("OPTION POSITIONS")
print("="*80)

options_pl = 0
closed_count = 0
open_count = 0

for key, data in sorted(option_trades.items()):
    has_buy = data['buy'] != 0
    has_sell = data['sell'] != 0
    is_in_portfolio = data['any_in_portfolio']
    pl = data['buy'] + data['sell']

    status = "CLOSED" if not is_in_portfolio else "OPEN"
    count_status = "counted" if not is_in_portfolio else "NOT counted"

    print(f"\n{key}:")
    print(f"  Buy: ${data['buy']:>10.2f}")
    print(f"  Sell: ${data['sell']:>10.2f}")
    print(f"  P&L: ${pl:>10.2f}")
    print(f"  In Portfolio: {is_in_portfolio} ({status})")
    print(f"  Contracts: {data['contracts']}")

    if not is_in_portfolio:
        options_pl += pl
        closed_count += 1
        print(f"  -> {count_status} in P&L")
    else:
        open_count += 1
        print(f"  -> {count_status} in P&L (still open)")

print("\n" + "="*80)
print("OPTIONS SUMMARY")
print("="*80)
print(f"Closed positions counted: {closed_count}")
print(f"Open positions NOT counted: {open_count}")
print(f"Total Options P&L: ${options_pl:,.2f}")

# Calculate stock P&L using FIFO
print("\n" + "="*80)
print("STOCK POSITIONS (FIFO)")
print("="*80)

stock_trades.sort(key=lambda x: x['timestamp'])
stock_positions = {}
stocks_pl = 0
closed_stock_count = 0

for trade in stock_trades:
    symbol = trade['symbol']
    if symbol not in stock_positions:
        stock_positions[symbol] = []

    if trade['side'] == 'BUY':
        stock_positions[symbol].append({
            'quantity': trade['quantity'],
            'amount': trade['amount'],
            'description': trade['description'],
            'timestamp': trade['timestamp']
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
            remaining_qty -= match_qty
            buy_trade['quantity'] -= match_qty

            if buy_trade['quantity'] == 0:
                stock_positions[symbol].pop(0)
                closed_stock_count += 1

print(f"Closed stock positions: {closed_stock_count}")
print(f"Stock P&L: ${stocks_pl:,.2f}")

# Total
total_pl = options_pl + stocks_pl
print("\n" + "="*80)
print("TOTAL P&L")
print("="*80)
print(f"Options: ${options_pl:,.2f}")
print(f"Stocks: ${stocks_pl:,.2f}")
print(f"TOTAL: ${total_pl:,.2f}")
print(f"\nTarget: $3,693.32")
print(f"Difference: ${total_pl - 3693.32:,.2f}")
