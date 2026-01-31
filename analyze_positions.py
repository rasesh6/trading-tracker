#!/usr/bin/env python3
"""Analyze which option positions are fully completed"""

import requests

response = requests.get("https://web-production-12eb.up.railway.app/api/stats")
data = response.json()
transactions = data.get('transactions', [])

# Filter to 2026 options
options = [t for t in transactions if '260' in t.get('description', '')]

# Group by contract (extract option symbol without BUY/SELL and quantity)
import re
contracts = {}
for tx in options:
    desc = tx['description']
    amount = tx['netAmount']

    # Extract option symbol (e.g., "GLD260206C00515000" from "BUY 1 GLD260206C00515000 at 6.34")
    match = re.search(r'([A-Z]+260\d{3}[CP]\d{8})', desc)
    if match:
        symbol = match.group(1)
    else:
        continue

    if symbol not in contracts:
        contracts[symbol] = {'buy': 0, 'sell': 0, 'total': 0, 'buy_qty': 0, 'sell_qty': 0}

    # Extract quantity
    qty_match = re.search(r'(BUY|SELL)\s+(\d+)', desc)
    qty = int(qty_match.group(2)) if qty_match else 1

    if desc.startswith('BUY'):
        contracts[symbol]['buy'] += amount
        contracts[symbol]['buy_qty'] += qty
    else:
        contracts[symbol]['sell'] += amount
        contracts[symbol]['sell_qty'] += qty
    contracts[symbol]['total'] += amount

print("=" * 80)
print("OPTION POSITIONS ANALYSIS")
print("=" * 80)

# Completed positions (have both buy and sell)
completed = {k: v for k, v in contracts.items() if v['buy'] != 0 and v['sell'] != 0}
only_buy = {k: v for k, v in contracts.items() if v['buy'] != 0 and v['sell'] == 0}
only_sell = {k: v for k, v in contracts.items() if v['buy'] == 0 and v['sell'] != 0}

print(f"\n✓ COMPLETED POSITIONS (both buy & sell): {len(completed)}")
for contract, data in sorted(completed.items(), key=lambda x: x[1]['total'], reverse=True):
    print(f"  {contract:25} B:{data['buy_qty']:3} S:{data['sell_qty']:3}  ${data['total']:10.2f}")

print(f"\n✗ ONLY BUY (open positions): {len(only_buy)}")
for contract, data in sorted(only_buy.items(), key=lambda x: x[1]['total']):
    print(f"  {contract:25} B:{data['buy_qty']:3} S:{data['sell_qty']:3}  ${data['total']:10.2f}")

print(f"\n✗ ONLY SELL (unmatched): {len(only_sell)}")
for contract, data in sorted(only_sell.items(), key=lambda x: x[1]['total'], reverse=True):
    print(f"  {contract:25} B:{data['buy_qty']:3} S:{data['sell_qty']:3}  ${data['total']:10.2f}")

total_completed = sum(v['total'] for v in completed.values())
total_only_buy = sum(v['total'] for v in only_buy.values())
total_only_sell = sum(v['total'] for v in only_sell.values())

print("\n" + "=" * 80)
print("SUMMARY")
print("=" * 80)
print(f"Completed positions total:     ${total_completed:12.2f}")
print(f"Only BUY (open):               ${total_only_buy:12.2f}")
print(f"Only SELL (unmatched):         ${total_only_sell:12.2f}")
print(f"Grand total (all options):     ${total_completed + total_only_buy + total_only_sell:12.2f}")
print(f"Target from Public.com:        $     3,693.32")
print(f"Difference:                    ${total_completed - 3693.32:12.2f}")
