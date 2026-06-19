from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from trades_core import (
    build_dashboard_data,
    fetch_flex_xml,
    load_trade_notes,
    save_trade_notes,
    summarize_xml_tags,
)


app = FastAPI(title="Trades Dashboard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class NotesPayload(BaseModel):
    notes: dict


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/dashboard")
def dashboard(refresh_market: bool = Query(False)) -> dict:
    xml_text, flex_debug = fetch_flex_xml()
    if not xml_text:
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Unable to fetch data from IB Flex.",
                "flex_debug": flex_debug,
            },
        )
    try:
        payload = build_dashboard_data(xml_text, refresh_market=refresh_market)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Unable to build dashboard data.",
                "error": str(exc),
                "xml_tags": summarize_xml_tags(xml_text),
            },
        ) from exc
    payload["flex_debug"] = flex_debug
    return payload


@app.get("/api/notes")
def notes() -> dict:
    return load_trade_notes()


@app.put("/api/notes")
def update_notes(payload: NotesPayload) -> dict:
    save_trade_notes(payload.notes)
    return {"status": "saved"}
