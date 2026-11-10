#!/usr/bin/env python3
"""
Art-Net JSON sequence recorder (local UDP server with a small GUI).

Listens for Art-Net (ArtDMX) packets on a given IP/port (default 127.0.0.1:6454),
records the stream from TouchDesigner / Madrix 5 at a fixed frame rate and saves it as JSON:
a header (all values stored as strings) plus "sequence": {"data": [...]} with floats in 0..1.

Run:      python main.py        (Python 3.9+, no extra packages)
Build:    pyinstaller --onefile --noconsole main.py
"""

import json
import os
import re
import socket
import threading
import time

try:
    import tkinter as tk
    from tkinter import filedialog
except ImportError:  # the core can still be imported without tkinter (for tests)
    tk = None

APPV = "1.0.0"
ARTNET_ID = b"Art-Net\x00"
OP_DMX = 0x5000


# ----------------------------------------------------------------------------
#  Core: Art-Net parsing, receiving, recording, post-processing, export
# ----------------------------------------------------------------------------
def parse_artdmx(data):
    """ArtDMX packet -> (port_address, sequence, dmx_bytes), or None if it is not ArtDMX."""
    if len(data) < 18 or data[:8] != ARTNET_ID:
        return None
    if (data[8] | (data[9] << 8)) != OP_DMX:
        return None
    length = (data[16] << 8) | data[17]
    port_address = ((data[15] & 0x7F) << 8) | data[14]  # Net:SubNet:Universe (0-based)
    return port_address, data[12], bytes(data[18 : 18 + length])


class ArtNetServer:
    def __init__(self):
        self.lock = threading.Lock()
        self.latest = {}  # port_address -> bytes(512), most recent data
        self.uni_stats = {}  # port_address -> {"count": int, "length": int}
        self.total_packets = 0
        self.ignored_packets = 0
        self.total_bytes = 0
        self.last_packet_time = 0.0
        self.running = False
        self.error = None  # set when the socket fails while the server is running
        self.host = ""
        self.port = 0
        self._sock = None
        self._thread = None

    def start(self, host, port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((host, port))  # raises OSError if the port is taken
        except OSError:
            sock.close()
            raise
        sock.settimeout(0.2)
        with self.lock:
            self.latest.clear()
            self.uni_stats.clear()
            self.total_packets = self.ignored_packets = self.total_bytes = 0
            self.last_packet_time = 0.0
        self.error = None
        self._sock, self.host, self.port, self.running = sock, host, port, True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._sock:
            self._sock.close()
        self._sock = self._thread = None

    def _loop(self):
        sock = self._sock
        while self.running:
            try:
                data, _ = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError as e:
                if self.running:  # not a normal shutdown
                    self.error = "Server socket error: %s" % e
                    self.running = False
                break
            pkt = parse_artdmx(data)
            now = time.perf_counter()
            with self.lock:
                self.total_packets += 1
                self.total_bytes += len(data)
                self.last_packet_time = now
                if pkt is None:
                    self.ignored_packets += 1
                    continue
                port_address, _seq, dmx = pkt
                self.latest[port_address] = dmx.ljust(512, b"\x00")[:512]
                st = self.uni_stats.setdefault(port_address, {"count": 0, "length": 0})
                st["count"] += 1
                st["length"] = len(dmx)

    def snapshot(self):
        with self.lock:
            return (
                dict(self.latest),
                {k: dict(v) for k, v in self.uni_stats.items()},
                self.total_packets,
                self.ignored_packets,
                self.total_bytes,
                self.last_packet_time,
            )


def build_spans(
    start_universe, start_channel, channels_per_chip, chips, chips_per_universe=0
):
    """Lay chips out over universes: [(universe, first_channel0, channel_count, offset_in_frame)]."""
    if channels_per_chip < 1 or channels_per_chip > 512:
        raise ValueError("Channels per chip must be between 1 and 512")
    if not 1 <= start_channel <= 512:
        raise ValueError("Start channel must be between 1 and 512")
    cap = 512 // channels_per_chip
    per_u = min(chips_per_universe, cap) if chips_per_universe > 0 else cap
    spans, placed, uni, ch = [], 0, start_universe, start_channel - 1
    while placed < chips:
        fit = min(per_u, (512 - ch) // channels_per_chip)
        if fit <= 0:
            raise ValueError(
                "Not a single chip fits into the universe: lower the start channel"
            )
        n = min(fit, chips - placed)
        spans.append((uni, ch, n * channels_per_chip, placed * channels_per_chip))
        placed += n
        uni += 1
        ch = 0
    return spans


class Recorder:
    """Samples the latest server data at a fixed fps and collects frames (bytes 0..255)."""

    def __init__(self, server):
        self.server = server
        self.frames = []
        self.stride = 0
        self.fps = 30.0
        self.max_frames = 0
        self.spans = []
        self.state = "idle"  # idle | waiting | recording | done
        self.missing = 0  # frames where at least one mapped universe had no data
        self.late = 0  # frames sampled more than 1.5 periods late
        self.elapsed = 0.0
        self.error = None  # set if the recording thread fails
        self._stop = threading.Event()
        self._thread = None

    def start(self, spans, stride, fps, max_frames=0, wait_first_packet=True):
        self.frames, self.spans, self.stride = [], spans, stride
        self.fps, self.max_frames = float(fps), int(max_frames)
        self.missing = self.late = 0
        self.elapsed = 0.0
        self.error = None
        self._stop.clear()
        self.state = "waiting" if wait_first_packet else "recording"
        self._thread = threading.Thread(
            target=self._run, args=(wait_first_packet,), daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _sample(self):
        with self.server.lock:
            latest = self.server.latest
            out, miss = bytearray(self.stride), False
            for uni, ch, n, off in self.spans:
                buf = latest.get(uni)
                if buf is None:
                    miss = True
                    continue
                out[off : off + n] = buf[ch : ch + n]
        return bytes(out), miss

    def _run(self, wait_first_packet):
        try:
            self._capture(wait_first_packet)
        except Exception as e:  # keep the GUI alive and report the problem
            self.error = "Recorder failed: %s" % e
        finally:
            self.state = "done"

    def _capture(self, wait_first_packet):
        if wait_first_packet:
            while not self._stop.is_set():
                with self.server.lock:
                    have = any(s[0] in self.server.latest for s in self.spans)
                if have:
                    break
                time.sleep(0.005)
            if self._stop.is_set():
                return
            self.state = "recording"
        period = 1.0 / self.fps
        t0 = time.perf_counter()
        k = 0
        while not self._stop.is_set():
            target = t0 + k * period
            while not self._stop.is_set():
                dt = target - time.perf_counter()
                if dt <= 0:
                    break
                time.sleep(dt - 0.002 if dt > 0.004 else 0)
            if self._stop.is_set():
                break
            now = time.perf_counter()
            if now - target > 1.5 * period:
                self.late += 1
            frame, miss = self._sample()
            self.frames.append(frame)
            if miss:
                self.missing += 1
            k += 1
            self.elapsed = now - t0
            if self.max_frames and k >= self.max_frames:
                break


def postprocess(frames, skip=0, blend=0, final=None):
    """
    Trim and loop-blend a recording.

    skip   - number of first recorded frames to drop.
    blend  - number of extra frames recorded after the final frame; the first `blend` frames of the
             result fade from that continuation into the original frames, so the last frame flows into
             the first one without a jump.
    final  - number of frames in the result (default: everything that is left after skip and blend).
    """
    frames = frames[skip:]
    if final is None:
        final = len(frames) - blend
    if final <= 0 or final + blend > len(frames):
        raise ValueError(
            "Not enough recorded frames: need %d, have %d"
            % (max(final, 0) + blend + skip, len(frames) + skip)
        )
    if blend > final:
        raise ValueError("Loop blend must not exceed the number of exported frames")
    out = [bytearray(f) for f in frames[:final]]
    for i in range(blend):
        w = i / float(blend)
        tail, head = frames[final + i], out[i]
        for j in range(len(head)):
            head[j] = int(round(tail[j] * (1.0 - w) + head[j] * w))
    return [bytes(f) for f in out]


_LUT = [json.dumps(round(i / 255.0, 3)) for i in range(256)]  # 0 -> "0.0", 255 -> "1.0"


def fmt_num(v):
    v = float(v)
    return str(int(v)) if v.is_integer() else ("%g" % v)


def write_json(path, meta, frames, stride, fps):
    count = len(frames)
    header = [
        ("model_name", meta["model_name"]),
        ("model_chips_tag", meta["model_chips_tag"]),
        ("model_chips_count", str(meta["model_chips_count"])),
        ("sequence_name", meta["sequence_name"]),
        ("sequence_version", meta["sequence_version"]),
        ("sequence_duration_sec", str(round(count / float(fps), 3))),
        ("sequence_fps", fmt_num(fps)),
        ("sequence_frame_count", str(count)),
        ("sequence_frame_stride", str(stride)),
        ("developer_notes", meta["developer_notes"]),
    ]
    lines = ["{"]
    for k, v in header:
        lines.append(
            "  %s: %s,"
            % (json.dumps(k, ensure_ascii=False), json.dumps(v, ensure_ascii=False))
        )
    lines.append('  "sequence": {')
    lines.append('    "data": [')
    rows = [", ".join(_LUT[b] for b in fr) for fr in frames]
    lines.append(",\n".join("      " + r for r in rows))
    lines.append("    ]")
    lines.append("  }")
    lines.append("}")
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fp:
        fp.write("\n".join(lines) + "\n")
    return os.path.getsize(path)


def path_key(path):
    """Normalized form of a path, for comparing two paths."""
    return os.path.normcase(os.path.abspath(path))


def next_free_path(path):
    """sequence_1.json -> sequence_2.json (first name that does not exist yet)."""
    folder, name = os.path.split(path)
    stem, ext = os.path.splitext(name)
    m = re.match(r"^(.*?)(\d+)$", stem)
    if m:
        base, num, width = m.group(1), int(m.group(2)), len(m.group(2))
    else:
        base, num, width = stem + "_", 0, 1
    while True:
        num += 1
        cand = os.path.join(folder, "%s%s%s" % (base, str(num).zfill(width), ext))
        if not os.path.exists(cand):
            return cand


# ----------------------------------------------------------------------------
#  Window
# ----------------------------------------------------------------------------
BG, FIELD, FIELD_RO, BORDER = "#141414", "#2b2b2b", "#222222", "#444444"
FG, FG_DIM = "#e6e6e6", "#8f8f8f"
BTN, BTN_HOVER = "#343434", "#404040"
RED, RED_BORDER = "#8f2d2d", "#5e1c1c"
ERR_FG, WARN_FG = "#ff8f8f", "#e3b341"
MAX_ERRORS = 6
FIELDS = [
    ("Model Name", "model_name", "Crystal Links Model 1"),
    ("Model Chips Tag", "model_chips_tag", "crystal_links_model_1_chip"),
    ("Model Chips Amount", "model_chips_count", "104"),
    ("Sequence Name", "sequence_name", "Random Colored Loop Flashing"),
    ("Sequence Version", "sequence_version", "1.0"),
    ("Sequence Duration (0 = manual)", "sequence_duration_sec", "6.0"),
    ("Sequence Fps", "sequence_fps", "30"),
    ("Sequence Frame Count", "sequence_frame_count", ""),
    ("Sequence Frame Stride", "sequence_frame_stride", ""),
    ("Developer Notes", "developer_notes", "Notes by developer or smb who worked..."),
]
READONLY = {"sequence_frame_count", "sequence_frame_stride"}
MAPPING = [
    ("Start Universe", "start_universe", "1"),
    ("Start channel", "start_channel", "1"),
    ("Channels per chip (3 = RGB, 4 = RGBW)", "channels_per_chip", "3"),
    ("Chips per universe (0 = auto)", "chips_per_universe", "0"),
]


class ChoiceDialog(tk.Toplevel if tk else object):
    """Small dark modal dialog with a message and a row of buttons; result is the button value."""

    def __init__(self, parent, title, message, buttons):
        super().__init__(parent)
        self.result = None
        self.title(title)
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(parent)
        tk.Label(
            self,
            text=message,
            bg=BG,
            fg=FG,
            justify="left",
            anchor="w",
            wraplength=500,
            padx=22,
            pady=18,
            font=("Segoe UI", 10),
        ).pack(fill="x")
        row = tk.Frame(self, bg=BG)
        row.pack(padx=16, pady=(0, 16))
        for label, value in buttons:
            tk.Button(
                row,
                text=label,
                command=lambda v=value: self._pick(v),
                bg=BTN,
                fg=FG,
                activebackground=BTN_HOVER,
                activeforeground=FG,
                relief="flat",
                bd=0,
                highlightthickness=1,
                highlightbackground=BORDER,
                cursor="hand2",
                font=("Segoe UI", 10),
            ).pack(side="left", padx=5, ipadx=12, ipady=6)
        self.protocol("WM_DELETE_WINDOW", lambda: self._pick(None))
        self.bind("<Escape>", lambda _e: self._pick(None))
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_reqwidth()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_reqheight()) // 3
        self.geometry("+%d+%d" % (max(x, 0), max(y, 0)))
        try:
            self.wait_visibility()
            self.grab_set()
        except tk.TclError:
            pass
        self.focus_set()

    def _pick(self, value):
        self.result = value
        self.destroy()


def ask_choice(parent, title, message, buttons):
    dlg = ChoiceDialog(parent, title, message, buttons)
    parent.wait_window(dlg)
    return dlg.result


class App(tk.Tk if tk else object):
    def __init__(self):
        super().__init__()
        self.title(f"Art-Net to JSON Sequence Recorder v.{APPV}")
        self.configure(bg=BG)
        self.geometry("1140x900")
        self.minsize(1000, 860)
        self.server = ArtNetServer()
        self.rec = Recorder(self.server)
        self.vars = {k: tk.StringVar(value=d) for _, k, d in FIELDS}
        self.map_vars = {k: tk.StringVar(value=d) for _, k, d in MAPPING}
        self.host_var, self.port_var = tk.StringVar(value="127.0.0.1"), tk.StringVar(
            value="6454"
        )
        self.path_var = tk.StringVar(value=os.path.join(os.getcwd(), "sequence_1.json"))
        self.wait_var = tk.BooleanVar(value=True)
        self.skip_on, self.skip_n = tk.BooleanVar(value=False), tk.StringVar(value="1")
        self.blend_on, self.blend_n = tk.BooleanVar(value=False), tk.StringVar(
            value="30"
        )
        self.last_saved = ""
        self.errors = []  # [(time, message)] shown in the statistics block
        self.saved_path = None  # file written by the last successful save
        self.pending = None  # finished recording that has not been saved yet
        self._approved = None  # path key the user agreed to overwrite
        self._was_recording = False
        self._job = (
            None  # settings of the current recording (target frames, skip, blend)
        )
        self._prev = (time.perf_counter(), 0, {})
        self._rates = (0.0, {})
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(250, self._refresh)

    # ---- widgets -------------------------------------------------------------
    def _lbl(self, parent, text):
        return tk.Label(
            parent,
            text=text,
            bg=FIELD,
            fg=FG,
            anchor="w",
            padx=6,
            pady=5,
            highlightthickness=1,
            highlightbackground=BORDER,
        )

    def _entry(self, parent, var, readonly=False, width=None):
        e = tk.Entry(
            parent,
            textvariable=var,
            bg=FIELD_RO if readonly else FIELD,
            fg=FG_DIM if readonly else FG,
            insertbackground=FG,
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor="#8a8a8a",
            readonlybackground=FIELD_RO,
            disabledbackground=FIELD_RO,
            disabledforeground=FG_DIM,
            selectbackground="#555555",
            selectforeground=FG,
            state="readonly" if readonly else "normal",
        )
        if width:
            e.config(width=width)
        return e

    def _btn(self, parent, command):
        return tk.Button(
            parent,
            command=command,
            bg=BTN,
            fg=FG,
            activebackground=BTN_HOVER,
            activeforeground=FG,
            disabledforeground=FG_DIM,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            font=("Segoe UI", 11),
            cursor="hand2",
        )

    def _check(self, parent, text, var, command=None, boxed=False):
        bg = FIELD if boxed else BG
        kw = (
            dict(highlightthickness=1, highlightbackground=BORDER, padx=6, pady=4)
            if boxed
            else {}
        )
        return tk.Checkbutton(
            parent,
            text=text,
            variable=var,
            command=command,
            bg=bg,
            fg=FG,
            selectcolor=FIELD,
            activebackground=bg,
            activeforeground=FG,
            anchor="w",
            **kw,
        )

    def _build(self):
        outer = tk.Frame(
            self, bg=BG, highlightthickness=2, highlightbackground="#bdbdbd"
        )
        outer.pack(fill="both", expand=True, padx=14, pady=14)
        outer.columnconfigure(0, weight=11, uniform="cols")
        outer.columnconfigure(1, weight=9, uniform="cols")
        outer.rowconfigure(0, weight=1)

        left = tk.Frame(outer, bg=BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(22, 12), pady=12)
        left.columnconfigure(1, weight=1)
        title = dict(bg=BG, fg=FG, font=("Segoe UI", 11))
        tk.Label(left, text="JSON Fields:", **title).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8)
        )
        for i, (label, key, _d) in enumerate(FIELDS, start=1):
            self._lbl(left, label).grid(
                row=i, column=0, sticky="nsew", padx=(0, 10), pady=1
            )
            self._entry(left, self.vars[key], readonly=key in READONLY).grid(
                row=i, column=1, sticky="nsew", pady=1, ipady=5
            )
        r = len(FIELDS) + 1
        tk.Label(left, text="Input mapping:", **title).grid(
            row=r, column=0, columnspan=2, sticky="w", pady=(14, 6)
        )
        for j, (label, key, _d) in enumerate(MAPPING):
            self._lbl(left, label).grid(
                row=r + 1 + j, column=0, sticky="nsew", padx=(0, 10), pady=1
            )
            self._entry(left, self.map_vars[key]).grid(
                row=r + 1 + j, column=1, sticky="nsew", pady=1, ipady=5
            )
        r2 = r + 1 + len(MAPPING)
        tk.Label(left, text="Post-processing (off by default):", **title).grid(
            row=r2, column=0, columnspan=2, sticky="w", pady=(14, 6)
        )
        self._check(
            left, "Skip first frames (count)", self.skip_on, self._sync_post, boxed=True
        ).grid(row=r2 + 1, column=0, sticky="nsew", padx=(0, 10), pady=1)
        self.skip_entry = self._entry(left, self.skip_n)
        self.skip_entry.grid(row=r2 + 1, column=1, sticky="nsew", pady=1, ipady=5)
        self._check(
            left, "Loop blend (frames)", self.blend_on, self._sync_post, boxed=True
        ).grid(row=r2 + 2, column=0, sticky="nsew", padx=(0, 10), pady=1)
        self.blend_entry = self._entry(left, self.blend_n)
        self.blend_entry.grid(row=r2 + 2, column=1, sticky="nsew", pady=1, ipady=5)
        tk.Label(
            left,
            text="Extra frames are recorded automatically: skipped ones at the start, blend frames at the end.",
            bg=BG,
            fg=FG_DIM,
            anchor="w",
            justify="left",
            wraplength=470,
            font=("Segoe UI", 9),
        ).grid(row=r2 + 3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        pf = tk.Frame(left, bg=BG)
        pf.grid(row=r2 + 4, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        pf.columnconfigure(0, weight=1)
        self._entry(pf, self.path_var).grid(row=0, column=0, sticky="ew", ipady=6)
        tk.Button(
            pf,
            text="Browse...",
            command=self._browse,
            bg=BTN,
            fg=FG,
            relief="flat",
            cursor="hand2",
            activebackground=BTN_HOVER,
            activeforeground=FG,
            highlightthickness=1,
            highlightbackground=BORDER,
        ).grid(row=0, column=1, padx=(10, 0), ipady=3, ipadx=8)
        self._sync_post()

        right = tk.Frame(outer, bg=BG)
        right.grid(row=0, column=1, sticky="nsew", padx=(12, 22), pady=12)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)
        ipf = tk.Frame(right, bg=BG)
        ipf.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        tk.Label(ipf, text="IP", bg=BG, fg=FG).pack(side="left")
        self._entry(ipf, self.host_var, width=14).pack(
            side="left", padx=(6, 14), ipady=3
        )
        tk.Label(ipf, text="UDP port", bg=BG, fg=FG).pack(side="left")
        self._entry(ipf, self.port_var, width=7).pack(side="left", padx=(6, 0), ipady=3)
        self.btn_server = self._btn(right, self._toggle_server)
        self.btn_server.grid(row=1, column=0, sticky="ew", ipady=14, pady=(0, 14))
        self.stats = tk.Text(
            right,
            bg=FIELD,
            fg=FG,
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            font=("Consolas", 10),
            wrap="word",
            height=12,
            width=20,
            padx=10,
            pady=8,
            state="disabled",
            cursor="arrow",
        )
        self.stats.tag_configure("err", foreground=ERR_FG)
        self.stats.tag_configure("warn", foreground=WARN_FG)
        self.stats.grid(row=2, column=0, sticky="nsew", pady=(0, 14))
        self.btn_rec = self._btn(right, self._toggle_record)
        self.btn_rec.grid(row=3, column=0, sticky="ew", ipady=16)
        self._check(right, "Start recording on first packet", self.wait_var).grid(
            row=4, column=0, sticky="w", pady=(8, 0)
        )
        self._update_buttons()

    def _sync_post(self):
        self.skip_entry.config(state="normal" if self.skip_on.get() else "disabled")
        self.blend_entry.config(state="normal" if self.blend_on.get() else "disabled")

    # ---- errors (shown in the statistics block) ---------------------------------
    def _err(self, msg):
        self.errors.append((time.strftime("%H:%M:%S"), msg))
        del self.errors[:-MAX_ERRORS]

    def _clear_errors(self):
        self.errors = []

    # ---- actions -------------------------------------------------------------
    def _browse(self):
        p = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
            initialfile=os.path.basename(self.path_var.get()),
            initialdir=os.path.dirname(self.path_var.get()) or None,
        )
        if p:
            self.path_var.set(p)

    def _ints(self):
        try:
            return {k: int(v.get().strip()) for k, v in self.map_vars.items()}
        except ValueError:
            raise ValueError("Input mapping fields must be whole numbers")

    def _post_values(self):
        try:
            skip = int(self.skip_n.get().strip()) if self.skip_on.get() else 0
            blend = int(self.blend_n.get().strip()) if self.blend_on.get() else 0
        except ValueError:
            raise ValueError("Skip and blend values must be whole numbers")
        if skip < 0 or blend < 0:
            raise ValueError("Skip and blend values must be 0 or greater")
        return skip, blend

    def _toggle_server(self):
        if self.server.running:
            if self.rec.state in ("waiting", "recording"):
                self.rec.stop()
            self.server.stop()
        else:
            try:
                self.server.start(self.host_var.get().strip(), int(self.port_var.get()))
                self._clear_errors()
            except (OSError, ValueError) as e:
                self._err(
                    "Could not start the server: %s. The port may already be in use "
                    "(for example, Unreal Engine listens on the same Art-Net port): "
                    "choose another IP/port." % e
                )
        self._update_buttons()

    def _toggle_record(self):
        if self.rec.state in ("waiting", "recording"):
            self.rec.stop()
            return
        if not self.server.running:
            self._err("Cannot record: start the server first.")
            return
        try:
            m = self._ints()
            skip, blend = self._post_values()
            chips = int(self.vars["model_chips_count"].get())
            fps = float(self.vars["sequence_fps"].get().replace(",", "."))
            dur = float(self.vars["sequence_duration_sec"].get().replace(",", ".") or 0)
            if chips < 1 or fps <= 0 or dur < 0:
                raise ValueError(
                    "Chips amount and fps must be greater than 0, duration must be 0 or greater"
                )
            stride = chips * m["channels_per_chip"]
            spans = build_spans(
                m["start_universe"],
                m["start_channel"],
                m["channels_per_chip"],
                chips,
                m["chips_per_universe"],
            )
            final = int(round(dur * fps)) if dur > 0 else 0
            if final and blend > final:
                raise ValueError(
                    "Loop blend must not exceed the number of exported frames (%d)"
                    % final
                )
        except ValueError as e:
            self._err(str(e))
            return
        if not self._confirm_can_record():
            return
        self._clear_errors()
        self.vars["sequence_frame_stride"].set(str(stride))
        self._job = {"final": final, "skip": skip, "blend": blend}
        self.rec.start(
            spans,
            stride,
            fps,
            max_frames=(skip + final + blend) if final else 0,
            wait_first_packet=self.wait_var.get(),
        )
        self._was_recording = True
        self._update_buttons()

    # ---- protecting recorded data and existing files ---------------------------------
    def _ask_file_conflict(self, path, cancel_label):
        if self.saved_path and path_key(self.saved_path) == path_key(path):
            head = "The previous recording was saved to this file:"
        else:
            head = "This file already exists:"
        msg = "%s\n\n%s\n\nOverwrite it, or save the new recording as a new file?" % (
            head,
            path,
        )
        return ask_choice(
            self,
            "File already exists",
            msg,
            [
                ("Overwrite existing file", "overwrite"),
                ("Save as new file...", "new"),
                (cancel_label, None),
            ],
        )

    def _choose_new_path(self, path):
        suggestion = next_free_path(path)
        return filedialog.asksaveasfilename(
            title="Save as new file",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
            initialdir=os.path.dirname(suggestion) or None,
            initialfile=os.path.basename(suggestion),
        )

    def _resolve_existing(self, path, cancel_label):
        """The target file exists: let the user overwrite it or pick a new name.
        Returns the path to use, or None if the user backed out."""
        while True:
            choice = self._ask_file_conflict(path, cancel_label)
            if choice == "overwrite":
                return path
            if choice == "new":
                new = self._choose_new_path(path)
                if new:
                    return new
                continue  # the save dialog was cancelled: ask again
            return None

    def _confirm_can_record(self):
        """Before a new recording: do not lose an unsaved recording or a saved file."""
        if self.pending is not None:
            choice = ask_choice(
                self,
                "Unsaved recording",
                "The previous recording (%d frames) has not been saved.\n\n"
                "Starting a new recording will discard it."
                % len(self.pending["frames"]),
                [
                    ("Save it now", "save"),
                    ("Discard it", "discard"),
                    ("Cancel", None),
                ],
            )
            if choice == "save":
                self._save_pending()
                if self.pending is not None:
                    return False
            elif choice == "discard":
                self.pending = None
            else:
                return False
        path = self.path_var.get().strip()
        if path and os.path.exists(path) and self._approved != path_key(path):
            chosen = self._resolve_existing(path, "Cancel")
            if chosen is None:
                return False
            self.path_var.set(chosen)
            self._approved = path_key(chosen)
        return True

    def _finish_recording(self):
        self._was_recording = False
        self._update_buttons()
        job, raw = self._job, self.rec.frames
        if not raw:
            self.last_saved = "Nothing to save: 0 frames recorded."
            return
        fps = self.rec.fps
        avail = len(raw) - job["skip"]
        final = (
            min(job["final"], avail - job["blend"])
            if job["final"]
            else avail - job["blend"]
        )
        base = {"fps": fps, "stride": self.rec.stride, "recorded": len(raw)}
        try:
            frames = postprocess(raw, job["skip"], job["blend"], final)
        except ValueError as e:
            # keep the raw recording so that nothing is lost
            self.pending = dict(base, frames=list(raw), skip=0, blend=0)
            self._err("Post-processing failed: %s" % e)
            self.last_saved = (
                "Not saved: the raw recording (%d frames) is kept in memory.\n"
                "Start Record will offer to save it." % len(raw)
            )
            return
        self.vars["sequence_frame_count"].set(str(len(frames)))
        self.vars["sequence_duration_sec"].set(str(round(len(frames) / fps, 3)))
        self.pending = dict(base, frames=frames, skip=job["skip"], blend=job["blend"])
        self._save_pending()

    def _save_pending(self):
        p = self.pending
        if p is None:
            return
        path = self.path_var.get().strip()
        if not path:
            self._err("Not saved: the file path is empty (recording kept in memory).")
            return
        if os.path.exists(path) and self._approved != path_key(path):
            chosen = self._resolve_existing(path, "Don't save yet")
            if chosen is None:
                self.last_saved = (
                    "Not saved: the recording (%d frames) is kept in memory.\n"
                    "Start Record will offer to save it." % len(p["frames"])
                )
                return
            path = chosen
            self.path_var.set(path)
        meta = {
            "model_name": self.vars["model_name"].get(),
            "model_chips_tag": self.vars["model_chips_tag"].get(),
            "model_chips_count": self.vars["model_chips_count"].get(),
            "sequence_name": self.vars["sequence_name"].get(),
            "sequence_version": self.vars["sequence_version"].get(),
            "developer_notes": self.vars["developer_notes"].get(),
        }
        try:
            size = write_json(path, meta, p["frames"], p["stride"], p["fps"])
        except OSError as e:
            self._err("Could not write the file: %s (recording kept in memory)" % e)
            self.last_saved = "Not saved: the recording is kept in memory."
            return
        extra = ""
        if p["skip"] or p["blend"]:
            extra = "\n(recorded %d: skipped %d, blended %d)" % (
                p["recorded"],
                p["skip"],
                p["blend"],
            )
        self.last_saved = "Saved: %s\n%d frames, %.0f KB%s" % (
            path,
            len(p["frames"]),
            size / 1024.0,
            extra,
        )
        self.saved_path = path
        self.pending = None
        self._approved = None

    def _update_buttons(self):
        running = self.server.running
        addr = "(%s) UDP: %s" % (self.host_var.get(), self.port_var.get())
        self.btn_server.config(
            text=("Stop Server\n" if running else "Start Server\n") + addr,
            bg=RED if running else BTN,
            activebackground=RED if running else BTN_HOVER,
            highlightbackground=RED_BORDER if running else BORDER,
        )
        rec = self.rec.state in ("waiting", "recording")
        self.btn_rec.config(
            text="Stop Record\nInput Data" if rec else "Start Record\nInput Data",
            bg=RED if rec else BTN,
            activebackground=RED if rec else BTN_HOVER,
            highlightbackground=RED_BORDER if rec else BORDER,
            state="normal" if (running or rec) else "disabled",
        )

    def _close(self):
        if self.rec.state in ("waiting", "recording"):
            self.rec.stop()
        if self._was_recording and self.rec.state == "done":
            self._finish_recording()
        if self.pending is not None:
            choice = ask_choice(
                self,
                "Unsaved recording",
                "The last recording (%d frames) has not been saved."
                % len(self.pending["frames"]),
                [
                    ("Save it now", "save"),
                    ("Close without saving", "close"),
                    ("Cancel", None),
                ],
            )
            if choice == "save":
                self._save_pending()
                if self.pending is not None:
                    return
            elif choice != "close":
                return
        try:
            self.server.stop()
        finally:
            self.destroy()

    # ---- statistics ----------------------------------------------------------
    def _refresh(self):
        if self.server.error:
            self._err(self.server.error)
            self.server.error = None
        if self.rec.error:
            self._err(self.rec.error)
            self.rec.error = None
        if not self.server.running and self.rec.state in ("waiting", "recording"):
            self.rec.stop()  # the source is gone: finish what has been recorded
        if self._was_recording and self.rec.state == "done":
            self._finish_recording()
        if self.rec.state not in ("waiting", "recording"):  # derived fields while idle
            try:
                chips = int(self.vars["model_chips_count"].get())
                cpc = int(self.map_vars["channels_per_chip"].get())
                self._set_ro("sequence_frame_stride", str(chips * cpc))
                dur = float(
                    self.vars["sequence_duration_sec"].get().replace(",", ".") or 0
                )
                fps = float(self.vars["sequence_fps"].get().replace(",", "."))
                if self.rec.state == "idle":
                    self._set_ro(
                        "sequence_frame_count",
                        str(int(round(dur * fps))) if dur > 0 else "",
                    )
            except ValueError:
                pass
        latest, ustats, total, ignored, nbytes, last = self.server.snapshot()
        now = time.perf_counter()
        pt, pn, pu = self._prev
        if now - pt >= 0.9:
            dt = now - pt
            self._rates = (
                (total - pn) / dt,
                {u: (s["count"] - pu.get(u, 0)) / dt for u, s in ustats.items()},
            )
            self._prev = (now, total, {u: s["count"] for u, s in ustats.items()})
        rate, urates = self._rates
        L = []
        if self.server.running:
            L.append(
                ("Server: RUNNING  %s:%d" % (self.server.host, self.server.port), None)
            )
            ago = "-" if not last else "%.2f s ago" % (now - last)
            L.append(("Packets: %d  (%.1f pkt/s)  last: %s" % (total, rate, ago), None))
            if ignored:
                L.append(("Ignored (not ArtDMX): %d" % ignored, None))
            L.append(("Universes received:", None))
            if ustats:
                for u in sorted(ustats):
                    L.append(
                        (
                            "  U%-4d %6.1f pkt/s   %3d ch"
                            % (u, urates.get(u, 0.0), ustats[u]["length"]),
                            None,
                        )
                    )
            else:
                L.append(("  (nothing received yet)", None))
        else:
            L.append(("Server: STOPPED", None))
        L.append(("", None))
        st = self.rec.state
        if st == "waiting":
            L.append(("Recording: waiting for the first packet...", None))
        elif st == "recording":
            tgt = ("/%d" % self.rec.max_frames) if self.rec.max_frames else ""
            L.append(
                (
                    "REC  %d%s frames  (%.2f s)"
                    % (len(self.rec.frames), tgt, self.rec.elapsed),
                    None,
                )
            )
            if self._job and (self._job["skip"] or self._job["blend"]):
                L.append(
                    (
                        "plan: skip %d + export %s + blend %d"
                        % (
                            self._job["skip"],
                            self._job["final"] or "?",
                            self._job["blend"],
                        ),
                        None,
                    )
                )
            L.append(
                ("missing: %d   late: %d" % (self.rec.missing, self.rec.late), None)
            )
        elif st == "done":
            L.append(
                (
                    "Recorded: %d frames  missing: %d  late: %d"
                    % (len(self.rec.frames), self.rec.missing, self.rec.late),
                    None,
                )
            )
            if self.rec.missing:
                L.append(
                    (
                        "Warning: some frames had no data for a universe from Input mapping.",
                        "warn",
                    )
                )
        if self.last_saved:
            L.append(("", None))
            L.extend((line, None) for line in self.last_saved.split("\n"))
        if self.errors:
            L.append(("", None))
            L.append(("Errors:", "err"))
            L.extend(("[%s] %s" % (t, m), "err") for t, m in self.errors)
        self.stats.config(state="normal")
        self.stats.delete("1.0", "end")
        for text, tag in L:
            self.stats.insert("end", text + "\n", tag or ())
        self.stats.config(state="disabled")
        self._update_buttons()
        self.after(250, self._refresh)

    def _set_ro(self, key, value):
        if self.vars[key].get() != value:
            self.vars[key].set(value)


def main():
    if tk is None:
        raise SystemExit(
            "tkinter is not installed (on Linux: sudo apt install python3-tk)"
        )
    App().mainloop()


if __name__ == "__main__":
    main()
