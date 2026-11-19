# Inventory Service

A small REST API for tracking stock items, built with FastAPI.

## Getting started

```bash
pip install -r requirements.txt
export DB_URL=sqlite:///inventory.db
uvicorn app.server:app --reload
```

You can also run `python run.py` for a quick local server.

## Configuration

| Variable | Description |
|---|---|
| `DB_URL` | Database connection string |
| `LOG_LEVEL` | Logging verbosity (default `info`) |

## Usage

Create an item with `POST /items`, then fetch it with `GET /items/{id}`.
Use `list_items(min_qty)` to filter low-stock items programmatically.

See the [API reference](docs/api.md) and the [contributing guide](CONTRIBUTING.md).
