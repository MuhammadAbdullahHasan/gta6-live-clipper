#!/bin/zsh
cd "$(dirname "$0")"; cat logs/status.txt; echo; tail -25 logs/clipper.log
