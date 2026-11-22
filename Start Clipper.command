#!/bin/zsh
cd "$(dirname "$0")"
if [ -e .disabled ]; then echo "Clipper is disabled (delete the .disabled file to enable)."; exit 0; fi
if pgrep -f "Python clipper.py" >/dev/null; then echo "Clipper already running"; else
nohup caffeinate -is python3 clipper.py >> logs/console.log 2>&1 &
echo "Clipper started (Mac will not idle-sleep)"; fi
sleep 2; tail -5 logs/clipper.log
