# P&L Calculation Breakdown
**Generated**: January 31, 2026
**Target**: $3,693.32 (from Public.com Cost Basis)
**Calculated**: $3,756.14
**Difference**: +$62.82 (1.7%)

---

## Summary

| Category | Calculated | Target (from Public.com) | Difference |
|----------|-----------|--------------------------|------------|
| Short-term P&L | $3,604.83 | $3,542.01 | +$62.82 |
| Long-term P&L | $151.31 | $151.31 | $0.00 |
| **Total** | **$3,756.14** | **$3,693.32** | **+$62.82** |

---

## Option Positions (Closed)

### Positions with BOTH Buy and Sell in YTD

| Group | Buy | Sell | Net P&L | Status |
|-------|-----|------|---------|--------|
| SOXL_260102 | -$1,395.26 | +$1,579.65 | +$184.39 | Closed |
| NVDA_260109 | -$958.36 | +$1,977.59 | +$1,019.23 | Closed |
| NVDA_260116 | -$250.37 | +$1,425.58 | +$1,175.21 | Closed |
| NVDA_260123 | $0.00 | +$725.06 | +$725.06 | Closed (expired worthless) |
| CRM_260130 | -$773.42 | +$857.54 | +$84.12 | Closed |
| FCX_260206 | -$86.80 | +$129.18 | +$42.38 | Closed |
| GLD_260206 | -$1,891.60 | +$1,948.36 | +$56.76 | Closed |
| NEM_260130 | -$142.88 | +$178.10 | +$35.22 | Closed |
| QQQ_260128 | -$105.88 | +$127.10 | +$21.22 | Closed |
| SLV_260130 | -$157.80 | +$263.18 | +$105.38 | Closed |
| SPY_260122 | -$99.94 | +$65.05 | -$34.89 | Closed |
| NFLX_260320 | -$64.86 | +$185.13 | +$120.27 | Closed |
| USO_260206 | -$90.94 | +$152.05 | +$61.11 | Closed |
| XLE_260306 | -$70.94 | +$81.05 | +$10.11 | Closed |

**Options Subtotal**: $3,414.12

### Option Position Still OPEN (Excluded)

| Group | Buy | Sell | Net P&L | Why Excluded |
|-------|-----|------|---------|--------------|
| SOXL_260130 | $0.00 | +$1,302.63 | +$1,302.63 | Assigned to SOXL stock (2000 shares @ $65, still open) |

---

## Stock Positions (FIFO)

| Symbol | Side | Quantity | Price | Total | Matched With | P&L |
|--------|------|----------|-------|-------|--------------|-----|
| SOXL | BUY | 3500 | $46.76 | -$163,660.03 | SELL 3500 @ $46.81 | +$175.08 |
| NVDA | BUY | 400 | $185.01 | -$74,004.00 | SELL 400 @ $185.05 | +$15.92 |
| NVDA | BUY | 100 | $179.00 | -$17,900.00 | SELL 100 @ $179.66 | +$66.08 |
| NVDA | BUY | 100 | $179.25 | -$17,925.00 | SELL 100 @ $180.10 | +$84.98 |
| SOXL | BUY | 2000 | $65.00 | -$130,000.00 | **OPEN** (from assignment) | Not counted |

**Stocks Subtotal**: $342.06

---

## Total Calculated P&L

- Options (closed): $3,414.12
- Stocks (closed): $342.06
- **Total**: $3,756.14

---

## Public.com Target Breakdown

From Public.com Cost Basis (Updated Jan 30, 2026):

### Short-term: $3,542.01
### Long-term: $151.31
### Total: $3,693.32

---

## Discrepancy Analysis

**Our calculation**: $3,756.14
**Public.com target**: $3,693.32
**Difference**: +$62.82

### Possible Causes:

1. **NVDA_260123** (+$725.06): These puts expired on Jan 23, 2026. Public.com's cost basis might not include them yet if the snapshot was taken before expiry settled.

2. **Stock FIFO method**: Public.com might use a different lot matching method (e.g., specific identification instead of FIFO).

3. **Timing difference**: The Public.com snapshot is from Jan 30, and our calculation is from Jan 31.

---

## Individual Transactions (Closed Options)

### NVDA_260123 (Expired worthless - pure profit)
- SELL 4 NVDA260123P00180000 at 0.86: +$344.53
- SELL 4 NVDA260123P00175000 at 0.95: +$380.53
- **Total**: +$725.06

### Other notable positions:
- SOXL_260102 (put spread): +$184.39
- NVDA_260109 (put spread): +$1,019.23
- NVDA_260116 (put spread): +$1,175.21

---

## Next Steps for Investigation

1. **Verify NVDA_260123 expiry**: Confirm if these puts should be included (expired Jan 23, snapshot Jan 30)

2. **Check stock lot matching**: Verify if FIFO matches Public.com's method

3. **Review timing**: Confirm if the Jan 30 snapshot includes all Jan 30 transactions
