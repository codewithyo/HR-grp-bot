#!/usr/bin/env python3
"""
Local development server for the Koyeb runtime.

Usage:
    python run_local.py

Then expose with ngrok:
    ngrok http 8000

Set webhook manually:
    curl https://<ngrok-url>/api/setup_webhook
"""

import os

# Set env vars here for local dev (don't commit real values)
os.environ.setdefault("API_ID",       "YOUR_API_ID")
os.environ.setdefault("API_HASH",     "YOUR_API_HASH")
os.environ.setdefault("BOT_TOKEN",    "YOUR_BOT_TOKEN")
os.environ.setdefault("OWNER_ID",     "YOUR_OWNER_ID")
os.environ.setdefault("LOG_GROUP_ID", "YOUR_LOG_GROUP_ID")

import uvicorn
from api.index import app

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
  
