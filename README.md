# TTS Editor

A small text editor (Tkinter) that reads text aloud with [Piper](https://github.com/OHF-Voice/piper1-gpl), a fast local neural TTS model. It runs offline on the CPU.

## Run

    ./run.sh

## Features

- New / Open / Save / Save As for text and Markdown files (Ctrl+N / Ctrl+O / Ctrl+S / Ctrl+Shift+S)
- Play (F5) reads the selected text. With no selection, it reads the whole file.
- Stop (Esc)
- Highlights the sentence it is reading
- Choose a voice and a speed (0.5×–2×)
- Markdown support: opening a `.md` file turns on the **Markdown** toggle. The app then reads the text, not the formatting. It skips `#`, `**`, `*`, list markers, table pipes, link URLs, code blocks, front matter and HTML comments. The editor still shows and saves the original Markdown. You can switch the toggle on or off by hand at any time.
- Export MP3 (Ctrl+E) saves the selection, or the whole text, as an MP3 file. It uses the chosen voice and speed.

## Setup

You need Python 3.9 or later with Tkinter (on Debian or Ubuntu: `sudo apt install python3-tk`). You also need an audio player: `aplay`, `paplay` or `ffplay`.

    git clone https://github.com/Heather-Herbert/tts-editor.git
    cd tts-editor
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    .venv/bin/python -m piper.download_voices --data-dir voices en_GB-alba-medium

Then start the app with `./run.sh`.

## Voices

The app lists every `.onnx` voice in `voices/`. To add more voices, download them with the same command. The voice list is at
https://huggingface.co/rhasspy/piper-voices. Examples: `en_US-lessac-medium`, `en_GB-northern_english_male-medium`.

## License

This project is licensed under the GNU General Public License v3.0. See [LICENSE](LICENSE). It uses [Piper](https://github.com/OHF-Voice/piper1-gpl), which is also licensed under GPL-3.0.
