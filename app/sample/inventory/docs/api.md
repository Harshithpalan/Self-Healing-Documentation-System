# API Reference

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/items` | List items, optionally filtered by `min_quantity` |
| `POST` | `/items` | Create an item (requires API key header) |
| `GET` | `/items/{id}` | Fetch a single item |
| `PUT` | `/items/{id}` | Update the stock quantity |
| `DELETE` | `/items/{id}` | Delete an item |
| `GET` | `/items/stats` | Inventory value summary |

## Python usage

```python
from app.service import InventoryService, calculate_total

service = InventoryService(database_url="sqlite:///inventory.db")
item = service.add_item(name="Widget", qty=5, price=9.99)
service.delete_item(item.id)
print(calculate_total([item]))
```

The service exposes `InventoryService.add_items(name, quantity)` and
`InventoryService.remove_item(item_id)`. Totals are computed with `calculate_total(items)`.
