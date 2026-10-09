# IP Recorder

[Srpski](README.md) | **English**

A simple desktop app with a graphical window for recording IP video streams (RTSP, HTTP, UDP/multicast, …) to **WMV** or **H.264 MP4**. Recording is done by a background service with no window, so it keeps running after you close the app window. Powered by [ffmpeg](https://ffmpeg.org/).

> The program interface is in Serbian. Field and button names below are quoted as they appear in the app.

## Features

- Any number of streams (up to 32): add and remove rows in the window
- Three formats: **WMV**, **H.264 MP4** (re-encoded) and **H.264 MP4 without re-encoding** (almost no CPU load)
- Video and audio bitrate, resolution, FPS, file name template and output folder settings
- File splitting aligned to the clock, like a DVR (for example 2 h: 00–02, 02–04, …)
- Recording schedule: start time, end time and days of the week
- Background service that can be started and stopped from the window, with optional auto-start at Windows logon
- Live monitor for one selected channel, with an L/R audio level indicator
- Automatic reconnect when the connection drops
- Automatic deletion of recordings older than a set number of days
- CPU limit (Windows 8+), process priority and thread count

## Requirements

- Python 3.8 or newer (tkinter ships with the standard installer), or the prebuilt `IPRecorder.exe`
- `ffmpeg` (version 5 or newer recommended). H.264 MP4 with re-encoding needs a build that includes `libx264`
- Windows (Windows 7 is supported with Python 3.8); most of the code also runs on Linux

## Running

**From source:**

```
python ip_recorder.py
```

**Prebuilt exe:** put `IPRecorder.exe` and `ffmpeg.exe` in the same folder and run `IPRecorder.exe`.

The background service starts from the window (the "Pokreni servis" button, or the first "Start"). Manually: `python ip_recorder.py --service`.

## Quick start

1. In the "ffmpeg putanja" field pick `ffmpeg.exe` (or place it next to the program).
2. Choose the "Folder za snimke" (recordings folder).
3. In the stream list enter a name and the camera URL, for example `rtsp://user:password@192.168.1.10:554/stream1`.
4. Choose the "Format snimka" and click "Start" (or "Pokreni sve").

Every field is explained in the user guide (in Serbian): [`ip_recorder_uputstvo.html`](ip_recorder_uputstvo.html).

## Building the exe

Locally (needs Python 3.8 and internet access):

```
pip install pyinstaller==5.13.2
pyinstaller --clean --onefile --noconsole --name IPRecorder ip_recorder.py
```

The result is `dist/IPRecorder.exe`. Alternatively use GitHub Actions: the workflow `.github/workflows/build-exe.yml` builds the exe on a Windows runner in the cloud (*Actions → Build exe → Run workflow*), and you download the file from the *Artifacts* section.

## Files the program creates

Next to the program:

| File | Contents |
|------|----------|
| `ip_recorder_settings.json` | all settings |
| `ip_recorder_service.log` | service log (recording start/stop events, errors) |
| `ip_recorder_error.log` | window errors |

## Notes

- **Windows 7:** use Python 3.8 (the last version that supports it). The hard CPU limit needs Windows 8 or newer.
- Multicast addresses in the `232.x.x.x` range (SSM) require the sender address: `udp://232.0.12.9:5000?sources=SOURCE_IP`.
- The service listens only on `127.0.0.1:47653` and is not exposed to the network.
- Deleting old recordings is permanent and only touches `.wmv` and `.mp4` files whose names start with a camera name.
- After replacing `ip_recorder.py` with a newer version, restart the service (the window offers to do it for you).

## License

Not specified. Add a `LICENSE` file if you like.
