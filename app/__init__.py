"""TRMNL-like color e-ink display system - backend package.

This package contains the shared services used by the FastAPI backend, the
background pre-render worker, and the Telegram bot. The three of them run
together inside a single asyncio process (see :mod:`app.runtime`).
"""

__version__ = "0.1.0"
