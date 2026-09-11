#!/bin/bash
echo "Stopping Zamal AI Operator services..."
lsof -ti :5678 | xargs kill -9 2>/dev/null
lsof -ti :8787 | xargs kill -9 2>/dev/null
echo "Stopped n8n and CPC Inspector."
read -p "Press Enter to close..."
