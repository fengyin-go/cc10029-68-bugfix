"""回执能力的单数命名入口，实现见 :mod:`app.services.receipts`。"""
from __future__ import annotations

from app.services.receipts import (  # noqa: F401
    RECEIPT_MODULE,
    ReceiptService,
    find_receipt,
    get_receipt,
    issue_receipt,
    latest_receipt,
    query_receipt,
    receipt_book,
    receipt_service,
    receipt_store,
)

__all__ = [
    "RECEIPT_MODULE",
    "ReceiptService",
    "find_receipt",
    "get_receipt",
    "query_receipt",
    "issue_receipt",
    "latest_receipt",
    "receipt_service",
    "receipt_book",
    "receipt_store",
]
