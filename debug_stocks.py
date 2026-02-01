#!/usr/bin/env python3
"""Debug stock FIFO calculation"""

import os
import requests
from datetime import datetime

# Get access token from env
secret = os.environ.get('PUBLIC_API_TOKEN')
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

print("=" * 80)
print("STOCK TRADES (FIFO CALCULATION)")
print("=" * 80)

# Extract stock trades (not options)
stock_trades = []
for tx in transactions:
    tx_type = tx.get('type', '')
    sub_type = tx.get('subType', '')

    if tx_type != 'TRADE' or sub_type != 'TRADE':
        continue

    description = tx.get('description', '')
    # Skip options (contain 260, 261, 262)
    if '260' in description or '261' in description or '262' in description:
        continue

    # Parse stock trades
    parts = description.split()
    if len(parts) >= 3 and ('BUY' in description or 'SELL' in description):
        side = 'BUY' if 'BUY' in description else 'SELL'
        try:
            qty = int(parts[1]) if parts[1].isdigit() else 1
        except:
            qty = 1
        symbol = parts[2]
        amount = float(tx.get('netAmount', 0))
        timestamp = tx.get('timestamp', '')

        stock_trades.append({
            'symbol': symbol,
            'side': side,
            'quantity': qty,
            'amount': amount,
            'timestamp': timestamp,
            'description': description,
            'price_per_share': abs(amount / qty) if qty > 0 else 0
        })

# Sort by timestamp
stock_trades.sort(key=lambda x: x['timestamp'])

# FIFO matching
stock_positions = {}
stocks_pl = 0

print("\nStock trades in order:")
for i, trade in enumerate(stock_trades, 1):
    symbol = trade['symbol']
    side = trade['side']
    qty = trade['quantity']
    amount = trade['amount']
    price = trade['price_per_share']
    print(f"\n{i}. {trade['timestamp']}")
    print(f"   {trade['description']}")
    print(f"   Amount: ${amount:.2f} (${price:.2f}/share)")

    if symbol not in stock_positions:
        stock_positions[symbol] = []

    if side == 'BUY':
        stock_positions[symbol].append({
            'quantity': qty,
            'amount': amount,
            'price': price,
            'timestamp': trade['timestamp']
        })
        print(f"   -> Added to {symbol} queue (position: {len(stock_positions[symbol])})")
    else:  # SELL
        remaining_qty = qty
        sell_price = price

        print(f"   -> Matching with buys (FIFO):")
        while remaining_qty > 0 and stock_positions[symbol]:
            buy_trade = stock_positions[symbol][0]
            match_qty = min(remaining_qty, buy_trade['quantity'])
            buy_price = buy_trade['price']
            match_pl = (sell_price - buy_price) * match_qty
            stocks_pl += match_pl

            print(f"      Sell {match_qty} @ ${sell_price:.2f} - Buy {match_qty} @ ${buy_price:.2f} = ${match_pl:.2f}")

            remaining_qty -= match_qty
            buy_trade['quantity'] -= match_qty

            if buy_trade['quantity'] == 0:
                stock_positions[symbol].pop(0)

        if remaining_qty > 0:
            print(f"      Warning: {remaining_qty} shares unmatched (short sale)")

# Show remaining open positions
print("\n" + "=" * 80)
print("OPEN POSITIONS (remaining after FIFO matching)")
print("=" * 80)
for symbol, queue in stock_positions.items():
    if queue:
        print(f"\n{symbol}: {len(queue)} lot(s)")
        for lot in queue:
            print(f"  {lot['quantity']} shares @ ${lot['price']:.2f}")

print("\n" + "=" * 80)
print("STOCK P&L SUMMARY")
print("=" * 80)
print(f"Total Stock P&L: ${stocks_pl:,.2f}")
