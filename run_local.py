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
os.environ.setdefault("API_ID",       "9605646")
os.environ.setdefault("API_HASH",     "822d45aa548a53682a458efa1933e4c9")
os.environ.setdefault("BOT_TOKEN",    "8707026358:AAF-DAP96HYUZe6d4aQ7g_d3lyE97q8KOBo")
os.environ.setdefault("OWNER_ID",     "8457503781")
os.environ.setdefault("LOG_GROUP_ID", "-1003834934514")

import uvicorn
from api.index import app

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
  
