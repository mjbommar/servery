"""Pinned FastAPI application used only by the optional comparison image."""

from __future__ import annotations

from fastapi import FastAPI

app = FastAPI()


@app.get("/fastapi/items/{item_id}")
async def item(item_id: int, q: str = "") -> dict[str, object]:
    """Exercise FastAPI/Pydantic path and query validation plus serialization."""
    return {"framework": "fastapi", "item_id": item_id, "q": q}
