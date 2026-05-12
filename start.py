#!/usr/bin/env python3
"""Koyeb runtime entrypoint."""

import os

import uvicorn

from api.index import app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
