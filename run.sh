#!/bin/sh
cd "$(dirname "$0")" && exec .venv/bin/python tts_editor.py "$@"
