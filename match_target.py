#!/usr/bin/env python3
"""Comprehensive analysis to match Public.com target of $3,693.32"""

import requests
import re

response = requests.get("https://web-production-12eb.up.railway.app/api/stats")
data = response.json()
transactions = data.get('transactions', [])

# Separate stock and option trades
stocks = []
options = []

for tx in transactions:
    desc = tx['description']
    amount = tx['netAmount']

    if '260' in desc or '261' in desc or '262' in desc:
        options.append((desc, amount))
    else:
        stocks.append((desc, amount))

print("=" * 80)
print("COMPREHENSIVE P&L ANALYSIS")
print("=" * 80)

# Analyze stock trades
stock_pl = 0
print("\n📈 STOCK TRADES:")
stock_symbols = {}
for desc, amount in stocks:
    # Parse: "BUY 100 NVDA at 179.00" -> symbol is "NVDA"
    parts = desc.split()
    symbol = parts[2] if len(parts) > 2 else 'UNKNOWN'
    if symbol not in stock_symbols:
        stock_symbols[symbol] = {'buy': 0, 'sell': 0, 'count': 0}
    if 'BUY' in desc:
        stock_symbols[symbol]['buy'] += amount
    else:
        stock_symbols[symbol]['sell'] += amount
    stock_symbols[symbol]['count'] += 1
    stock_pl += amount

for symbol, data in sorted(stock_symbols.items()):
    net = data['buy'] + data['sell']
    # A position is closed if both buy and sell exist AND the net amount is small relative to trades
    # (meaning most shares were sold, not just some)
    has_both = data['buy'] != 0 and data['sell'] != 0
    # Position is closed if net is less than 10% of either buy or sell amount (round-trip completed)
    is_closed = has_both and abs(net) < 0.1 * min(abs(data['buy']), abs(data['sell']))
    status = '✓ CLOSED' if is_closed else '⚠ OPEN'
    print(f"  {symbol}: {data['count']:2} trades  ${data['buy']:>12.2f} (buy) + ${data['sell']:>12.2f} (sell) = ${net:>12.2f} {status}")

# Calculate separate P&L for closed vs open
closed_stock_pl = sum(d['buy'] + d['sell'] for d in stock_symbols.values()
                     if d['buy'] != 0 and d['sell'] != 0 and abs(d['buy'] + d['sell']) < 0.1 * min(abs(d['buy']), abs(d['sell'])))
open_stock_pl = sum(d['buy'] + d['sell'] for d in stock_symbols.values()
                    if not (d['buy'] != 0 and d['sell'] != 0 and abs(d['buy'] + d['sell']) < 0.1 * min(abs(d['buy']), abs(d['sell']))))

print(f"\n  Stock P&L: ${stock_pl:.2f}")

# Analyze option trades
print("\n📊 OPTION TRADES:")
option_contracts = {}
for desc, amount in options:
    # Extract option symbol
    match = re.search(r'([A-Z]+260\d{3}[CP]\d{8})', desc)
    if match:
        symbol = match.group(1)
    else:
        continue

    if symbol not in option_contracts:
        option_contracts[symbol] = {'buy': 0, 'sell': 0, 'total': 0}

    if 'BUY' in desc:
        option_contracts[symbol]['buy'] += amount
    else:
        option_contracts[symbol]['sell'] += amount
    option_contracts[symbol]['total'] += amount

# Categorize option positions
completed_options = {k: v for k, v in option_contracts.items() if v['buy'] != 0 and v['sell'] != 0}
open_options = {k: v for k, v in option_contracts.items() if v['buy'] != 0 and v['sell'] == 0}
short_open_options = {k: v for k, v in option_contracts.items() if v['buy'] == 0 and v['sell'] != 0}

completed_pl = sum(v['total'] for v in completed_options.values())
open_pl = sum(v['total'] for v in open_options.values())
short_open_pl = sum(v['total'] for v in short_open_options.values())

print(f"\n  Completed spreads (both buy & sell): {len(completed_options)} contracts, ${completed_pl:.2f}")
print(f"  Open long positions (only buy):       {len(open_options)} contracts, ${open_pl:.2f}")
print(f"  Open short positions (only sell):     {len(short_open_options)} contracts, ${short_open_pl:.2f}")
print(f"  Total options P&L:                    ${completed_pl + open_pl + short_open_pl:.2f}")

# Calculate totals
total_all = stock_pl + completed_pl + open_pl + short_open_pl
total_closed_only = closed_stock_pl + completed_pl
total_with_short = closed_stock_pl + completed_pl + short_open_pl

print("\n" + "=" * 80)
print("P&L SCENARIOS")
print("=" * 80)
print(f"  Closed stock P&L:                    ${closed_stock_pl:12.2f}")
print(f"  Open stock P&L:                      ${open_stock_pl:12.2f}")
print(f"\n1. CLOSED Stock + Completed options:  ${total_closed_only:12.2f}")
print(f"2. CLOSED Stock + Completed + Short open: ${total_with_short:12.2f}")
print(f"3. ALL Stock + All options:           ${total_all:12.2f}")
print(f"\n🎯 Target from Public.com:             $     3,693.32")
print(f"   Difference (scenario 2):            ${total_with_short - 3693.32:12.2f}")
print(f"   Difference (scenario 1):            ${total_closed_only - 3693.32:12.2f}")

print("\n" + "=" * 80)
print("OPEN SHORT POSITIONS (needs investigation):")
print("=" * 80)
for symbol, data in sorted(short_open_options.items(), key=lambda x: x[1]['total'], reverse=True):
    print(f"  {symbol:30} ${data['total']:>10.2f} (sell)")

print("\n" + "=" * 80)
print("OPEN LONG POSITIONS:")
print("=" * 80)
for symbol, data in sorted(open_options.items(), key=lambda x: x[1]['total']):
    print(f"  {symbol:30} ${data['total']:>10.2f} (buy)")
