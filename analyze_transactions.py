#!/usr/bin/env python3
"""Analyze exact transaction types for NVDA and SOXL positions"""

import os
import requests
import re
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
print("ANALYZING NVDA_260123 AND SOXL_260130 TRANSACTIONS")
print("=" * 80)

# Find all transactions for these positions
for tx in transactions:
    desc = tx.get('description', '')
    tx_type = tx.get('type', '')
    sub_type = tx.get('subType', '')

    # Only look at TRADE transactions
    if tx_type != 'TRADE':
        continue

    if '260123' in desc or '260130' in desc:
        print(f"\nType: {tx_type} / {sub_type}")
        print(f"Description: {desc}")
        amount = tx.get('netAmount', 0)
        print(f"Net Amount: ${float(amount):.2f}")
        print(f"Timestamp: {tx.get('timestamp', '')}")
        print(f"Order ID: {tx.get('orderId', 'N/A')}")

# Also check if there are any non-TRADE transactions that might be assignment/exercise
print("\n" + "=" * 80)
print("CHECKING FOR NON-TRADE TRANSACTIONS (Assignment/Exercise/Expire)")
print("=" * 80)

for tx in transactions:
    tx_type = tx.get('type', '')
    sub_type = tx.get('subType', '')

    if tx_type != 'TRADE' and tx_type != 'JOURNAL':
        desc = tx.get('description', '')
        if 'NVDA' in desc or 'SOXL' in desc:
            print(f"\nType: {tx_type} / {sub_type}")
            print(f"Description: {desc}")
            amount = tx.get('netAmount', 0)
            print(f"Net Amount: ${float(amount):.2f}")
            print(f"Timestamp: {tx.get('timestamp', '')}")

# Now let's try fetching from December 2025 to see if there were opening transactions
print("\n" + "=" * 80)
print("TRYING: Fetch Dec 2025 - Jan 2026 to find opening transactions")
print("=" * 80)

dec_start = datetime(2025, 12, 1).strftime('%Y-%m-%dT%H:%M:%SZ')
response = requests.get(
    f"https://api.public.com/userapigateway/trading/{account_id}/history",
    params={'start': dec_start, 'end': end_date, 'pageSize': 1000},
    headers={'Authorization': f'Bearer {token}'}
)
history = response.json()
transactions = history.get('transactions', [])

# Find NVDA and SOXL option transactions
nvda_soxl_txs = []
for tx in transactions:
    desc = tx.get('description', '')
    tx_type = tx.get('type', '')
    if tx_type == 'TRADE' and ('NVDA260123' in desc or 'SOXL260130' in desc):
        nvda_soxl_txs.append(tx)

print(f"\nFound {len(nvda_soxl_txs)} transactions for NVDA_260123 and SOXL_260130:")
for tx in sorted(nvda_soxl_txs, key=lambda x: x.get('timestamp', '')):
    desc = tx.get('description', '')
    amount = float(tx.get('netAmount', 0))
    timestamp = tx.get('timestamp', '')
    print(f"\n{timestamp}")
    print(f"  {desc}")
    print(f"  Net Amount: ${amount:.2f}")

# Calculate totals
nvda_total = sum(float(tx.get('netAmount', 0)) for tx in nvda_soxl_txs if 'NVDA260123' in tx.get('description', ''))
soxl_total = sum(float(tx.get('netAmount', 0)) for tx in nvda_soxl_txs if 'SOXL260130' in tx.get('description', ''))

print(f"\nTotals:")
print(f"  NVDA_260123: ${nvda_total:.2f}")
print(f"  SOXL_260130: ${soxl_total:.2f}")
print(f"  Combined: ${nvda_total + soxl_total:.2f}")
