"""Serve the small same-origin milestone interface without adding API operations."""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse


router = APIRouter(include_in_schema=False)
STATIC = Path(__file__).resolve().parents[1] / "static"


@router.get("/")
def index():
    return FileResponse(STATIC / "index.html", media_type="text/html")


@router.get("/static/app.js")
def javascript():
    return FileResponse(STATIC / "app.js", media_type="text/javascript")


@router.get("/static/style.css")
def stylesheet():
    return FileResponse(STATIC / "style.css", media_type="text/css")
