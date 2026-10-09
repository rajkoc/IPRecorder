#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IP Recorder - snima proizvoljan broj IP video strimova (RTSP/HTTP/RTMP/UDP...) u WMV ili H.264 MP4.

Program ima dva dela u istom fajlu:
  * SERVIS  (python ip_recorder.py --service)  - pozadinski proces bez prozora koji snima
                                                  i izvrsava raspored. Radi i kad zatvorite prozor.
  * PROZOR  (python ip_recorder.py)            - podesavanja i upravljanje servisom
                                                  (pokretanje/zaustavljanje servisa i snimanja).

Potrebno:
  - Python 3.8+ (tkinter dolazi uz standardnu instalaciju)
  - ffmpeg (verzija 5 ili novija preporucena): https://ffmpeg.org/download.html
    Ili ga stavite u isti folder kao ovaj program, ili upisite punu putanju u polje "ffmpeg".
"""

from array import array
import ctypes
import datetime
import json
import math
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time


def _fix_tcl_env():
    """Automatski pronalazi Tcl/Tk biblioteke u Python instalaciji (bilo gde da je instalirana)
    i uklanja pogresne TCL_LIBRARY / TK_LIBRARY vrednosti ako postoje."""
    import glob
    for var, marker in (("TCL_LIBRARY", "init.tcl"), ("TK_LIBRARY", "tk.tcl")):
        cur = os.environ.get(var)
        if cur and not os.path.isfile(os.path.join(cur, marker)):
            del os.environ[var]  # pokazuje na nepostojeci folder
    roots = {sys.base_prefix, sys.prefix, os.path.dirname(sys.executable)}
    for var, marker, pattern in (("TCL_LIBRARY", "init.tcl", "tcl8*"),
                                 ("TK_LIBRARY", "tk.tcl", "tk8*")):
        if var in os.environ:
            continue
        for root in roots:
            found = False
            for base in (os.path.join(root, "tcl"), os.path.join(root, "lib"),
                         os.path.join(root, "Lib")):
                for d in sorted(glob.glob(os.path.join(base, pattern)), reverse=True):
                    if os.path.isfile(os.path.join(d, marker)):
                        os.environ[var] = d
                        found = True
                        break
                if found:
                    break
            if found:
                break


FROZEN = getattr(sys, "frozen", False)  # True kad radi kao exe (PyInstaller)

try:
    if not FROZEN:
        _fix_tcl_env()
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # servis (--service) radi i bez tkinter-a
    tk = None

DEFAULT_STREAMS = 5   # broj strimova u novoj instalaciji (dalje se dodaje/brise u prozoru)
MAX_STREAMS = 32
VISIBLE_ROWS = 5      # toliko redova je vidljivo, dalje se skroluje
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])),
                           "ip_recorder_settings.json")

APP_VERSION = 14  # povecava se kad se promeni ponasanje servisa; prozor poredi sa servisom

CODEC_WMV = "WMV"
CODEC_MP4 = "H.264 MP4"
CODEC_COPY = "H.264 MP4 (bez rekodiranja)"
ALL_CODECS = ["WMV", "H.264 MP4", "H.264 MP4 (bez rekodiranja)"]
X264_PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium"]
MP4_FRAG = "+frag_keyframe+empty_moov+default_base_moof"

# Stanja
IDLE, CONNECTING, RECORDING, RECONNECTING, ERROR = (
    "IDLE", "CONNECTING", "RECORDING", "RECONNECTING", "ERROR")

STATE_TEXT = {
    IDLE: "Zaustavljeno",
    CONNECTING: "Povezivanje...",
    RECORDING: "SNIMANJE",
    RECONNECTING: "Ponovno povezivanje...",
    ERROR: "Greska",
}
STATE_COLOR = {
    IDLE: "#9a9a9a",
    CONNECTING: "#e6b800",
    RECORDING: "#e00000",
    RECONNECTING: "#ff8c00",
    ERROR: "#600000",
}

DEFAULTS = {
    "ffmpeg": "ffmpeg",
    "folder": os.path.join(os.path.expanduser("~"), "Snimci"),
    "codec": CODEC_WMV,
    "x264_preset": "veryfast",
    "mp4_fragmented": True,
    "keep_days": "0",
    "video_kbps": "1500",
    "audio_kbps": "128",
    "resolution": "Original",
    "fps": "0",
    "segment_min": "120",
    "align_clock": True,
    "template": "%Y-%m-%d_%H-%M-%S",
    "subfolders": True,
    "transport": "tcp",
    "reconnect": True,
    "reconnect_delay": "5",
    "sched_enabled": False,
    "sched_start": "22:00",
    "sched_stop": "06:00",
    "sched_days": [True] * 7,
    "cpu_cap": "0",
    "priority": "Ispod normalnog",
    "threads": "0",
    "streams": [
        {"id": "s%d" % (i + 1), "enabled": True, "name": "Kamera%d" % (i + 1), "url": ""}
        for i in range(DEFAULT_STREAMS)
    ],
}


# ----------------------------------------------------------------------------
# Prioritet i ogranicenje CPU-a
# ----------------------------------------------------------------------------
PRIORITIES = {"Normalan": "normal", "Ispod normalnog": "below", "Nizak (idle)": "low"}

DAY_NAMES = ["Pon", "Uto", "Sre", "Cet", "Pet", "Sub", "Ned"]  # weekday(): 0 = ponedeljak


def parse_hhmm(text):
    """'22:30' -> minuti od ponoci (1350) ili None ako format nije ispravan."""
    m = re.match(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$", text or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def in_schedule_window(now, start_min, stop_min, days):
    """Da li je 'now' u prozoru snimanja. 'days' = dani u kojima prozor POCINJE.
    Ako je kraj manji od pocetka, prozor prelazi preko ponoci (npr. 22:00 - 06:00)."""
    nowm = now.hour * 60 + now.minute
    today = now.weekday()
    yesterday = (today - 1) % 7
    if start_min < stop_min:
        return days[today] and start_min <= nowm < stop_min
    return (days[today] and nowm >= start_min) or (days[yesterday] and nowm < stop_min)


def next_schedule_start(now, start_min, days):
    for d in range(0, 8):
        day = now.date() + datetime.timedelta(days=d)
        cand = datetime.datetime.combine(day, datetime.time(start_min // 60, start_min % 60))
        if cand > now and days[cand.weekday()]:
            return cand
    return None


def cpu_cap_supported():
    """Tvrdi CPU limit (Job Object CPU rate control) postoji od Windows 8 (6.2)."""
    if os.name != "nt":
        return False
    try:
        v = sys.getwindowsversion()
        return (v.major, v.minor) >= (6, 2)
    except Exception:
        return False


_FFMPEG_MAJOR = {}


def ffmpeg_major(path):
    """Vraca glavnu verziju ffmpeg-a (npr. 4, 5, 6) ili None ako se ne moze utvrditi."""
    if path in _FFMPEG_MAJOR:
        return _FFMPEG_MAJOR[path]
    major = None
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        out = subprocess.run([path, "-version"], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, timeout=15,
                             creationflags=flags).stdout.decode(errors="replace")
        m = re.search(r"ffmpeg version n?(\d+)\.", out)
        if m:
            major = int(m.group(1))
    except Exception:
        pass
    _FFMPEG_MAJOR[path] = major
    return major


_ENCODERS = {}


def ffmpeg_has_encoder(path, name):
    """True/False, ili None ako se ne moze utvrditi."""
    key = (path, name)
    if key in _ENCODERS:
        return _ENCODERS[key]
    res = None
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        out = subprocess.run([path, "-hide_banner", "-encoders"], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, timeout=20,
                             creationflags=flags).stdout.decode(errors="replace")
        if "Encoders:" in out:
            res = re.search(r"^\s*[VAS][\w.]+\s+%s\s" % re.escape(name), out, re.M) is not None
    except Exception:
        pass
    _ENCODERS[key] = res
    return res


class CpuJob:
    """Windows Job Object sa tvrdim ogranicenjem CPU-a za SVE ffmpeg procese zajedno.
    Procenat se odnosi na ukupni CPU racunara (svih jezgara)."""

    JobObjectCpuRateControlInformation = 15
    ENABLE = 0x1
    HARD_CAP = 0x4

    class _Info(ctypes.Structure):
        _fields_ = [("ControlFlags", ctypes.c_uint32), ("CpuRate", ctypes.c_uint32)]

    def __init__(self):
        self.job = None
        self.k32 = None
        if not cpu_cap_supported():
            return
        try:
            from ctypes import wintypes
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.CreateJobObjectW.restype = wintypes.HANDLE
            k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            k.SetInformationJobObject.restype = wintypes.BOOL
            k.SetInformationJobObject.argtypes = [
                wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            k.AssignProcessToJobObject.restype = wintypes.BOOL
            k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            self.k32 = k
            self.job = k.CreateJobObjectW(None, None)
        except Exception:
            self.job = None

    def set_cap(self, percent):
        """percent 0 = bez ogranicenja. Moze se menjati i dok procesi rade."""
        if not self.job:
            return False
        info = self._Info()
        if percent > 0:
            info.ControlFlags = self.ENABLE | self.HARD_CAP
            info.CpuRate = int(min(100, max(1, percent)) * 100)
        else:
            info.ControlFlags = 0
            info.CpuRate = 0
        return bool(self.k32.SetInformationJobObject(
            self.job, self.JobObjectCpuRateControlInformation,
            ctypes.byref(info), ctypes.sizeof(info)))

    def assign(self, proc):
        if not self.job:
            return False
        try:
            return bool(self.k32.AssignProcessToJobObject(self.job, int(proc._handle)))
        except Exception:
            return False


CPU_JOB = CpuJob()


# ----------------------------------------------------------------------------
# Worker za jedan strim
# ----------------------------------------------------------------------------
def probe_codecs(cfg, url):
    """Kratko otvara izvor i vraca (video_kodek, audio_kodek) malim slovima, ili (None, None)."""
    cmd = [cfg["ffmpeg"], "-hide_banner"] + input_args(cfg, url) + ["-i", url]
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        out = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, timeout=25,
                             creationflags=flags).stdout.decode(errors="replace")
    except Exception:
        return None, None
    v = re.search(r"Video: (\w+)", out)
    a = re.search(r"Audio: (\w+)", out)
    return (v.group(1).lower() if v else None, a.group(1).lower() if a else None)


class StreamWorker:
    def __init__(self, idx, ui_queue):
        self.idx = idx
        self.ui_q = ui_queue
        self.stop_evt = threading.Event()
        self.proc = None
        self.thread = None

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, cfg, name, url):
        if self.running:
            return
        self.stop_evt.clear()
        self.thread = threading.Thread(
            target=self._run, args=(cfg, name, url), daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_evt.set()
        p = self.proc
        if p and p.poll() is None:
            try:
                p.stdin.write(b"q")  # ffmpeg lepo zatvara fajl
                p.stdin.flush()
            except Exception:
                pass
            threading.Timer(8.0, self._kill).start()

    def _kill(self):
        p = self.proc
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass

    def _emit(self, state, info=""):
        self.ui_q.put((self.idx, state, info))

    # -- gradnja ffmpeg komande --
    @staticmethod
    def build_cmd(cfg, name, url, vcodec=None, acodec=None):
        folder = cfg["folder"]
        if cfg["subfolders"]:
            folder = os.path.join(folder, name)
        os.makedirs(folder, exist_ok=True)

        cmd = [cfg["ffmpeg"], "-hide_banner", "-y",
               "-loglevel", "warning", "-stats"]

        threads = int(cfg["threads"])
        if threads > 0:
            cmd += ["-threads", str(threads)]  # niti za dekodiranje ulaza

        if url.lower().startswith("rtsp"):
            major = ffmpeg_major(cfg["ffmpeg"])
            # ffmpeg 4.x i stariji: -stimeout, ffmpeg 5+: -timeout
            tmo = "-stimeout" if (major is not None and major < 5) else "-timeout"
            cmd += ["-rtsp_transport", cfg["transport"], tmo, "10000000"]
        else:
            cmd += ["-rw_timeout", "10000000"]
        cmd += ["-i", url, "-map", "0:v:0", "-map", "0:a:0?"]

        if threads > 0:
            cmd += ["-threads", str(threads)]  # niti za enkodiranje izlaza

        copy = cfg.get("codec") == CODEC_COPY
        mp4 = cfg.get("codec") in (CODEC_MP4, CODEC_COPY)
        frag = bool(cfg.get("mp4_fragmented", True))

        # Video
        vb = int(cfg["video_kbps"])
        if copy:
            cmd += ["-c:v", "copy"]  # bez rekodiranja: nema bitrate/rezolucije/FPS podesavanja
            if vcodec == "hevc":
                cmd += ["-tag:v", "hvc1"]
        elif mp4:
            preset = cfg.get("x264_preset") or "veryfast"
            cmd += ["-c:v", "libx264", "-preset", preset, "-b:v", f"{vb}k",
                    "-maxrate", f"{int(vb * 1.5)}k", "-bufsize", f"{vb * 2}k",
                    "-pix_fmt", "yuv420p",
                    "-force_key_frames", "expr:gte(t,n_forced*2)"]  # kljucni kadar na 2 s -> precizan rez
        else:
            cmd += ["-c:v", "wmv2", "-b:v", f"{vb}k",
                    "-maxrate", f"{int(vb * 1.5)}k", "-bufsize", f"{vb * 2}k"]
        res = "original" if copy else cfg["resolution"].strip().lower()
        if res and res != "original":
            m = re.match(r"^(\d+)\s*x\s*(\d+)$", res)
            if not m:
                raise ValueError("Rezolucija mora biti oblika 1280x720 ili 'Original'")
            cmd += ["-vf", f"scale={m.group(1)}:{m.group(2)}"]
        fps = int(cfg["fps"])
        if fps > 0 and not copy:
            cmd += ["-r", str(fps)]

        # Audio
        ab = int(cfg["audio_kbps"])
        if ab > 0:
            # i u "copy" rezimu zvuk ide u AAC (vrlo lagano): AAC iz MPEG-TS/UDP izvora je u ADTS
            # formatu koji MP4 ne prihvata, a G.711 iz kamera MP4 uopste ne podrzava
            cmd += ["-c:a", "aac" if mp4 else "wmav2", "-b:a", f"{ab}k", "-ar", "44100", "-ac", "2"]
        else:
            cmd += ["-an"]

        # Fajl / segmentiranje
        seg_secs = int(float(cfg["segment_min"]) * 60)
        base = f"{name}_{cfg['template']}" + (".mp4" if mp4 else ".wmv")
        if seg_secs > 0:
            if fps > 0 and not mp4:
                cmd += ["-g", str(fps)]  # kljucni kadar svake sekunde -> precizan rez
            cmd += ["-f", "segment", "-segment_time", str(seg_secs)]
            if cfg["align_clock"]:
                # DVR nacin: rez na okrugle sate racunara (00:00, 02:00, 04:00 ...)
                cmd += ["-segment_atclocktime", "1"]
            cmd += ["-segment_format", "mp4" if mp4 else "asf"]
            if mp4 and frag:
                # fragmentirani MP4: fajl ostaje ispravan i ako se snimanje naglo prekine
                cmd += ["-segment_format_options", "movflags=" + MP4_FRAG]
            cmd += ["-reset_timestamps", "1", "-strftime", "1", os.path.join(folder, base)]
        else:
            out = os.path.join(folder, time.strftime(base))
            if mp4:
                cmd += ["-f", "mp4"] + (["-movflags", MP4_FRAG] if frag else []) + [out]
            else:
                cmd += ["-f", "asf", out]
        return cmd

    # -- glavna petlja --
    def _run(self, cfg, name, url):
        delay = max(1, int(cfg["reconnect_delay"]))
        vcodec = acodec = None
        if cfg.get("codec") == CODEC_COPY:
            self._emit(CONNECTING, "Provera formata izvora...")
            vcodec, acodec = probe_codecs(cfg, url)
            if self.stop_evt.is_set():
                self._emit(IDLE)
                return
            if vcodec and vcodec not in ("h264", "hevc"):
                self._emit(ERROR, "Izvor nije H.264 (otkriveno: %s). Izaberite WMV ili H.264 MP4 "
                                  "sa rekodiranjem." % vcodec)
                return
        while not self.stop_evt.is_set():
            self._emit(CONNECTING)
            try:
                cmd = self.build_cmd(cfg, name, url, vcodec, acodec)
            except Exception as e:
                self._emit(ERROR, str(e))
                break

            prio = PRIORITIES.get(cfg["priority"], "normal")
            flags = 0
            kw = {}
            if os.name == "nt":
                flags = subprocess.CREATE_NO_WINDOW
                if prio == "below":
                    flags |= 0x00004000  # BELOW_NORMAL_PRIORITY_CLASS
                elif prio == "low":
                    flags |= 0x00000040  # IDLE_PRIORITY_CLASS
            else:
                nice = {"below": 5, "low": 15}.get(prio, 0)
                if nice:
                    kw["preexec_fn"] = lambda n=nice: os.nice(n)
            try:
                self.proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, bufsize=0, creationflags=flags, **kw)
                if os.name == "nt":
                    CPU_JOB.assign(self.proc)
            except FileNotFoundError:
                self._emit(ERROR, "ffmpeg nije pronadjen")
                break
            except Exception as e:
                self._emit(ERROR, str(e))
                break

            recording = False
            last_msg = ""
            recent = []
            buf = b""
            while True:
                chunk = self.proc.stderr.read(1024)
                if not chunk:
                    break
                buf += chunk
                parts = re.split(rb"[\r\n]+", buf)
                buf = parts.pop()
                for line in parts:
                    if not line.strip():
                        continue
                    if b"frame=" in line or b"size=" in line:
                        tm = re.search(rb"time=(\S+)", line)
                        sz = re.search(rb"size=\s*(\d+)", line)
                        info = ""
                        if tm:
                            info += "vreme " + tm.group(1).decode(errors="replace")
                        if sz:
                            info += f"  |  {int(sz.group(1)) // 1024} MB" \
                                if b"kB" not in line else f"  |  {int(sz.group(1)) / 1024:.1f} MB"
                        if not recording:
                            recording = True
                        self._emit(RECORDING, info)
                    else:
                        text = line.decode(errors="replace").strip()
                        last_msg = text[:160]
                        recent.append(text)
                        del recent[:-15]

            code = self.proc.wait()
            self.proc = None

            # Izvor bez zvuka + zadat audio bitrate: stariji ffmpeg odbija da pokrene snimanje
            # ("Codec AVOption ... has not been used for any stream"). Tada snimaj samo sliku.
            if (not recording and not self.stop_evt.is_set() and int(cfg["audio_kbps"]) > 0
                    and any("has not been used for any stream" in t for t in recent)):
                cfg = dict(cfg, audio_kbps="0")
                self._emit(CONNECTING, "Izvor nema zvuk - snimam samo sliku")
                continue

            if self.stop_evt.is_set():
                break
            if not cfg["reconnect"]:
                self._emit(ERROR, last_msg or f"ffmpeg izasao (kod {code})")
                break

            self._emit(RECONNECTING, last_msg or f"Veza prekinuta (kod {code})")
            for _ in range(delay * 10):
                if self.stop_evt.is_set():
                    break
                time.sleep(0.1)

        self._emit(IDLE if self.stop_evt.is_set() else ERROR,
                   "" if self.stop_evt.is_set() else "")


# ----------------------------------------------------------------------------
# Konfiguracija, provera i komunikacija sa servisom
# ----------------------------------------------------------------------------
SERVICE_PORT = 47653  # samo localhost
SCRIPT_PATH = os.path.abspath(sys.argv[0])
BASE_DIR = os.path.dirname(SCRIPT_PATH)
SERVICE_LOG = os.path.join(BASE_DIR, "ip_recorder_service.log")


def next_stream_id(ids):
    """Sledeci slobodan ID oblika s<broj>."""
    nums = [int(i[1:]) for i in ids if re.match(r"^s\d+$", i)]
    return "s%d" % ((max(nums) if nums else 0) + 1)


def normalize_streams(streams):
    """Svaki strim ima stabilan ID (cuva se u podesavanjima), pa brisanje/dodavanje
    redova ne mesa snimanja. Stariji fajlovi bez ID-jeva dobijaju s1, s2, ... po redosledu."""
    out, used = [], set()
    for i, st in enumerate(streams[:MAX_STREAMS]):
        if not isinstance(st, dict):
            continue
        sid = str(st.get("id") or "")
        if not sid or sid in used:
            sid = ""
        else:
            used.add(sid)
        out.append({"id": sid, "enabled": bool(st.get("enabled", True)),
                    "name": str(st.get("name") or "").strip() or "Kamera%d" % (len(out) + 1),
                    "url": str(st.get("url") or "")})
    for st in out:
        if not st["id"]:
            st["id"] = next_stream_id(used)
            used.add(st["id"])
    return out or [{"id": "s1", "enabled": True, "name": "Kamera1", "url": ""}]


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        for k, v in saved.items():
            if k == "streams":
                if isinstance(v, list):
                    cfg["streams"] = normalize_streams(v)
            else:
                cfg[k] = v
    except Exception:
        pass
    return cfg


def save_config(cfg):
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_FILE)  # servis nikad ne cita napola upisan fajl


def config_error(cfg):
    """Vraca tekst greske ili None ako je konfiguracija ispravna."""
    try:
        assert int(cfg["video_kbps"]) > 0
        assert int(cfg["audio_kbps"]) >= 0
        assert int(cfg["fps"]) >= 0
        assert float(cfg["segment_min"]) >= 0
        assert int(cfg["reconnect_delay"]) >= 0
        assert 0 <= int(cfg["cpu_cap"]) <= 100
        assert int(cfg["threads"]) >= 0
        assert int(cfg.get("keep_days", 0)) >= 0
    except Exception:
        return "Proverite numericka polja (bitrate, FPS, minuti, sekunde)."
    if not cfg["folder"]:
        return "Unesite folder za snimke."
    if not cfg["template"]:
        return "Sablon imena ne sme biti prazan."
    if shutil.which(cfg["ffmpeg"]) is None and not os.path.isfile(cfg["ffmpeg"]):
        return "ffmpeg nije pronadjen. Instalirajte ga ili upisite punu putanju do ffmpeg.exe."
    names = [safe_stream_name(st["name"].strip(), i).lower() for i, st in enumerate(cfg["streams"])]
    dups = sorted({n for n in names if names.count(n) > 1})
    if dups:
        return "Nazivi strimova moraju biti razliciti (ponavlja se: %s)." % ", ".join(dups)
    if cfg.get("codec") == CODEC_MP4 and ffmpeg_has_encoder(cfg["ffmpeg"], "libx264") is False:
        return ("Ovaj ffmpeg nema H.264 enkoder (libx264). Preuzmite ffmpeg 'full' ili 'GPL' build "
                "(npr. sa gyan.dev ili BtbN) ili izaberite WMV.")
    if cfg["sched_enabled"]:
        a, b = parse_hhmm(cfg["sched_start"]), parse_hhmm(cfg["sched_stop"])
        if a is None or b is None:
            return "Vreme rasporeda mora biti u obliku HH:MM (npr. 22:00)."
        if a == b:
            return "Pocetak i kraj rasporeda ne smeju biti isti."
        if not any(cfg["sched_days"]):
            return "Izaberite bar jedan dan u rasporedu."
    return None


def ipc_request(cmd, timeout=3.0):
    with socket.create_connection(("127.0.0.1", SERVICE_PORT), timeout=timeout) as sk:
        sk.settimeout(timeout)
        sk.sendall((json.dumps(cmd) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sk.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.decode("utf-8"))


def pythonw_path():
    exe = sys.executable
    if FROZEN:
        return exe
    if os.name == "nt":
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(pyw):
            return pyw
    return exe


def service_command():
    """Komanda kojom se pokrece servis: exe --service, ili pythonw skripta --service."""
    if FROZEN:
        return [sys.executable, "--service"]
    return [pythonw_path(), SCRIPT_PATH, "--service"]


def launch_service():
    """Pokrece servis kao odvojen pozadinski proces (bez prozora, ne zavisi od GUI-ja)."""
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(service_command(), cwd=BASE_DIR,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, **kw)


def wait_for_service(seconds=10):
    end = time.time() + seconds
    while time.time() < end:
        try:
            if ipc_request({"cmd": "status"}, timeout=1.0).get("ok"):
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def startup_script_path():
    return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                        "Start Menu", "Programs", "Startup", "IPRecorderService.vbs")


def autostart_enabled():
    return os.name == "nt" and os.path.isfile(startup_script_path())


def set_autostart(on):
    """Uključuje/isključuje automatski start servisa pri prijavi na Windows (Startup folder)."""
    path = startup_script_path()
    if not on:
        if os.path.isfile(path):
            os.remove(path)
        return
    cmd = " ".join('"%s"' % part for part in service_command())
    vbs = ('Set sh = CreateObject("WScript.Shell")\r\n'
           'sh.CurrentDirectory = "%s"\r\n'
           'sh.Run "%s", 0, False\r\n') % (BASE_DIR.replace('"', '""'), cmd.replace('"', '""'))
    with open(path, "w", encoding="utf-16", newline="") as f:
        f.write(vbs)


def safe_stream_name(name, i):
    return re.sub(r'[\\/:*?"<>|]', "_", name) or "Kamera%d" % (i + 1)


def cleanup_old_recordings(cfg, now=None):
    """Brise snimke (.wmv/.mp4) starije od 'keep_days' dana. Vraca (broj_fajlova, bajtova).

    Bezbednosna pravila: brise samo u folderu za snimke i njegovim direktnim podfolderima,
    samo fajlove cije ime pocinje nazivom neke kamere + '_' (kako ih program imenuje),
    i samo ako je poslednja izmena fajla starija od zadatog broja dana."""
    days = int(cfg.get("keep_days") or 0)
    root = cfg.get("folder") or ""
    if days <= 0 or not os.path.isdir(root):
        return 0, 0
    cutoff = (now if now is not None else time.time()) - days * 86400
    prefixes = tuple(safe_stream_name(st["name"].strip(), i) + "_"
                     for i, st in enumerate(cfg["streams"]) if st["name"].strip())
    if not prefixes:
        return 0, 0
    dirs = [root]
    try:
        dirs += [e.path for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
    except OSError:
        pass
    count = freed = 0
    for d in dirs:
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            try:
                if (e.is_file(follow_symlinks=False) and e.name.lower().endswith((".wmv", ".mp4"))
                        and e.name.startswith(prefixes)):
                    st = e.stat()
                    if st.st_mtime < cutoff:
                        os.remove(e.path)
                        count += 1
                        freed += st.st_size
            except OSError:
                continue  # fajl je zauzet ili nestao - preskoci
    return count, freed


# ----------------------------------------------------------------------------
# SERVIS: snimanje + raspored + komandni server (bez ikakvog prozora)
# ----------------------------------------------------------------------------
class Engine:
    def __init__(self):
        self.q = queue.Queue()
        self.workers = {}  # id strima -> StreamWorker
        self.states = {}
        self.infos = {}
        self.cfg = load_config()
        self.lock = threading.RLock()
        self.shutdown_evt = threading.Event()
        self.in_window = None
        self.sched_msg = ""
        self.notice = ""
        self.cleanup_msg = ""
        self.last_cleanup = time.time() - 3600 + 30  # prvo brisanje 30 s posle starta, pa na svaki sat
        self._cleanup_busy = False

    def log(self, msg):
        try:
            with open(SERVICE_LOG, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
        except Exception:
            pass

    # -- stanje --
    def stream_name(self, sid):
        for st in self.cfg["streams"]:
            if st["id"] == sid:
                return st["name"]
        return sid

    def _worker(self, sid):
        w = self.workers.get(sid)
        if w is None:
            w = self.workers[sid] = StreamWorker(sid, self.q)
            self.states.setdefault(sid, IDLE)
            self.infos.setdefault(sid, "")
        return w

    def _gc_workers(self):
        """Strimovi obrisani iz podesavanja: zaustavi snimanje i ukloni radnika."""
        ids = {st["id"] for st in self.cfg["streams"]}
        for sid in list(self.workers):
            if sid in ids:
                continue
            w = self.workers[sid]
            if w.running:
                w.stop()
            else:
                del self.workers[sid]
                self.states.pop(sid, None)
                self.infos.pop(sid, None)

    def drain(self):
        try:
            while True:
                sid, state, info = self.q.get_nowait()
                if sid not in self.workers:
                    continue
                if state == ERROR and self.states.get(sid) == ERROR and not info:
                    continue
                if state != self.states.get(sid):
                    self.log("%s: %s %s" % (self.stream_name(sid), state, info))
                self.states[sid] = state
                self.infos[sid] = info
        except queue.Empty:
            pass

    def status(self):
        streams = {sid: [self.states.get(sid, IDLE), self.infos.get(sid, "")] for sid in self.workers}
        return {"ok": True, "pid": os.getpid(), "version": APP_VERSION, "streams": streams,
                "sched": self.sched_msg, "notice": self.notice, "cleanup": self.cleanup_msg}

    def cleanup_async(self):
        """Brisanje starih snimaka u pozadinskoj niti (da ne blokira snimanje)."""
        if self._cleanup_busy:
            return
        cfg = json.loads(json.dumps(self.cfg))
        try:
            days = int(cfg.get("keep_days") or 0)
        except ValueError:
            days = 0
        if days <= 0:
            self.cleanup_msg = ""
            return
        self._cleanup_busy = True

        def work():
            try:
                n, b = cleanup_old_recordings(cfg)
                self.cleanup_msg = "Brisanje starih snimaka (> %d dana): %s - obrisano %d fajl(ova), %.2f GB." % (
                    days, time.strftime("%d.%m. %H:%M"), n, b / 1024.0 ** 3)
                if n:
                    self.log("Obrisano %d starih snimaka (%.2f GB), starijih od %d dana" % (n, b / 1024.0 ** 3, days))
            except Exception as e:
                self.cleanup_msg = "Brisanje starih snimaka nije uspelo: %s" % e
                self.log(self.cleanup_msg)
            finally:
                self._cleanup_busy = False
        threading.Thread(target=work, daemon=True).start()

    # -- start / stop --
    def start_stream(self, sid):
        pos, s = None, None
        for i, st in enumerate(self.cfg["streams"]):
            if st["id"] == sid:
                pos, s = i, st
        if s is None:
            return "Strim nije pronadjen u podesavanjima (sacuvajte podesavanja)."
        name = safe_stream_name(s["name"], pos)
        if not s["url"].strip():
            return "%s: URL nije unet" % name
        CPU_JOB.set_cap(int(self.cfg["cpu_cap"]))
        self._worker(sid).start(self.cfg, name, s["url"].strip())
        return None

    def start_all(self):
        """Pokrece sve strimove ciji je kvacica ukljucena. Vraca tekst greske ili None."""
        err = config_error(self.cfg)
        if err:
            return err
        errs = []
        for st in self.cfg["streams"]:
            if not st["enabled"] or not st["url"].strip():
                continue  # iskljucen ili bez URL-a: preskoci bez greske
            w = self.workers.get(st["id"])
            if w is not None and w.running:
                continue
            e = self.start_stream(st["id"])
            if e:
                errs.append(e)
        return "; ".join(errs) or None

    def stop_all(self):
        for w in list(self.workers.values()):
            if w.running:
                w.stop()

    # -- raspored --
    def sched_tick(self):
        c = self.cfg
        if not c["sched_enabled"]:
            self.in_window = None
            self.sched_msg = ""
            return
        a, b = parse_hhmm(c["sched_start"]), parse_hhmm(c["sched_stop"])
        days = list(c["sched_days"])
        if a is None or b is None or a == b or not any(days):
            self.in_window = None
            self.sched_msg = "Raspored: proverite vreme (HH:MM, pocetak i kraj ne smeju biti isti) i dane."
            return
        now = datetime.datetime.now()
        inw = in_schedule_window(now, a, b, days)
        if inw != self.in_window:
            prev = self.in_window
            self.in_window = inw
            if inw:
                self.cfg = load_config()  # uzmi najnovija podesavanja
                self.log("Raspored: pocetak snimanja")
                err = self.start_all()
                self.notice = ("Raspored nije mogao da pokrene snimanje: " + err) if err else ""
            elif prev is not None:
                self.log("Raspored: kraj snimanja")
                self.stop_all()
        if inw:
            self.sched_msg = "Raspored aktivan: snimanje se iskljucuje u %s." % c["sched_stop"].strip()
        else:
            nxt = next_schedule_start(now, a, days)
            self.sched_msg = ("Raspored aktivan: sledece ukljucivanje %s %s." % (
                DAY_NAMES[nxt.weekday()], nxt.strftime("%d.%m. u %H:%M"))) if nxt else "Raspored aktivan."

    # -- komande iz prozora --
    def handle(self, req):
        cmd = req.get("cmd")
        with self.lock:
            self.drain()
            if cmd == "status":
                return self.status()
            if cmd == "apply":
                self.cfg = load_config()
                self.in_window = None  # ako smo u prozoru rasporeda, snimanje krece odmah
                self.notice = ""
                self.last_cleanup = 0.0  # primeni nov broj dana odmah
                CPU_JOB.set_cap(int(self.cfg["cpu_cap"]) if str(self.cfg["cpu_cap"]).isdigit() else 0)
                self.log("Podesavanja primenjena")
                return {"ok": True}
            if cmd in ("start", "start_all"):
                self.cfg = load_config()
                err = config_error(self.cfg)
                if not err:
                    if cmd == "start":
                        err = self.start_stream(req.get("id"))
                    else:
                        err = self.start_all()
                self.notice = ""
                return {"ok": not err, "error": err or ""}
            if cmd == "stop":
                w = self.workers.get(req.get("id"))
                if w is not None and w.running:
                    w.stop()
                return {"ok": True}
            if cmd == "stop_all":
                self.stop_all()
                return {"ok": True}
            if cmd == "shutdown":
                self.log("Zahtev za zaustavljanje servisa")
                self.stop_all()
                self.shutdown_evt.set()
                return {"ok": True}
        return {"ok": False, "error": "Nepoznata komanda"}

    def _serve(self, srv):
        while not self.shutdown_evt.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.settimeout(5)
                buf = b""
                while not buf.endswith(b"\n") and len(buf) < 65536:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                resp = self.handle(json.loads(buf.decode("utf-8")))
                conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
            except Exception as e:
                self.log("IPC greska: %r" % (e,))
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    def run(self):
        try:
            if os.path.isfile(SERVICE_LOG) and os.path.getsize(SERVICE_LOG) > 2_000_000:
                os.replace(SERVICE_LOG, SERVICE_LOG + ".old")
        except Exception:
            pass
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":
            # Linux/macOS: dozvoli brz ponovni start (TIME_WAIT); i dalje samo jedan listener.
            # Na Windowsu SO_REUSEADDR dozvoljava dupli bind, pa se ne koristi.
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", SERVICE_PORT))  # samo jedan servis
        except OSError:
            self.log("Servis vec radi (port zauzet) - izlazim")
            return
        srv.listen(5)
        srv.settimeout(0.5)
        threading.Thread(target=self._serve, args=(srv,), daemon=True).start()
        self.log("Servis pokrenut (PID %d)" % os.getpid())
        while not self.shutdown_evt.is_set():
            with self.lock:
                self.drain()
                self._gc_workers()
                try:
                    self.sched_tick()
                except Exception as e:
                    self.sched_msg = "Greska rasporeda: %s" % e
                if time.time() - self.last_cleanup >= 3600:
                    self.last_cleanup = time.time()
                    self.cleanup_async()
            time.sleep(0.5)
        deadline = time.time() + 15
        while any(w.running for w in list(self.workers.values())) and time.time() < deadline:
            time.sleep(0.2)
        with self.lock:
            self.drain()
        try:
            srv.close()
        except Exception:
            pass
        self.log("Servis zaustavljen")


def run_service():
    eng = Engine()
    try:
        eng.run()
    except Exception:
        import traceback
        eng.log("PAD SERVISA:\n" + traceback.format_exc())


class StatusPoller(threading.Thread):
    """U pozadini svake sekunde pita servis za stanje (da prozor ne zastaje)."""

    def __init__(self):
        super().__init__(daemon=True)
        self._lock = threading.Lock()
        self._status = None
        self._halt = threading.Event()

    def run(self):
        while not self._halt.is_set():
            try:
                st = ipc_request({"cmd": "status"}, timeout=2.0)
                st = st if st.get("ok") else None
            except Exception:
                st = None
            with self._lock:
                self._status = st
            self._halt.wait(1.0)

    def get(self):
        with self._lock:
            return self._status

    def stop(self):
        self._halt.set()


# ----------------------------------------------------------------------------
# Monitor (preview jednog kanala + indikator zvuka)
# ----------------------------------------------------------------------------
def input_args(cfg, url):
    """Ulazne opcije za ffmpeg (iste kao pri snimanju) + mali bafer za nisko kasnjenje."""
    args = ["-fflags", "nobuffer", "-analyzeduration", "2000000", "-probesize", "2000000"]
    if url.lower().startswith("rtsp"):
        major = ffmpeg_major(cfg["ffmpeg"])
        tmo = "-stimeout" if (major is not None and major < 5) else "-timeout"
        args += ["-rtsp_transport", cfg["transport"], tmo, "10000000"]
    else:
        args += ["-rw_timeout", "10000000"]
    return args


class Preview:
    """Posebna ffmpeg veza koja daje malu sliku (PPM kadrovi preko pipe-a) i zvuk
    (s16le preko lokalnog UDP-a) za jedan izabrani kanal. Radi nezavisno od snimanja."""

    W, H, FPS = 320, 180, 5

    def __init__(self):
        self.lock = threading.Lock()
        self.gen = 0
        self.proc = None
        self.running = False
        self.status = "Iskljuceno"
        self.frame = None
        self.seq = 0
        self.frame_time = 0.0
        self.peak = [0.0, 0.0]

    # -- javni API --
    def start(self, cfg, url):
        self.stop()
        with self.lock:
            self.gen += 1
            gen = self.gen
            self.running = True
            self.status = "Povezivanje..."
        if not url:
            self._set(gen, "URL nije unet")
            return
        if shutil.which(cfg["ffmpeg"]) is None and not os.path.isfile(cfg["ffmpeg"]):
            self._set(gen, "ffmpeg nije pronadjen")
            return
        threading.Thread(target=self._run, args=(gen, cfg, url), daemon=True).start()

    def stop(self):
        with self.lock:
            self.gen += 1
            self.running = False
            self.status = "Iskljuceno"
            self.frame = None
            self.seq += 1
            self.peak = [0.0, 0.0]
            p, self.proc = self.proc, None
        if p is not None:
            try:
                p.kill()
            except Exception:
                pass

    def snapshot(self):
        with self.lock:
            return self.running, self.status, self.frame, self.seq, self.frame_time

    def take_peaks(self):
        """Maksimum (0..1) po kanalu od prethodnog poziva."""
        with self.lock:
            p, self.peak = self.peak, [0.0, 0.0]
        return p

    # -- interno --
    def _set(self, gen, status):
        with self.lock:
            if gen == self.gen:
                self.status = status

    def _alive(self, gen):
        return gen == self.gen

    def _build(self, cfg, url, port):
        w, h = self.W, self.H
        cmd = [cfg["ffmpeg"], "-hide_banner", "-loglevel", "error", "-nostdin"]
        cmd += input_args(cfg, url)
        cmd += ["-i", url,
                "-map", "0:v:0",
                "-vf", ("fps=%d,scale=%d:%d:force_original_aspect_ratio=decrease,"
                        "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:black") % (self.FPS, w, h, w, h),
                "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"]
        if port:
            cmd += ["-map", "0:a:0?", "-ac", "2", "-ar", "8000", "-f", "s16le",
                    "udp://127.0.0.1:%d?pkt_size=1024" % port]
        return cmd

    @staticmethod
    def _read_exact(f, n):
        data = f.read(n)
        return data if data is not None and len(data) == n else None

    def _audio_loop(self, gen, sock):
        while self._alive(gen):
            try:
                data = sock.recvfrom(4096)[0]
            except socket.timeout:
                continue
            except OSError:
                break
            n = len(data) // 4 * 4
            if n == 0:
                continue
            a = array("h")
            a.frombytes(data[:n])
            if sys.byteorder == "big":
                a.byteswap()
            left, right = a[0::2], a[1::2]
            pl = max(max(left), -min(left)) / 32768.0
            pr = max(max(right), -min(right)) / 32768.0
            with self.lock:
                if gen == self.gen:
                    self.peak[0] = max(self.peak[0], pl)
                    self.peak[1] = max(self.peak[1], pr)

    def _run(self, gen, cfg, url):
        use_audio = True
        fsize = self.W * self.H * 3
        while self._alive(gen):
            sock, port = None, 0
            if use_audio:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.bind(("127.0.0.1", 0))
                    sock.settimeout(0.5)
                    port = sock.getsockname()[1]
                    threading.Thread(target=self._audio_loop, args=(gen, sock), daemon=True).start()
                except OSError:
                    sock, port, use_audio = None, 0, False
            flags = 0
            if os.name == "nt":
                flags = subprocess.CREATE_NO_WINDOW | 0x00004000  # + BELOW_NORMAL priority
            try:
                p = subprocess.Popen(self._build(cfg, url, port), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     bufsize=fsize * 2, creationflags=flags)
            except Exception as e:
                if sock:
                    sock.close()
                self._set(gen, "Greska: %s" % e)
                return
            with self.lock:
                if gen != self.gen:
                    p.kill()
                    if sock:
                        sock.close()
                    return
                self.proc = p

            errs = []

            def drain(proc=p, out=errs):
                try:
                    for line in proc.stderr:
                        out.append(line.decode(errors="replace").strip())
                        del out[:-20]
                except Exception:
                    pass
            threading.Thread(target=drain, daemon=True).start()

            got = 0
            while self._alive(gen):
                buf = self._read_exact(p.stdout, fsize)
                if buf is None:
                    break
                got += 1
                with self.lock:
                    if gen != self.gen:
                        break
                    self.frame = buf
                    self.seq += 1
                    self.frame_time = time.time()
                    self.status = "Uzivo"
            try:
                p.kill()
            except Exception:
                pass
            try:
                p.wait(timeout=3)
            except Exception:
                pass
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
            if not self._alive(gen):
                return
            text = " ".join(errs).lower()
            if use_audio and got == 0 and "does not contain any stream" in text:
                use_audio = False  # izvor nema zvuk -> ponovo samo sa slikom
                continue
            self._set(gen, "Nema signala - ponovno povezivanje...")
            with self.lock:
                self.peak = [0.0, 0.0]
            for _ in range(30):
                if not self._alive(gen):
                    return
                time.sleep(0.1)
            use_audio = True


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------
class App(tk.Tk if tk else object):
    def __init__(self):
        super().__init__()
        self.title("IP Recorder")
        self.resizable(True, False)
        self.res_q = queue.Queue()
        self.states = {}   # id strima -> stanje
        self.last = {}
        self.rows = []     # redovi liste strimova
        self._mon_id = None
        self.svc = None  # poslednji status servisa (None = servis ne radi)
        self._ver_prompted = False
        try:
            self._keep_ok = int(load_config().get("keep_days") or 0)  # vec potvrdjena vrednost
        except ValueError:
            self._keep_ok = 0
        self.blink = False

        self.cfg = load_config()
        self.preview = Preview()
        self._mon_seq = -1
        self._mon_img_on = False
        self.lv = [0.0, 0.0]
        self.hold = [0.0, 0.0]
        self.hold_t = [0.0, 0.0]
        self._build_ui()
        self.poller = StatusPoller()
        self.poller.start()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(300, self._poll)
        self.after(500, self._blink)
        self.after(100, self._mon_tick)

    # -- konfiguracija --
    def _save_cfg(self):
        try:
            save_config(self._collect_cfg())
        except Exception as e:
            messagebox.showerror("Greska", "Podesavanja nisu sacuvana: %s" % e)

    def _collect_cfg(self):
        c = {
            "ffmpeg": self.v_ffmpeg.get().strip() or "ffmpeg",
            "folder": self.v_folder.get().strip(),
            "codec": self.v_codec.get(),
            "x264_preset": self.v_preset.get(),
            "mp4_fragmented": self.v_frag.get(),
            "keep_days": self.v_keep.get().strip(),
            "video_kbps": self.v_vb.get().strip(),
            "audio_kbps": self.v_ab.get().strip(),
            "resolution": self.v_res.get().strip(),
            "fps": self.v_fps.get().strip(),
            "segment_min": self.v_seg.get().strip(),
            "align_clock": self.v_align.get(),
            "template": self.v_tpl.get().strip(),
            "subfolders": self.v_sub.get(),
            "transport": self.v_trans.get(),
            "reconnect": self.v_recon.get(),
            "reconnect_delay": self.v_delay.get().strip(),
            "sched_enabled": self.v_sched.get(),
            "sched_start": self.v_sstart.get().strip(),
            "sched_stop": self.v_sstop.get().strip(),
            "sched_days": [v.get() for v in self.v_days],
            "cpu_cap": self.v_cap.get().strip(),
            "priority": self.v_prio.get(),
            "threads": self.v_threads.get().strip(),
            "streams": [
                {"id": r["id"], "enabled": r["en"].get(),
                 "name": r["name"].get().strip(), "url": r["url"].get().strip()}
                for r in self.rows
            ],
        }
        return c

    # -- UI --
    def _build_ui(self):
        pad = {"padx": 6, "pady": 3}
        main = ttk.Frame(self, padding=8)
        main.pack(fill="both", expand=True)

        top_row = ttk.Frame(main)
        top_row.pack(fill="x", pady=(0, 8))
        left = ttk.Frame(top_row)
        left.pack(side="left", fill="both", expand=True)
        monframe = ttk.LabelFrame(top_row, text=" Monitor ")
        monframe.pack(side="left", fill="y", padx=(8, 0))

        # Servis
        sv = ttk.LabelFrame(left, text=" Servis (snimanje u pozadini, bez prozora) ")
        sv.pack(fill="x")
        top = ttk.Frame(sv)
        top.pack(fill="x", padx=6, pady=4)
        self.svc_canvas = tk.Canvas(top, width=22, height=22, highlightthickness=0)
        self.svc_dot = self.svc_canvas.create_oval(3, 3, 19, 19, fill="#9a9a9a", outline="#333")
        self.svc_canvas.pack(side="left", padx=(2, 4))
        self.v_svc_text = tk.StringVar(value="Servis nije pokrenut")
        ttk.Label(top, textvariable=self.v_svc_text, width=30,
                  font=("TkDefaultFont", 10, "bold")).pack(side="left")
        self.btn_svc = ttk.Button(top, text="Pokreni servis", width=16, command=self._toggle_service)
        self.btn_svc.pack(side="left", padx=4)
        ttk.Button(top, text="Sacuvaj i primeni", command=self._apply).pack(side="left", padx=4)
        self.v_autostart = tk.BooleanVar(value=autostart_enabled())
        acb = ttk.Checkbutton(sv, text="Automatski pokreni servis pri prijavi na Windows",
                              variable=self.v_autostart, command=self._toggle_autostart)
        acb.pack(anchor="w", padx=10)
        if os.name != "nt":
            acb.config(state="disabled")
        ttk.Label(sv, foreground="#555",
                  text="Servis snima i kad zatvorite ovaj prozor. Raspored radi samo dok je servis pokrenut."
                  ).pack(anchor="w", padx=10, pady=(0, 4))

        # Podesavanja snimanja
        box = ttk.LabelFrame(main, text=" Podesavanja snimanja ")
        box.pack(fill="x")
        c = self.cfg

        self.v_vb = tk.StringVar(value=c["video_kbps"])
        self.v_ab = tk.StringVar(value=c["audio_kbps"])
        self.v_res = tk.StringVar(value=c["resolution"])
        self.v_fps = tk.StringVar(value=c["fps"])
        self.v_seg = tk.StringVar(value=c["segment_min"])
        self.v_align = tk.BooleanVar(value=c["align_clock"])
        self.v_tpl = tk.StringVar(value=c["template"])
        self.v_sub = tk.BooleanVar(value=c["subfolders"])
        self.v_trans = tk.StringVar(value=c["transport"])
        self.v_recon = tk.BooleanVar(value=c["reconnect"])
        self.v_delay = tk.StringVar(value=c["reconnect_delay"])
        self.v_sched = tk.BooleanVar(value=c["sched_enabled"])
        self.v_sstart = tk.StringVar(value=c["sched_start"])
        self.v_sstop = tk.StringVar(value=c["sched_stop"])
        days_cfg = (list(c["sched_days"]) + [True] * 7)[:7]
        self.v_days = [tk.BooleanVar(value=bool(d)) for d in days_cfg]
        self.v_sstatus = tk.StringVar(value="")
        self.v_codec = tk.StringVar(value=c["codec"] if c["codec"] in ALL_CODECS else CODEC_WMV)
        self.v_preset = tk.StringVar(value=c["x264_preset"])
        self.v_frag = tk.BooleanVar(value=c["mp4_fragmented"])
        self.v_keep = tk.StringVar(value=str(c["keep_days"]))
        self.v_cleanup = tk.StringVar(value="")
        self.v_cap = tk.StringVar(value=c["cpu_cap"])
        self.v_prio = tk.StringVar(value=c["priority"])
        self.v_threads = tk.StringVar(value=c["threads"])
        self.v_folder = tk.StringVar(value=c["folder"])
        self.v_ffmpeg = tk.StringVar(value=c["ffmpeg"])

        ttk.Label(box, text="Video bitrate (kbps):").grid(row=0, column=0, sticky="e", **pad)
        self.e_vb = ttk.Entry(box, textvariable=self.v_vb, width=10)
        self.e_vb.grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(box, text="Audio bitrate (kbps, 0 = bez zvuka):").grid(row=0, column=2, sticky="e", **pad)
        ttk.Entry(box, textvariable=self.v_ab, width=10).grid(row=0, column=3, sticky="w", **pad)

        ttk.Label(box, text="Rezolucija:").grid(row=1, column=0, sticky="e", **pad)
        self.cb_res = ttk.Combobox(box, textvariable=self.v_res, width=12,
                                   values=["Original", "1920x1080", "1280x720", "854x480",
                                           "640x480", "640x360", "352x288"])
        self.cb_res.grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(box, text="FPS (0 = original):").grid(row=1, column=2, sticky="e", **pad)
        self.e_fps = ttk.Entry(box, textvariable=self.v_fps, width=10)
        self.e_fps.grid(row=1, column=3, sticky="w", **pad)

        ttk.Label(box, text="Deljenje fajla na svakih (min, 0 = bez):").grid(row=2, column=0, columnspan=1, sticky="e", **pad)
        ttk.Entry(box, textvariable=self.v_seg, width=10).grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(box, text="Sablon imena (strftime):").grid(row=2, column=2, sticky="e", **pad)
        ttk.Entry(box, textvariable=self.v_tpl, width=24).grid(row=2, column=3, sticky="w", **pad)
        ttk.Checkbutton(box, text="Deli na pun sat (kao DVR: 00-02, 02-04...)",
                        variable=self.v_align).grid(row=2, column=4, sticky="w", **pad)

        ttk.Label(box, text="Folder za snimke:").grid(row=3, column=0, sticky="e", **pad)
        ttk.Entry(box, textvariable=self.v_folder, width=60).grid(row=3, column=1, columnspan=3, sticky="we", **pad)
        ttk.Button(box, text="Izaberi...", command=self._pick_folder).grid(row=3, column=4, **pad)

        ttk.Label(box, text="ffmpeg putanja:").grid(row=4, column=0, sticky="e", **pad)
        ttk.Entry(box, textvariable=self.v_ffmpeg, width=60).grid(row=4, column=1, columnspan=3, sticky="we", **pad)
        ttk.Button(box, text="Izaberi...", command=self._pick_ffmpeg).grid(row=4, column=4, **pad)

        opts = ttk.Frame(box)
        opts.grid(row=5, column=0, columnspan=5, sticky="w", **pad)
        ttk.Checkbutton(opts, text="Poseban folder za svaku kameru",
                        variable=self.v_sub).pack(side="left", padx=6)
        ttk.Label(opts, text="RTSP transport:").pack(side="left", padx=(12, 2))
        ttk.Combobox(opts, textvariable=self.v_trans, values=["tcp", "udp"],
                     width=5, state="readonly").pack(side="left")
        ttk.Checkbutton(opts, text="Automatsko ponovno povezivanje",
                        variable=self.v_recon).pack(side="left", padx=(12, 2))
        ttk.Label(opts, text="posle (s):").pack(side="left")
        ttk.Entry(opts, textvariable=self.v_delay, width=4).pack(side="left", padx=2)

        cpu = ttk.Frame(box)
        cpu.grid(row=6, column=0, columnspan=5, sticky="w", **pad)
        ttk.Label(cpu, text="Maks. CPU za sve strimove zajedno (%, 0 = bez limita):").pack(side="left", padx=6)
        cap_entry = ttk.Entry(cpu, textvariable=self.v_cap, width=5)
        cap_entry.pack(side="left")
        if not CPU_JOB.job:
            cap_entry.config(state="disabled")  # tvrdi limit: Windows 8+ (ne radi na Win 7)
            ttk.Label(cpu, text="(nedostupno na ovom sistemu)",
                      foreground="#a00").pack(side="left", padx=4)
        ttk.Label(cpu, text="Prioritet:").pack(side="left", padx=(14, 2))
        ttk.Combobox(cpu, textvariable=self.v_prio, values=list(PRIORITIES),
                     width=16, state="readonly").pack(side="left")
        ttk.Label(cpu, text="Niti po strimu (0 = auto):").pack(side="left", padx=(14, 2))
        ttk.Entry(cpu, textvariable=self.v_threads, width=4).pack(side="left")

        fmt = ttk.Frame(box)
        fmt.grid(row=7, column=0, columnspan=5, sticky="w", **pad)
        ttk.Label(fmt, text="Format snimka:").pack(side="left", padx=6)
        ttk.Combobox(fmt, textvariable=self.v_codec, values=ALL_CODECS,
                     width=26, state="readonly").pack(side="left")
        self.lbl_preset = ttk.Label(fmt, text="x264 preset (ultrafast = najmanje CPU):")
        self.lbl_preset.pack(side="left", padx=(14, 2))
        self.cb_preset = ttk.Combobox(fmt, textvariable=self.v_preset, values=X264_PRESETS,
                                      width=10, state="readonly")
        self.cb_preset.pack(side="left")
        self.cb_frag = ttk.Checkbutton(fmt, text="Fragmentirani MP4 (siguran pri prekidu)",
                                       variable=self.v_frag)
        self.cb_frag.pack(side="left", padx=(14, 0))
        ret = ttk.Frame(box)
        ret.grid(row=8, column=0, columnspan=5, sticky="w", **pad)
        ttk.Label(ret, text="Brisanje starih snimaka - starije od (dana, 0 = ne brisi):").pack(side="left", padx=6)
        ttk.Entry(ret, textvariable=self.v_keep, width=5).pack(side="left")
        ttk.Label(ret, textvariable=self.v_cleanup, foreground="#555").pack(side="left", padx=(12, 0))
        self.v_codec.trace_add("write", lambda *a: self._codec_changed())
        self._codec_changed()
        box.columnconfigure(3, weight=1)

        # Raspored
        rbox = ttk.LabelFrame(left, text=" Raspored snimanja (automatsko ukljucivanje / iskljucivanje) ")
        rbox.pack(fill="x", pady=(8, 0))
        r1 = ttk.Frame(rbox)
        r1.pack(fill="x", padx=6, pady=3)
        ttk.Checkbutton(r1, text="Ukljuci raspored", variable=self.v_sched).pack(side="left", padx=6)
        ttk.Label(r1, text="Pocetak (HH:MM):").pack(side="left", padx=(14, 2))
        ttk.Entry(r1, textvariable=self.v_sstart, width=7).pack(side="left")
        ttk.Label(r1, text="Kraj (HH:MM):").pack(side="left", padx=(14, 2))
        ttk.Entry(r1, textvariable=self.v_sstop, width=7).pack(side="left")
        r2 = ttk.Frame(rbox)
        r2.pack(fill="x", padx=6)
        ttk.Label(r2, text="Dani:").pack(side="left", padx=(6, 2))
        for k, name in enumerate(DAY_NAMES):
            ttk.Checkbutton(r2, text=name, variable=self.v_days[k]).pack(side="left", padx=1)
        ttk.Label(rbox, textvariable=self.v_sstatus, foreground="#0a5").pack(
            anchor="w", padx=12, pady=(0, 4))

        self._build_monitor(monframe)

        # Strimovi (lista se skroluje kad ima vise od VISIBLE_ROWS redova)
        sbox = ttk.LabelFrame(main, text=" IP strimovi ")
        sbox.pack(fill="x", pady=(8, 0))
        self.s_canvas = tk.Canvas(sbox, highlightthickness=0, height=60)
        self.s_scroll = ttk.Scrollbar(sbox, orient="vertical", command=self.s_canvas.yview)
        self.s_canvas.configure(yscrollcommand=self.s_scroll.set)
        self.s_canvas.pack(side="left", fill="x", expand=True)
        self.s_inner = ttk.Frame(self.s_canvas)
        self.s_win = self.s_canvas.create_window((0, 0), window=self.s_inner, anchor="nw")
        self.s_inner.bind("<Configure>", self._rows_resized)
        self.s_canvas.bind("<Configure>", lambda e: self.s_canvas.itemconfig(self.s_win, width=e.width))
        self.bind_all("<MouseWheel>", self._on_wheel)
        self.bind_all("<Button-4>", self._on_wheel)
        self.bind_all("<Button-5>", self._on_wheel)
        for st in c["streams"]:
            self._add_row(st)
        self._rows_resized()
        self._mon_refresh_values()

        # Donji dugmici
        bot = ttk.Frame(main)
        bot.pack(fill="x", pady=(10, 0))
        ttk.Button(bot, text="▶ Pokreni sve", command=self._start_all).pack(side="left")
        ttk.Button(bot, text="■ Zaustavi sve", command=self._stop_all).pack(side="left", padx=6)
        ttk.Button(bot, text="+ Dodaj strim", command=self._add_stream).pack(side="left", padx=(18, 0))
        self.v_global = tk.StringVar(value="Spremno.")
        ttk.Label(bot, textvariable=self.v_global, font=("TkDefaultFont", 10, "bold")).pack(side="right")

    # -- redovi liste strimova --
    def _row(self, sid):
        for r in self.rows:
            if r["id"] == sid:
                return r
        return None

    def _add_row(self, st):
        sid = st["id"]
        f = ttk.Frame(self.s_inner)
        f.pack(fill="x", padx=6, pady=4)
        en = tk.BooleanVar(value=st["enabled"])
        nm = tk.StringVar(value=st["name"])
        ur = tk.StringVar(value=st["url"])
        ttk.Checkbutton(f, variable=en).grid(row=0, column=0)
        ttk.Entry(f, textvariable=nm, width=12).grid(row=0, column=1, padx=4)
        url_entry = ttk.Entry(f, textvariable=ur, width=46)
        url_entry.grid(row=0, column=2, padx=4, sticky="we")
        cv = tk.Canvas(f, width=22, height=22, highlightthickness=0)
        dot = cv.create_oval(3, 3, 19, 19, fill=STATE_COLOR[IDLE], outline="#333")
        cv.grid(row=0, column=3, padx=4)
        status = ttk.Label(f, text=STATE_TEXT[IDLE], width=22)
        status.grid(row=0, column=4)
        btn = ttk.Button(f, text="Start", width=7, command=lambda k=sid: self._toggle(k))
        btn.grid(row=0, column=5, padx=4)
        ttk.Button(f, text="×", width=3, command=lambda k=sid: self._remove_row(k)).grid(
            row=0, column=6, padx=(0, 4))
        info = ttk.Label(f, text="", foreground="#555")
        info.grid(row=1, column=2, columnspan=4, sticky="w", padx=4)
        f.columnconfigure(2, weight=1)
        row = {"id": sid, "frame": f, "en": en, "name": nm, "url": ur, "url_entry": url_entry,
               "cv": cv, "dot": dot, "status": status, "btn": btn, "info": info}
        self.rows.append(row)
        return row

    def _add_stream(self):
        if len(self.rows) >= MAX_STREAMS:
            messagebox.showinfo("Strimovi", "Najvise je dozvoljeno %d strimova." % MAX_STREAMS)
            return
        sid = next_stream_id([r["id"] for r in self.rows])
        names = {r["name"].get().strip().lower() for r in self.rows}
        n = len(self.rows) + 1
        while "kamera%d" % n in names:
            n += 1
        row = self._add_row({"id": sid, "enabled": True, "name": "Kamera%d" % n, "url": ""})
        self._rows_resized()
        self.update_idletasks()
        self.s_canvas.yview_moveto(1.0)
        self._mon_refresh_values()
        row["url_entry"].focus_set()

    def _remove_row(self, sid):
        row = self._row(sid)
        if row is None:
            return
        if len(self.rows) <= 1:
            messagebox.showinfo("Strimovi", "Mora ostati bar jedan strim.")
            return
        name = row["name"].get().strip() or sid
        active = self.states.get(sid, IDLE) in (CONNECTING, RECORDING, RECONNECTING)
        msg = "Obrisati strim \"%s\" iz liste?" % name
        if active:
            msg += "\n\nSnimanje ovog strima bice zaustavljeno."
        msg += "\n\nVec snimljeni fajlovi se ne brisu."
        if not messagebox.askyesno("Brisanje strima", msg):
            return
        if self._mon_id == sid:
            self.preview.stop()
            self._mon_id = None
        if active or self.svc is not None:
            self._send({"cmd": "stop", "id": sid}, quiet=True)
        row["frame"].destroy()
        self.rows.remove(row)
        self.states.pop(sid, None)
        self.last.pop(sid, None)
        self._save_cfg()
        self._rows_resized()
        self._mon_refresh_values()

    def _rows_resized(self, _event=None):
        need = self.s_inner.winfo_reqheight()
        n = max(1, len(self.rows))
        vis = min(need, int(need / n * VISIBLE_ROWS)) if self.rows else 40
        self.s_canvas.configure(height=max(vis, 40), width=self.s_inner.winfo_reqwidth(),
                                scrollregion=(0, 0, self.s_inner.winfo_reqwidth(), need))
        if need > vis + 2:
            if not self.s_scroll.winfo_ismapped():
                self.s_scroll.pack(side="right", fill="y", before=self.s_canvas)
        else:
            self.s_scroll.pack_forget()
            self.s_canvas.yview_moveto(0)

    def _on_wheel(self, e):
        try:
            if not self.s_scroll.winfo_ismapped() or not str(e.widget).startswith(str(self.s_canvas)):
                return
        except Exception:
            return
        if getattr(e, "num", 0) == 4:
            self.s_canvas.yview_scroll(-1, "units")
        elif getattr(e, "num", 0) == 5:
            self.s_canvas.yview_scroll(1, "units")
        else:
            self.s_canvas.yview_scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1), "units")

    def _pick_folder(self):
        d = filedialog.askdirectory(initialdir=self.v_folder.get() or None)
        if d:
            self.v_folder.set(d)

    def _pick_ffmpeg(self):
        f = filedialog.askopenfilename(title="Izaberi ffmpeg")
        if f:
            self.v_ffmpeg.set(f)

    def _codec_changed(self):
        codec = self.v_codec.get()
        enc = codec == CODEC_MP4                      # H.264 sa rekodiranjem
        mp4 = codec in (CODEC_MP4, CODEC_COPY)
        copy = codec == CODEC_COPY
        self.cb_preset.config(state="readonly" if enc else "disabled")
        self.lbl_preset.config(foreground="" if enc else "#999")
        self.cb_frag.config(state="normal" if mp4 else "disabled")
        self.e_vb.config(state="disabled" if copy else "normal")
        self.e_fps.config(state="disabled" if copy else "normal")
        self.cb_res.config(state="disabled" if copy else "normal")

    # -- monitor --
    def _build_monitor(self, parent):
        W, H = Preview.W, Preview.H
        hdr = ttk.Frame(parent)
        hdr.pack(fill="x", padx=6, pady=(4, 3))
        ttk.Label(hdr, text="Kanal:").pack(side="left")
        self.v_mon = tk.StringVar(value="Iskljuceno")
        self.cb_mon = ttk.Combobox(hdr, textvariable=self.v_mon, state="readonly", width=20,
                                   values=self._mon_values(), postcommand=self._mon_refresh_values)
        self.cb_mon.pack(side="left", padx=4)
        self.cb_mon.bind("<<ComboboxSelected>>", self._mon_selected)
        ttk.Button(hdr, text="Osvezi", width=7, command=self._mon_restart).pack(side="left")

        view = ttk.Frame(parent)
        view.pack(padx=6, pady=(0, 6))
        self.mon_canvas = tk.Canvas(view, width=W, height=H, bg="black", highlightthickness=0)
        self.mon_canvas.pack(side="left")
        self.mon_item = self.mon_canvas.create_image(0, 0, anchor="nw")
        self.mon_text = self.mon_canvas.create_text(W // 2, H // 2, text="Monitor iskljucen",
                                                    fill="#aaaaaa", width=W - 20, justify="center")
        # indikator zvuka: dve uske linije (L, R) zalepljene uz desnu ivicu slike
        self.meter = tk.Canvas(view, width=11, height=H, bg="#141414", highlightthickness=0)
        self.meter.pack(side="left", padx=0)
        self.m_items = []
        for ch in (0, 1):
            g = self.meter.create_rectangle(-9, -9, -8, -8, fill="#19c43a", width=0)
            y = self.meter.create_rectangle(-9, -9, -8, -8, fill="#e6c700", width=0)
            r = self.meter.create_rectangle(-9, -9, -8, -8, fill="#e03030", width=0)
            hl = self.meter.create_rectangle(-9, -9, -8, -8, fill="#ffffff", width=0)
            self.m_items.append((g, y, r, hl))

    def _mon_values(self):
        vals = ["Iskljuceno"]
        for i, r in enumerate(self.rows):
            vals.append("%d: %s" % (i + 1, r["name"].get().strip() or "Kamera%d" % (i + 1)))
        return vals

    def _mon_refresh_values(self):
        vals = self._mon_values()
        self.cb_mon.config(values=vals)
        idx = next((i for i, r in enumerate(self.rows) if r["id"] == self._mon_id), None)
        self.v_mon.set(vals[idx + 1] if idx is not None else "Iskljuceno")

    def _mon_selected(self, _event=None):
        idx = self.cb_mon.current() - 1
        self._mon_id = self.rows[idx]["id"] if 0 <= idx < len(self.rows) else None
        self._mon_restart()

    def _mon_restart(self):
        self.preview.stop()
        self._mon_seq = -1
        row = self._row(self._mon_id) if self._mon_id else None
        if row is None:
            self._mon_id = None
            return
        self.preview.start(self._collect_cfg(), row["url"].get().strip())

    def _mon_tick(self):
        try:
            running, status, frame, seq, ftime = self.preview.snapshot()
            now = time.time()
            live = running and frame is not None and now - ftime < 3.0
            if live:
                if seq != self._mon_seq:
                    self._mon_seq = seq
                    try:
                        self.mon_img = tk.PhotoImage(
                            data=b"P6\n%d %d\n255\n" % (Preview.W, Preview.H) + frame)
                        self.mon_canvas.itemconfig(self.mon_item, image=self.mon_img)
                        self._mon_img_on = True
                    except tk.TclError:
                        pass
                self.mon_canvas.itemconfig(self.mon_text, state="hidden")
            else:
                if self._mon_img_on:
                    self.mon_canvas.itemconfig(self.mon_item, image="")
                    self._mon_img_on = False
                self._mon_seq = -1
                txt = status if running else "Monitor iskljucen"
                if running and status == "Uzivo":
                    txt = "Nema signala"
                self.mon_canvas.itemconfig(self.mon_text, text=txt, state="normal")
            self._draw_meter(self.preview.take_peaks() if live else [0.0, 0.0], now)
        except Exception:
            pass
        self.after(50, self._mon_tick)

    def _draw_meter(self, peaks, now):
        H = Preview.H
        Y_AT, R_AT = 0.70, 0.90  # zuta od -18 dB, crvena od -6 dB (skala -60..0 dB)
        for ch in (0, 1):
            p = peaks[ch]
            db = 20.0 * math.log10(p) if p > 1e-5 else -100.0
            frac = min(1.0, max(0.0, (db + 60.0) / 60.0))
            self.lv[ch] = max(frac, self.lv[ch] - 0.035)
            if frac >= self.hold[ch]:
                self.hold[ch], self.hold_t[ch] = frac, now
            elif now - self.hold_t[ch] > 1.0:
                self.hold[ch] = max(frac, self.hold[ch] - 0.02)
            lvl = self.lv[ch]
            x0 = ch * 6
            x1 = x0 + 5
            g, y, r, hl = self.m_items[ch]
            segs = ((g, 0.0, min(lvl, Y_AT)),
                    (y, Y_AT, min(max(lvl, Y_AT), R_AT)),
                    (r, R_AT, max(lvl, R_AT)))
            for item, lo, hi in segs:
                if hi - lo > 0.004:
                    self.meter.coords(item, x0, H - hi * H, x1, H - lo * H)
                else:
                    self.meter.coords(item, -9, -9, -8, -8)
            if self.hold[ch] > 0.01:
                yh = H - self.hold[ch] * H
                self.meter.coords(hl, x0, yh, x1, yh + 2)
            else:
                self.meter.coords(hl, -9, -9, -8, -8)

    # -- komunikacija sa servisom --
    def _validate(self, cfg):
        err = config_error(cfg)
        if err:
            messagebox.showerror("Greska", err)
            return False
        keep = int(cfg.get("keep_days") or 0)
        if keep > 0 and keep != self._keep_ok:
            if not messagebox.askyesno(
                    "Brisanje starih snimaka",
                    "Snimci (.wmv/.mp4) kamera iz foldera:\n%s\nstariji od %d dana bice TRAJNO obrisani "
                    "(proverava se na svaki sat).\n\nNastaviti?" % (cfg["folder"], keep)):
                return False
        self._keep_ok = keep
        seg = int(float(cfg["segment_min"]) * 60)
        if cfg["align_clock"] and seg > 0 and 86400 % seg != 0:
            if not messagebox.askyesno(
                    "Upozorenje",
                    "Trajanje segmenta se ne deli ravnomerno na 24 h (npr. 60, 120, 180, 240, "
                    "360, 480, 720 ili 1440 min), pa se podela na pun sat resetuje u ponoc.\n\n"
                    "Nastaviti svejedno?"):
                return False
        return True

    def _send(self, cmd, autostart=False, quiet=False, ok_msg=None):
        """Salje komandu servisu u pozadinskoj niti. Ako autostart=True a servis ne radi, prvo ga pokrene."""
        def work():
            try:
                if autostart and self.poller.get() is None and not wait_for_service(0.1):
                    launch_service()
                    if not wait_for_service(12):
                        raise RuntimeError("Servis se nije pokrenuo (pogledajte ip_recorder_service.log).")
                res = ipc_request(cmd, timeout=6.0)
            except Exception as e:
                res = {"ok": False, "error": "Servis nije dostupan: %s" % e, "conn": True}
            self.res_q.put((res, quiet, ok_msg))
        threading.Thread(target=work, daemon=True).start()

    def _toggle(self, sid):
        if self.states.get(sid, IDLE) in (CONNECTING, RECORDING, RECONNECTING):
            self._send({"cmd": "stop", "id": sid}, quiet=True)
            return
        cfg = self._collect_cfg()
        if not self._validate(cfg):
            return
        self._save_cfg()
        self._send({"cmd": "start", "id": sid}, autostart=True)

    def _start_all(self):
        cfg = self._collect_cfg()
        if not self._validate(cfg):
            return
        self._save_cfg()
        self._send({"cmd": "start_all"}, autostart=True)

    def _stop_all(self):
        self._send({"cmd": "stop_all"}, quiet=True)

    def _apply(self):
        cfg = self._collect_cfg()
        if not self._validate(cfg):
            return
        self._save_cfg()
        self._send({"cmd": "apply"}, autostart=bool(cfg["sched_enabled"]), quiet=True,
                   ok_msg="Podesavanja sacuvana i primenjena.")
        if self.svc is None and not cfg["sched_enabled"]:
            self.v_global.set("Podesavanja sacuvana.")

    def _toggle_service(self):
        if self.svc is not None:
            rec = sum(1 for s_ in self.states.values() if s_ == RECORDING)
            if rec and not messagebox.askyesno(
                    "Zaustavi servis", "Snimanje je u toku (%d). Zaustaviti servis i snimanje?" % rec):
                return
            self._send({"cmd": "shutdown"}, quiet=True)
        else:
            cfg = self._collect_cfg()
            if config_error(cfg) is None:
                self._save_cfg()
            self._send({"cmd": "status"}, autostart=True)

    def _offer_restart(self):
        rec = sum(1 for x in self.states.values() if x == RECORDING)
        msg = ("Servis radi sa starom verzijom programa i ne zna za nova podesavanja "
               "(npr. format snimka).\n\nRestartovati servis sada?")
        if rec:
            msg += "\n\nSnimanje koje je u toku (%d) bice prekinuto." % rec
        if messagebox.askyesno("Stari servis", msg):
            self._restart_service()

    def _restart_service(self):
        """Gasi pokrenuti servis, saceka da nestane i pokrece ga ponovo (novom verzijom)."""
        def work():
            try:
                try:
                    ipc_request({"cmd": "shutdown"}, timeout=5)
                except Exception:
                    pass
                end = time.time() + 30
                while time.time() < end:
                    try:
                        ipc_request({"cmd": "status"}, timeout=1)
                        time.sleep(0.4)
                    except Exception:
                        break
                launch_service()
                ok = wait_for_service(12)
                res = {"ok": ok, "conn": False,
                       "error": "" if ok else "Servis se nije ponovo pokrenuo (pogledajte ip_recorder_service.log)."}
            except Exception as e:
                res = {"ok": False, "conn": False, "error": "Restart servisa nije uspeo: %s" % e}
            self.res_q.put((res, False, "Servis restartovan."))
        threading.Thread(target=work, daemon=True).start()

    def _toggle_autostart(self):
        want = self.v_autostart.get()
        try:
            if want:
                self._save_cfg()
            set_autostart(want)
        except Exception as e:
            self.v_autostart.set(not want)
            messagebox.showerror("Greska", "Nije moguce promeniti automatski start: %s" % e)

    # -- osvezavanje UI --
    def _set_state(self, sid, state, info=""):
        self.states[sid] = state
        r = self._row(sid)
        if r is None:
            return
        r["status"].config(text=STATE_TEXT.get(state, state))
        r["info"].config(text=info)
        r["cv"].itemconfig(r["dot"], fill=STATE_COLOR.get(state, "#9a9a9a"))
        r["btn"].config(text="Stop" if state in (CONNECTING, RECORDING, RECONNECTING) else "Start")

    def _update_global(self):
        n = sum(1 for s_ in self.states.values() if s_ == RECORDING)
        if self.svc is None:
            self.v_global.set("Servis nije pokrenut.")
        elif n:
            self.v_global.set("● SNIMANJE U TOKU (%d/%d)" % (n, len(self.rows)))
        else:
            self.v_global.set("Servis radi, snimanje nije u toku.")

    def _poll(self):
        # rezultati komandi
        try:
            while True:
                res, quiet, ok_msg = self.res_q.get_nowait()
                if res.get("ok"):
                    if ok_msg:
                        self.v_global.set(ok_msg)
                elif not quiet or not res.get("conn"):
                    if res.get("error") and not (quiet and res.get("conn")):
                        messagebox.showerror("Servis", res["error"])
        except queue.Empty:
            pass

        st = self.poller.get()
        self.svc = st
        if st:
            stale = st.get("version") != APP_VERSION
            if stale:
                self.v_svc_text.set("Servis radi - STARA VERZIJA")
                self.svc_canvas.itemconfig(self.svc_dot, fill="#ff8c00")
                if not self._ver_prompted:
                    self._ver_prompted = True
                    self.after(200, self._offer_restart)
            else:
                self.v_svc_text.set("Servis radi (PID %s)" % st.get("pid", "?"))
                self.svc_canvas.itemconfig(self.svc_dot, fill="#1faa3c")
            self.btn_svc.config(text="Zaustavi servis")
            sts = st.get("streams") or {}
            for r in list(self.rows):
                cur = tuple(sts.get(r["id"], (IDLE, "")))
                if cur != self.last.get(r["id"]):
                    self.last[r["id"]] = cur
                    self._set_state(r["id"], cur[0], cur[1])
            self.v_sstatus.set(st.get("notice") or st.get("sched") or "")
            self.v_cleanup.set(st.get("cleanup", ""))
        else:
            self.v_svc_text.set("Servis nije pokrenut")
            self.svc_canvas.itemconfig(self.svc_dot, fill="#9a9a9a")
            self.btn_svc.config(text="Pokreni servis")
            for r in list(self.rows):
                if self.last.get(r["id"]) != (IDLE, ""):
                    self.last[r["id"]] = (IDLE, "")
                    self._set_state(r["id"], IDLE, "")
            self.v_sstatus.set("Raspored radi samo dok je servis pokrenut." if self.v_sched.get() else "")
        self._update_global()
        self.after(300, self._poll)

    def _blink(self):
        self.blink = not self.blink
        for r in self.rows:
            if self.states.get(r["id"]) == RECORDING:
                r["cv"].itemconfig(r["dot"], fill=STATE_COLOR[RECORDING] if self.blink else "#ffb0b0")
        self.after(500, self._blink)

    def _on_close(self):
        # servis nastavlja da radi (i snima) i kad se prozor zatvori
        try:
            self._save_cfg()
        except Exception:
            pass
        self.poller.stop()
        self.preview.stop()
        self.destroy()


def hide_console():
    """Sakriva cmd prozor ako je program pokrenut preko python.exe (samo Windows)."""
    if os.name != "nt":
        return
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass


if __name__ == "__main__":
    if "--service" in sys.argv:
        run_service()
        sys.exit(0)
    hide_console()
    if tk is None:
        sys.exit("tkinter nije dostupan - prozor ne moze da se otvori (servis radi i bez njega: --service).")
    try:
        App().mainloop()
    except Exception:
        # bez konzole greske nisu vidljive, pa ih upisujemo u log fajl
        import traceback
        log = os.path.join(BASE_DIR, "ip_recorder_error.log")
        with open(log, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + traceback.format_exc() + "\n")
        try:
            messagebox.showerror("Greska", "Program je prekinut. Detalji su u:\n" + log)
        except Exception:
            pass
