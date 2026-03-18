#!/bin/bash
# One command to update and restart the app from GitHub
# Usage: bash update.sh

echo "Fetching latest from GitHub..."
git fetch origin
git reset --hard origin/main

echo "Copying files..."
cp Transport/app.py .
cp Transport/bus_route_optimizer.py .

echo "Restarting app..."
fuser -k 5000/tcp 2>/dev/null || true
sleep 1
python3 app.py
