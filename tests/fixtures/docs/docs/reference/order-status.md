---
title: "Order status codes"
owner: Data Team
odcl:
  kind: reference
  describes:
    - key: column:warehouse.sales.orders.status_code
      section: status-codes
      says: "Closed = status_code IN ('C', 'X')"
    - key: table:warehouse.ref.status_lookup
    - key: column:warehouse.sales.orders.legacy_flag
    - key: table:warehouse.sales.orders
      section: shipping-windows
---
# Order status codes

Orders move through statuses as fulfilment progresses. The warehouse writes one code per order.

## Status codes

| Code | Meaning |
|---|---|
| O | Open |
| C | Closed |
| X | Cancelled, which also counts as closed |

## Refund handling

A refund reopens an order: its code goes back to `O`.

```sql
-- # not a heading
SELECT * FROM warehouse.sales.orders WHERE status_code IN ('C', 'X')
```
