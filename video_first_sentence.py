"""Rename videos using the first spoken sentence near the start."""

from __future__ import annotations

import queue
import re
import subprocess
import sys
import os
import tempfile
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinterdnd2 import DND_FILES, TkinterDnD

VIDEO_TYPES = (".mp4", ".mov", ".mkv", ".avi", ".wmv", ".m4v", ".webm")
PUNCTUATION = re.compile(r"[。！？!?；;]|(?<!\d)\.(?!\d)")
INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOW_SECONDS = 30


def first_sentence(segments: list[dict]) -> str:
    """Return speech through the first sentence ending or a clear pause."""
    accumulated = ""
    previous_end = None
    for segment in segments:
        piece = str(segment.get("text", "")).strip()
        if not piece:
            continue
        start = segment.get("start")
        if accumulated and isinstance(start, (int, float)) and previous_end is not None and start - previous_end >= 0.8:
            break
        accumulated = (accumulated + " " + piece).strip()
        ending = PUNCTUATION.search(accumulated)
        if ending:
            return accumulated[: ending.end()].strip()
        end = segment.get("end")
        previous_end = end if isinstance(end, (int, float)) else None
    return accumulated.strip()


def safe_stem(sentence: str) -> str:
    name = INVALID_FILENAME.sub("", sentence)
    name = re.sub(r"\s+", " ", name).strip(" .")
    name = name[:90].rstrip(" .")
    if name.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        name += "_"
    return name


def available_path(source: Path, stem: str) -> Path:
    candidate = source.with_name(stem + source.suffix)
    if candidate == source or not candidate.exists():
        return candidate
    for number in range(2, 10000):
        candidate = source.with_name(f"{stem} ({number}){source.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError("找不到可用的文件名")


def recognize(video: Path, model) -> str:
    with tempfile.TemporaryDirectory() as folder:
        audio = Path(folder) / "opening.wav"
        bundled_ffmpeg = Path(getattr(sys, "_MEIPASS", "")) / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
        ffmpeg = str(bundled_ffmpeg) if bundled_ffmpeg.is_file() else "ffmpeg"
        if bundled_ffmpeg.is_file():
            os.environ["PATH"] = str(bundled_ffmpeg.parent) + os.pathsep + os.environ.get("PATH", "")
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
                   "-t", str(WINDOW_SECONDS), "-vn", "-ac", "1", "-ar", "16000", str(audio)]
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if completed.returncode:
            raise RuntimeError(completed.stderr.strip() or "无法提取音频")
        result = model.transcribe(str(audio), fp16=False, verbose=False, condition_on_previous_text=False)
    return first_sentence(result.get("segments", []))


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("视频首句重命名")
        self.root.geometry("980x540")
        self.files: list[Path] = []
        self.results: dict[Path, str] = {}
        self.events: queue.Queue = queue.Queue()
        self.busy = False

        toolbar = ttk.Frame(root, padding=12)
        toolbar.pack(fill="x")
        self.add_button = ttk.Button(toolbar, text="添加视频", command=self.add_files)
        self.add_button.pack(side="left")
        self.folder_button = ttk.Button(toolbar, text="添加文件夹", command=self.add_folder)
        self.folder_button.pack(side="left", padx=6)
        self.analyze_button = ttk.Button(toolbar, text="识别首句", command=self.analyze)
        self.analyze_button.pack(side="left", padx=(18, 6))
        self.rename_button = ttk.Button(toolbar, text="按预览重命名", command=self.rename)
        self.rename_button.pack(side="left")
        self.model_name = tk.StringVar(value="base")
        ttk.Label(toolbar, text="识别模型:").pack(side="left", padx=(24, 5))
        self.model_box = ttk.Combobox(toolbar, textvariable=self.model_name, values=("tiny", "base", "small", "medium"), width=9, state="readonly")
        self.model_box.pack(side="left")

        path_bar = ttk.Frame(root, padding=(12, 0, 12, 8))
        path_bar.pack(fill="x")
        ttk.Label(path_bar, text="视频或文件夹位置:").pack(side="left")
        self.path_text = tk.StringVar()
        path_entry = ttk.Entry(path_bar, textvariable=self.path_text)
        path_entry.pack(side="left", fill="x", expand=True, padx=8)
        path_entry.bind("<Return>", lambda _event: self.add_entered_path())
        ttk.Button(path_bar, text="添加位置", command=self.add_entered_path).pack(side="left")
        ttk.Label(root, text="也可以直接把视频或文件夹拖进下方表格。首次使用模型会联网下载；视频内容在本机处理。", padding=(12, 0, 12, 8)).pack(anchor="w")
        columns = ("original", "sentence", "new_name", "status")
        self.table = ttk.Treeview(root, columns=columns, show="headings", selectmode="extended")
        for key, title, width in (("original", "原文件", 250), ("sentence", "识别出的首句", 300), ("new_name", "预览文件名", 280), ("status", "状态", 120)):
            self.table.heading(key, text=title)
            self.table.column(key, width=width, minwidth=90)
        self.table.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        for widget in (root, self.table):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self.on_drop)
        self.status = tk.StringVar(value="请选择视频或文件夹。")
        ttk.Label(root, textvariable=self.status, padding=(12, 0, 12, 12)).pack(anchor="w")
        self.root.after(100, self.drain_events)

    def add_paths(self, paths):
        known = set(self.files)
        for raw in paths:
            path = Path(raw).resolve()
            if path.suffix.lower() in VIDEO_TYPES and path.is_file() and path not in known:
                self.files.append(path)
                known.add(path)
                self.table.insert("", "end", iid=str(path), values=(path.name, "", "", "待识别"))
        self.status.set(f"已添加 {len(self.files)} 个视频。")

    def add_files(self):
        paths = filedialog.askopenfilenames(title="选择视频", filetypes=[("视频文件", " ".join("*" + x for x in VIDEO_TYPES)), ("所有文件", "*.*")])
        self.add_paths(paths)

    def add_folder(self):
        folder = filedialog.askdirectory(title="选择视频文件夹")
        if folder:
            self.add_paths(path for path in Path(folder).iterdir() if path.is_file())

    def add_locations(self, locations):
        files = []
        for location in locations:
            path = Path(location.strip().strip('"')).expanduser()
            if path.is_file():
                files.append(path)
            elif path.is_dir():
                files.extend(item for item in path.iterdir() if item.is_file())
            else:
                messagebox.showwarning("无效位置", f"找不到文件或文件夹：\n{path}")
        self.add_paths(files)

    def add_entered_path(self):
        value = self.path_text.get().strip()
        if value:
            self.add_locations([value])
            self.path_text.set("")

    def on_drop(self, event):
        self.add_locations(self.root.tk.splitlist(event.data))

    def set_busy(self, busy: bool):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for button in (self.add_button, self.folder_button, self.analyze_button, self.rename_button):
            button.configure(state=state)
        self.model_box.configure(state="disabled" if busy else "readonly")

    def analyze(self):
        targets = [path for path in self.files if path.exists()]
        if not targets:
            messagebox.showinfo("提示", "请先添加视频。")
            return
        self.set_busy(True)
        self.status.set("正在加载语音模型……")
        threading.Thread(target=self.analyze_worker, args=(targets, self.model_name.get()), daemon=True).start()

    def analyze_worker(self, targets: list[Path], model_name: str):
        try:
            import whisper
            model = whisper.load_model(model_name)
            for index, path in enumerate(targets, 1):
                self.events.put(("progress", f"正在识别 {index}/{len(targets)}：{path.name}"))
                try:
                    sentence = recognize(path, model)
                    self.events.put(("result", path, sentence, ""))
                except Exception as error:
                    self.events.put(("result", path, "", str(error)))
        except Exception as error:
            self.events.put(("error", f"加载模型失败：{error}"))
        finally:
            self.events.put(("done",))

    def drain_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "progress":
                    self.status.set(event[1])
                elif kind == "result":
                    _, path, sentence, error = event
                    stem = safe_stem(sentence)
                    if stem:
                        self.results[path] = stem
                        preview = available_path(path, stem).name
                        state = "可重命名"
                    else:
                        self.results.pop(path, None)
                        preview = ""
                        state = ("失败：" + error[:70]) if error else "未识别到语音"
                    self.table.item(str(path), values=(path.name, sentence, preview, state))
                elif kind == "error":
                    messagebox.showerror("识别失败", event[1])
                elif kind == "done":
                    self.set_busy(False)
                    self.status.set("识别完成。请检查预览，再点击“按预览重命名”。")
        except queue.Empty:
            pass
        self.root.after(100, self.drain_events)

    def rename(self):
        targets = [(path, self.results[path]) for path in self.files if path in self.results]
        if not targets:
            messagebox.showinfo("提示", "没有可重命名的视频，请先识别。")
            return
        if not messagebox.askyesno("确认重命名", f"将重命名 {len(targets)} 个视频。确认已检查预览？"):
            return
        done = 0
        for source, stem in targets:
            try:
                if not source.exists():
                    raise FileNotFoundError("原文件不存在")
                destination = available_path(source, stem)
                if destination != source:
                    source.rename(destination)
                self.table.item(str(source), values=(source.name, self.table.set(str(source), "sentence"), destination.name, "已完成"))
                done += 1
            except Exception as error:
                self.table.set(str(source), "status", f"失败：{str(error)[:70]}")
            finally:
                self.results.pop(source, None)
        self.status.set(f"重命名完成：{done}/{len(targets)} 个视频。")


if __name__ == "__main__":
    window = TkinterDnD.Tk()
    App(window)
    window.mainloop()
