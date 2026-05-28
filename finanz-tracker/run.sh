#!/bin/sh
export DB_PATH="/share/finanz-tracker/finanz.db"
mkdir -p /share/finanz-tracker
cd /app
exec python3 app.py
