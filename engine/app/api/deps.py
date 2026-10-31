"""Shared API dependencies."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Query

from ..core.database import get_db  # re-exported for routers

__all__ = ["get_db", "PageParams", "page_params"]


@dataclass
class PageParams:
    page: int
    page_size: int


def page_params(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
) -> PageParams:
    return PageParams(page=page, page_size=page_size)


def paginate_meta(total: int, page: int, page_size: int) -> dict:
    pages = (total + page_size - 1) // page_size if page_size else 1
    return {"total": total, "page": page, "page_size": page_size, "pages": max(1, pages)}
