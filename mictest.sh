#!/bin/bash
cd "$(dirname "$0")"
DEV=$(./.venv/bin/python -c 'import agent; print(agent.MIC)' 2>/dev/null | tail -1)
{
echo "=== Using device $DEV — Recording 3 seconds, TALK NOW! ==="
ffmpeg -y -f avfoundation -i "$DEV" -t 3 -ac 1 -ar 16000 mictest.wav 2>ff_err.txt
echo "exit=$?  size=$(stat -f%z mictest.wav 2>/dev/null) bytes"
ffmpeg -i mictest.wav -af volumedetect -f null - 2>&1 | grep -E "mean_volume|max_volume"
echo "--- errors ---"; tail -3 ff_err.txt
echo "=== DONE — tell Claude 'done' ==="
} | tee mictest.log
