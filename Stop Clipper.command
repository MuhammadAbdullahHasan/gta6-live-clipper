#!/bin/zsh
pkill -f "clipper.py"; sleep 1
pkill -f "streamlink --twitch-disable-ads"
pkill -f "ffmpeg -hide_banner -loglevel error"
pkill -f "ffmpeg -hide_banner -nostats"
echo "Clipper stopped"
