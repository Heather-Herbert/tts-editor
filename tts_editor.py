#!/usr/bin/env python3
"""TTS Editor: a small text editor that can read its contents aloud with Piper TTS."""
# SPDX-License-Identifier: GPL-3.0-or-later

import queue
import re
import shutil
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import lameenc
from piper import PiperVoice, SynthesisConfig

APP_DIR = Path(__file__).resolve().parent
VOICES_DIR = APP_DIR / "voices"
# A sentence ends at . ! ? (plus any closing quotes/brackets) followed by whitespace, or at a
# line end. Requiring whitespace after the full stop keeps "3.14" and "example.com" in one piece.
# A leading list number ("1. ") is kept with its sentence.
SENTENCE_RE = re.compile(r"(?:\d{1,3}[.)][ \t]+)?\S[^\n]*?(?:[.!?]+[\"')\]’”]*(?=\s|$)|$)", re.M)
MARKDOWN_SUFFIXES = {".md", ".markdown", ".mdown", ".mkd"}

# --- Markdown → speakable text ---
_MD_BLOCKS = re.compile(
    r"\A---[ \t]*\n.*?\n(?:---|\.\.\.)[ \t]*$"        # YAML front matter
    r"|^[ ]{0,3}(`{3,}|~{3,}).*?(?:^[ ]{0,3}\1[ \t]*$|\Z)"  # fenced code block
    r"|<!--.*?-->",                                   # HTML comment
    re.S | re.M)
_MD_LINE_PREFIX = re.compile(r"^\s*(?:#{1,6}\s+|>\s?|(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?)+")
_MD_RULE_LINE = re.compile(r"^[\s\-=*_|:]*$")   # ---, ***, ===, table separators
_MD_REF_DEF = re.compile(r"^\s*\[[^\]]+\]:\s+\S+")
_MD_INLINE = [
    (re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~])"), "\x00\\1"),     # escapes (protected)
    (re.compile(r"\[\^[^\]]*\]"), ""),                         # footnote refs
    (re.compile(r"!\[([^\]]*)\]\([^)]*\)"), r"\1"),            # images → alt text
    (re.compile(r"\[([^\]]+)\](?:\([^)]*\)|\[[^\]]*\])"), r"\1"),  # links → link text
    (re.compile(r"<https?://[^>]*>|https?://\S+"), "link"),     # bare/auto links
    (re.compile(r"</?[A-Za-z][^>]*>"), ""),                     # HTML tags
    (re.compile(r"(`+)(.+?)\1"), r"\2"),                        # inline code
    (re.compile(r"(\*\*|__|~~)(?=\S)(.+?)(?<=\S)\1"), r"\2"),   # bold / strikethrough
    (re.compile(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])"), r"\1"),  # *italic*
    (re.compile(r"(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)"), r"\1"),          # _italic_
    (re.compile(r"\s*\|\s*"), ", "),                            # table cells
]


def mask_markdown_blocks(text):
    """Blank out code blocks, front matter and comments, keeping character offsets intact."""
    return _MD_BLOCKS.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), text)


def markdown_to_speech(fragment, at_line_start):
    """Strip Markdown syntax from one sentence so it reads naturally."""
    if at_line_start:
        if _MD_RULE_LINE.match(fragment) or _MD_REF_DEF.match(fragment):
            return ""
        fragment = _MD_LINE_PREFIX.sub("", fragment)
    for pattern, repl in _MD_INLINE:
        fragment = pattern.sub(repl, fragment)
    fragment = fragment.replace("\x00", "")
    return re.sub(r"\s+", " ", fragment).strip(" ,")


def find_player():
    """Return a command that plays raw mono S16LE audio from stdin, given a sample rate."""
    if shutil.which("aplay"):
        return lambda rate: ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-c", "1", "-r", str(rate)]
    if shutil.which("paplay"):
        return lambda rate: ["paplay", "--raw", "--format=s16le", "--channels=1", f"--rate={rate}"]
    if shutil.which("ffplay"):
        return lambda rate: ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
                             "-f", "s16le", "-ch_layout", "mono", "-ar", str(rate), "-"]
    return None


class TTSEditor:
    def __init__(self, root):
        self.root = root
        self.path = None
        self.voice = None
        self.voice_name = None
        self.player_cmd = find_player()
        self.stop_event = threading.Event()
        self.play_thread = None
        self.proc = None
        self.play_gen = 0

        self._build_ui()
        self._load_voice_list()
        self._update_title()
        root.protocol("WM_DELETE_WINDOW", self.quit)

    # ---------- UI ----------
    def _build_ui(self):
        self.root.geometry("800x600")

        menubar = tk.Menu(self.root)
        filemenu = tk.Menu(menubar, tearoff=False)
        filemenu.add_command(label="New", accelerator="Ctrl+N", command=self.new_file)
        filemenu.add_command(label="Open…", accelerator="Ctrl+O", command=self.open_file)
        filemenu.add_command(label="Save", accelerator="Ctrl+S", command=self.save_file)
        filemenu.add_command(label="Save As…", accelerator="Ctrl+Shift+S", command=self.save_as)
        filemenu.add_separator()
        filemenu.add_command(label="Quit", accelerator="Ctrl+Q", command=self.quit)
        menubar.add_cascade(label="File", menu=filemenu)
        speechmenu = tk.Menu(menubar, tearoff=False)
        speechmenu.add_command(label="Play", accelerator="F5", command=self.play)
        speechmenu.add_command(label="Stop", accelerator="Esc", command=self.stop)
        speechmenu.add_separator()
        speechmenu.add_command(label="Export MP3…", accelerator="Ctrl+E", command=self.export_mp3)
        menubar.add_cascade(label="Speech", menu=speechmenu)
        self.root.config(menu=menubar)

        bar = ttk.Frame(self.root, padding=4)
        bar.pack(fill="x")
        self.play_btn = ttk.Button(bar, text="▶ Play", command=self.play)
        self.play_btn.pack(side="left")
        self.stop_btn = ttk.Button(bar, text="■ Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        self.export_btn = ttk.Button(bar, text="⤓ Export MP3", command=self.export_mp3)
        self.export_btn.pack(side="left", padx=(0, 12))

        ttk.Label(bar, text="Voice:").pack(side="left")
        self.voice_var = tk.StringVar()
        self.voice_box = ttk.Combobox(bar, textvariable=self.voice_var, state="readonly", width=28)
        self.voice_box.pack(side="left", padx=(4, 12))

        ttk.Label(bar, text="Speed:").pack(side="left")
        self.speed_var = tk.DoubleVar(value=1.0)
        ttk.Scale(bar, from_=0.5, to=2.0, variable=self.speed_var, length=120,
                  command=lambda _: self.speed_lbl.config(text=f"{self.speed_var.get():.1f}×")
                  ).pack(side="left", padx=4)
        self.speed_lbl = ttk.Label(bar, text="1.0×", width=5)
        self.speed_lbl.pack(side="left")

        # On: read Markdown as prose (skip #, **, link URLs, code blocks…). Auto-set on open.
        self.md_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Markdown", variable=self.md_var).pack(side="left", padx=(12, 0))

        frame = ttk.Frame(self.root)
        frame.pack(fill="both", expand=True)
        self.text = tk.Text(frame, wrap="word", undo=True, font=("TkDefaultFont", 12),
                            padx=8, pady=8)
        scroll = ttk.Scrollbar(frame, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_configure("speaking", background="#fff2a8", foreground="black")
        self.text.bind("<<Modified>>", lambda _: self._update_title())

        self.status = ttk.Label(self.root, text="Ready", anchor="w", padding=(6, 2))
        self.status.pack(fill="x")

        for seq, fn in [("<Control-n>", self.new_file), ("<Control-o>", self.open_file),
                        ("<Control-s>", self.save_file), ("<Control-S>", self.save_as),
                        ("<Control-q>", self.quit), ("<F5>", self.play), ("<Escape>", self.stop),
                        ("<Control-e>", self.export_mp3)]:
            self.root.bind(seq, lambda e, f=fn: (f(), "break")[1])

    def _load_voice_list(self):
        voices = sorted(p.stem for p in VOICES_DIR.glob("*.onnx"))
        self.voice_box["values"] = voices
        if voices:
            self.voice_box.current(0)
        else:
            self.set_status(f"No voices found in {VOICES_DIR} — see README.md")

    def set_status(self, msg):
        self.status.config(text=msg)

    def _update_title(self):
        name = self.path.name if self.path else "Untitled"
        star = "*" if self.text.edit_modified() else ""
        self.root.title(f"{star}{name} — TTS Editor")

    # ---------- File handling ----------
    def _confirm_discard(self):
        if not self.text.edit_modified():
            return True
        ans = messagebox.askyesnocancel("Unsaved changes", "Save changes before continuing?")
        if ans is None:
            return False
        return self.save_file() if ans else True

    def _set_content(self, content):
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)
        self.text.edit_reset()
        self.text.edit_modified(False)
        self._update_title()

    def new_file(self):
        if self._confirm_discard():
            self.stop()
            self.path = None
            self.md_var.set(False)
            self._set_content("")

    def open_file(self):
        if not self._confirm_discard():
            return
        name = filedialog.askopenfilename(filetypes=[
            ("Text and Markdown", "*.txt *.md *.markdown"), ("Markdown", "*.md *.markdown"),
            ("Text files", "*.txt"), ("All files", "*")])
        if not name:
            return
        try:
            content = Path(name).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            messagebox.showerror("Open failed", str(e))
            return
        self.stop()
        self.path = Path(name)
        self.md_var.set(self.path.suffix.lower() in MARKDOWN_SUFFIXES)
        self._set_content(content)
        self.set_status(f"Opened {self.path}" + (" (Markdown)" if self.md_var.get() else ""))

    def save_file(self):
        if self.path is None:
            return self.save_as()
        try:
            self.path.write_text(self.text.get("1.0", "end-1c"), encoding="utf-8")
        except OSError as e:
            messagebox.showerror("Save failed", str(e))
            return False
        self.text.edit_modified(False)
        self._update_title()
        self.set_status(f"Saved {self.path}")
        return True

    def save_as(self):
        md = self.md_var.get()
        name = filedialog.asksaveasfilename(
            defaultextension=".md" if md else ".txt",
            filetypes=[("Markdown", "*.md"), ("Text files", "*.txt"), ("All files", "*")] if md
            else [("Text files", "*.txt"), ("Markdown", "*.md"), ("All files", "*")])
        if not name:
            return False
        self.path = Path(name)
        self.md_var.set(self.path.suffix.lower() in MARKDOWN_SUFFIXES)
        return self.save_file()

    def quit(self):
        if self._confirm_discard():
            self.stop()
            self.root.destroy()

    # ---------- Speech ----------
    def _get_voice(self):
        name = self.voice_var.get()
        if not name:
            raise RuntimeError(f"No voice selected. Put a Piper .onnx voice in {VOICES_DIR}.")
        if self.voice_name != name:
            self.voice = PiperVoice.load(VOICES_DIR / f"{name}.onnx")
            self.voice_name = name
        return self.voice

    def _busy(self):
        return self.play_thread is not None and self.play_thread.is_alive()

    def _segments(self):
        """Sentences of the selection, or of the whole text: (start_offset, end_offset, text)."""
        try:
            start, end = self.text.index("sel.first"), self.text.index("sel.last")
        except tk.TclError:
            start, end = "1.0", "end-1c"
        full = self.text.get("1.0", "end-1c")
        offset = len(self.text.get("1.0", start))
        stop = offset + len(self.text.get(start, end))
        markdown = self.md_var.get()
        if markdown:
            full = mask_markdown_blocks(full)  # on the full text so a selection knows its context
        segments = []
        for m in SENTENCE_RE.finditer(full, offset, stop):
            spoken = m.group()
            if markdown:
                line_start = full.rfind("\n", 0, m.start()) + 1
                at_line_start = not full[line_start:m.start()].strip()
                spoken = markdown_to_speech(spoken, at_line_start)
            if spoken.strip():
                segments.append((m.start(), m.end(), spoken))
        return segments

    def _start_job(self, target, *args):
        """Lock the UI and run a speech job (playback or export) in the background."""
        self.stop_event.clear()
        self.play_btn.config(state="disabled")
        self.export_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.text.config(state="disabled")
        self.set_status("Loading voice…")
        cfg = SynthesisConfig(length_scale=1.0 / self.speed_var.get())
        self.play_thread = threading.Thread(target=target, args=(*args, cfg), daemon=True)
        self.play_thread.start()

    def play(self):
        if self._busy():
            return
        if self.player_cmd is None:
            messagebox.showerror("No audio player", "Install aplay (alsa-utils), paplay or ffplay.")
            return
        segments = self._segments()
        if not segments:
            self.set_status("Nothing to read")
            return
        self._start_job(self._speak, segments)

    def export_mp3(self):
        if self._busy():
            return
        segments = self._segments()
        if not segments:
            self.set_status("Nothing to export")
            return
        name = filedialog.asksaveasfilename(
            defaultextension=".mp3", filetypes=[("MP3 audio", "*.mp3")],
            initialfile=(self.path.stem if self.path else "speech") + ".mp3")
        if not name:
            return
        self._start_job(self._export, segments, Path(name))

    def _export(self, segments, dest, cfg):
        tmp = dest.with_name(dest.name + ".part")
        try:
            voice = self._get_voice()
            enc = lameenc.Encoder()
            enc.set_bit_rate(128)
            enc.set_in_sample_rate(voice.config.sample_rate)
            enc.set_channels(1)
            enc.set_quality(2)  # 2 = high quality, 7 = fastest
            with open(tmp, "wb") as f:
                for i, (_, _, sentence) in enumerate(segments, 1):
                    if self.stop_event.is_set():
                        break
                    self.root.after(0, self.set_status, f"Exporting {i}/{len(segments)}…")
                    for chunk in voice.synthesize(sentence, syn_config=cfg):
                        f.write(enc.encode(chunk.audio_int16_bytes))
                f.write(enc.flush())
            if self.stop_event.is_set():
                tmp.unlink(missing_ok=True)
                msg = "Export cancelled"
            else:
                tmp.replace(dest)
                msg = f"Exported {dest}"
        except Exception as ex:
            tmp.unlink(missing_ok=True)
            msg = f"Export failed: {ex}"
        self.root.after(0, self._done, msg)

    def _synthesize_ahead(self, voice, segments, cfg, out):
        """Producer: synthesize sentences into `out`, staying up to its maxsize ahead of playback."""
        try:
            for i, (s, e, sentence) in enumerate(segments, 1):
                if self.stop_event.is_set():
                    return
                audio = b"".join(c.audio_int16_bytes for c in voice.synthesize(sentence, syn_config=cfg))
                if audio:
                    while not self.stop_event.is_set():
                        try:
                            out.put((i, s, e, audio), timeout=0.1)
                            break
                        except queue.Full:
                            pass
        except Exception as ex:
            self._put_or_give_up(out, ex)
        finally:
            self._put_or_give_up(out, None)

    def _put_or_give_up(self, out, item):
        # Don't block forever if the consumer has already stopped reading.
        while True:
            try:
                out.put(item, timeout=0.1)
                return
            except queue.Full:
                if self.stop_event.is_set():
                    return

    def _speak(self, segments, cfg):
        """Consumer: stream audio into one long-running player so there are no gaps."""
        self.play_gen += 1
        gen = self.play_gen
        try:
            voice = self._get_voice()
            rate = voice.config.sample_rate
            ahead = queue.Queue(maxsize=4)
            threading.Thread(target=self._synthesize_ahead,
                             args=(voice, segments, cfg, ahead), daemon=True).start()
            self.proc = subprocess.Popen(self.player_cmd(rate), stdin=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL)
            play_end = 0.0  # wall-clock time at which the audio written so far finishes
            while True:
                item = ahead.get()
                if item is None or self.stop_event.is_set():
                    break
                if isinstance(item, Exception):
                    raise item
                i, s, e, audio = item
                # If the player ran dry it restarts from now; otherwise this follows on.
                starts = max(play_end, time.monotonic())
                play_end = starts + len(audio) / (2 * rate)
                delay = max(0, int((starts - time.monotonic()) * 1000))
                self.root.after(delay, self._highlight, gen, s, e,
                                f"Speaking {i}/{len(segments)}")
                self.proc.stdin.write(audio)  # blocks while the pipe is full
                self.proc.stdin.flush()
            self.proc.stdin.close()
            self.proc.wait()
            msg = "Stopped" if self.stop_event.is_set() else "Finished"
        except (BrokenPipeError, ValueError, OSError):
            msg = "Stopped" if self.stop_event.is_set() else "Audio player exited"
        except Exception as ex:  # surface model/audio errors in the UI
            msg = f"Error: {ex}"
        finally:
            self.stop_event.set()  # make sure the producer exits too
            if self.proc:
                if self.proc.poll() is None:
                    self.proc.terminate()
                try:
                    self.proc.stdin.close()
                except OSError:
                    pass  # pipe already broken by stop
            self.proc = None
        self.root.after(0, self._done, msg)

    def _highlight(self, gen, s, e, msg):
        if gen != self.play_gen or self.stop_btn.instate(["disabled"]):
            return  # stale highlight from a finished or stopped playback
        self.text.tag_remove("speaking", "1.0", "end")
        self.text.tag_add("speaking", f"1.0+{s}c", f"1.0+{e}c")
        self.text.see(f"1.0+{s}c")
        self.set_status(msg)

    def _done(self, msg):
        self.text.tag_remove("speaking", "1.0", "end")
        self.text.config(state="normal")
        self.play_btn.config(state="normal")
        self.export_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.set_status(msg)

    def stop(self):
        self.stop_event.set()
        proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()


def main():
    root = tk.Tk()
    TTSEditor(root)
    root.mainloop()


if __name__ == "__main__":
    main()
