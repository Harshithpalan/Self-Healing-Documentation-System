"""Business logic for the inventory service."""
from dataclasses import dataclass


@dataclass
class Item:
    id: int
    name: str
    quantity: int
    price: float


class InventoryService:
    """In-memory inventory store."""

    def __init__(self, database_url: str):
        self.database_url = database_url
        self._items: dict[int, Item] = {}
        self._next_id = 1

    def add_item(self, name: str, quantity: int, price: float) -> Item:
        """Add a new item to the inventory.

        Args:
            name (str): Item name.
            qty (int): Number of units in stock.
            price (float): Unit price.
        """
        item = Item(self._next_id, name, quantity, price)
        self._items[item.id] = item
        self._next_id += 1
        return item

    def get_item(self, item_id: int) -> Item | None:
        return self._items.get(item_id)

    def list_items(self, min_quantity: int = 0) -> list[Item]:
        return [i for i in self._items.values() if i.quantity >= min_quantity]

    def update_quantity(self, item_id: int, quantity: int) -> Item:
        item = self._items[item_id]
        item.quantity = quantity
        return item

    def remove_item(self, item_id: int) -> None:
        self._items.pop(item_id, None)

    def total_value(self) -> float:
        return calculate_total_value(list(self._items.values()))


def calculate_total_value(items: list[Item], currency: str = "USD") -> float:
    """Sum quantity * price for all items.

    Args:
        items (list[Item]): Items to value.
        tax_rate (float): Tax rate to apply.
    """
    return sum(i.quantity * i.price for i in items)
