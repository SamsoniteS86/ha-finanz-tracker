#!/bin/sh
export DB_PATH="/data/finanz.db"
cd /app
exec python3 app.py
