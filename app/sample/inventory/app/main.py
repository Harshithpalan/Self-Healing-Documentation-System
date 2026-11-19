"""Inventory Service: REST API for tracking stock items."""
import os

from fastapi import FastAPI, Header, HTTPException

from .service import InventoryService

app = FastAPI(title="Inventory Service")
service = InventoryService(os.getenv("DATABASE_URL", "sqlite:///inventory.db"))
API_KEY = os.getenv("INVENTORY_API_KEY")
LOG_LEVEL = os.getenv("LOG_LEVEL", "info")


def require_key(key: str | None) -> None:
    if API_KEY and key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


@app.get("/items")
def list_items(min_quantity: int = 0):
    return service.list_items(min_quantity=min_quantity)


@app.post("/items", status_code=201)
def create_item(name: str, quantity: int, price: float, x_api_key: str | None = Header(None)):
    require_key(x_api_key)
    return service.add_item(name, quantity, price)


@app.get("/items/{item_id}")
def get_item(item_id: int):
    item = service.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return item


@app.patch("/items/{item_id}")
def update_quantity(item_id: int, quantity: int):
    return service.update_quantity(item_id, quantity)


@app.delete("/items/{item_id}", status_code=204)
def delete_item(item_id: int):
    service.remove_item(item_id)


@app.get("/items/stats/summary")
def stats():
    return {"total_value": service.total_value()}


@app.get("/health")
def health():
    return {"status": "ok"}
