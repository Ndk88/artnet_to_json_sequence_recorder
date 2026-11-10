# Art-Net to JSON Sequence Recorder (v1.2)

A small desktop tool that listens for **Art-Net (ArtDMX)** packets on a local UDP port, records the stream sent by
**TouchDesigner**, **Madrix 5** or any other Art-Net source at a fixed frame rate, and saves the recording as a **JSON
sequence** (floats in the range 0..1) that can be loaded and played back offline, for example in Unreal Engine.

The recorder is a single Python file with a Tkinter window. It needs no extra packages.

---

## Contents

1. [Features](#features)
2. [Requirements and files](#requirements-and-files)
3. [Quick start](#quick-start)
4. [User interface](#user-interface)
5. [Recording step by step](#recording-step-by-step)
6. [Output JSON format](#output-json-format)
7. [How it works (algorithms)](#how-it-works-algorithms)
8. [Protection of recordings and files](#protection-of-recordings-and-files)
9. [Test source: packets_tester](#test-source-packets_tester)
10. [Troubleshooting and notes](#troubleshooting-and-notes)
11. [Building an .exe](#building-an-exe)

---

## Features

- Local **UDP server** (default `127.0.0.1:6454`, the standard Art-Net port) with Start/Stop button.
- Parses **ArtDMX** packets of any universe (Art-Net port-address 0..32767); other packet types are counted and ignored.
- **Live statistics**: packet counter and rate, last packet age, every universe that is arriving with its rate and
  channel count, recording progress, and counters of `missing` and `late` frames.
- **Fixed frame rate recording** (fps is configurable): the stream is sampled on a drift-free schedule, so a source
  that sends 60 packets per second can be recorded at 30 fps.
- **Flexible channel mapping**: start universe, start channel, channels per chip (3 = RGB, 4 = RGBW, any 1..512), and
  chips per universe. Chips can span several universes.
- **Automatic stop** after `Sequence Duration x Sequence Fps` frames, or manual stop (duration `0`).
- Optional **start on the first received packet**, so the recording does not begin with empty frames.
- Optional **post-processing** (both are off by default):
  - *Skip first frames* - drops the first N recorded frames (useful if the first frames are unstable).
  - *Loop blend* - cross-fades the end of the recording into its beginning so the sequence loops without a jump.
- **JSON export** with an editable header and a flat float array.
- **Errors are shown in the statistics block** (red, with time stamps), not in pop-up windows.
- **Protection of data**: asks before overwriting a file, offers a new file name, keeps unsaved recordings in memory,
  and warns before closing the window with an unsaved recording.
- A **test Art-Net source** (`packets_tester.py`) with a console log, and launcher `.bat` files for both programs.

---

## Requirements and files

- **Python 3.9 or newer.** On Windows use the installer from python.org and tick *Add python.exe to PATH*; Tkinter is
  included. On Linux install it with `sudo apt install python3-tk`.
- No third-party packages.

| File | Purpose |
|---|---|
| `main.py` | The recorder application (server, recorder, post-processing, JSON export, GUI). |
| `run.bat` | Launches `main.py` without a console window. `run.bat debug` launches it with a console to show errors. |
| `packets_tester.py` | Test Art-Net source that sends an animated rainbow and prints a log. |
| `run_packets_tester.bat` | Launches `packets_tester.py` in a console that stays open. |

---

## Quick start

1. Double-click `run.bat` (or run `python main.py`). If the window does not appear, run `run.bat debug` and read the
   error message.
2. Press **Start Server**.
3. Start your Art-Net source (TouchDesigner, Madrix 5, or `run_packets_tester.bat`). The statistics block must list the
   universe(s) that arrive.
4. Check the *Input mapping* values so they match your patch.
5. Press **Start Record Input Data**. With the default duration of 6 s at 30 fps the recording stops by itself and the
   file is saved to the path at the bottom of the window.

---

## User interface

### Left column

**JSON Fields** - values that are written to the header of the file. All of them are saved as strings.

| Field | Meaning |
|---|---|
| Model Name | Human-readable model name. |
| Model Chips Tag | Tag of the chips of the model (for example an Unreal Engine actor tag). |
| Model Chips Amount | Number of chips. Together with *Channels per chip* it defines the frame size. |
| Sequence Name | Name of the sequence. |
| Sequence Version | Version string of the sequence. |
| Sequence Duration (0 = manual) | Target duration in seconds. `0` means that you stop the recording by hand. After a manual stop the field is updated with the real duration. |
| Sequence Fps | Recording and playback frame rate. |
| Sequence Frame Count | Read-only. Number of frames that will be exported (duration x fps) and, after a recording, the real number. |
| Sequence Frame Stride | Read-only. Numbers per frame = chips x channels per chip. |
| Developer Notes | Free text. **The default text is saved to every file unless you change it.** |

**Input mapping** - which Art-Net channels are packed into each frame.

| Field | Meaning |
|---|---|
| Start Universe | Art-Net universe of the first chip, as shown in the *Universes received* list (default `1`). |
| Start channel | First DMX channel of the first chip, 1..512. |
| Channels per chip | 3 for RGB, 4 for RGBW, or any value from 1 to 512. All channels of a chip are stored. |
| Chips per universe | How many chips fit into one universe. `0` = automatic (`512 // channels per chip`). |

**Post-processing (off by default)**

| Option | Meaning |
|---|---|
| Skip first frames (count) | Number of recorded frames to drop at the start. |
| Loop blend (frames) | Number of frames used to cross-fade the end of the recording into its beginning. |

The two entries are active only when their check box is ticked. Extra frames are recorded automatically.

**File path** - where the JSON file is saved (default `sequence_1.json` in the current folder). **Browse...** opens a
standard *Save as* dialog.

### Right column

- **IP** and **UDP port** of the server (default `127.0.0.1` and `6454`).
- **Start Server / Stop Server** - the button turns red while the server is running.
- **Statistics block** - live status (see below).
- **Start Record Input Data / Stop Record Input Data** - enabled only while the server is running; red while recording.
- **Start recording on first packet** (check box under the record button, on by default).

### Statistics block

```
Server: RUNNING  127.0.0.1:6454
Packets: 1234  (60.0 pkt/s)  last: 0.01 s ago
Universes received:
  U1      60.0 pkt/s   512 ch

REC  75/180 frames  (2.47 s)
missing: 0   late: 0

Saved: D:\json_folder\sequence_1.json
180 frames, 331 KB

Errors:
[12:31:07] ...
```

- `Packets` - total ArtDMX/other packets received since the server started and the rate over the last second.
- `Universes received` - the real Art-Net universe numbers, their packet rate and the number of channels they carry.
- `REC n/N` - recorded frames out of the planned total (planned total = skip + exported frames + blend). A line with the
  plan is shown when post-processing is on.
- `missing` - frames in which at least one universe from *Input mapping* had not been received yet (its channels are 0).
- `late` - frames that were sampled more than 1.5 frame periods late (the computer was too busy).
- `Errors` - the last 6 errors with time stamps, shown in red. The list is cleared when a server or a recording starts
  successfully.

---

## Recording step by step

1. **Start Server.** If the port is busy, an error appears in the statistics block (see *Troubleshooting*).
2. **Start the source** and watch the `Universes received` list. Set *Start Universe* to a universe that really arrives.
3. **Fill in the JSON fields** and set *Sequence Duration* and *Sequence Fps*.
4. *(Optional)* tick **Skip first frames** and/or **Loop blend**.
5. Check the **file path**.
6. Press **Start Record Input Data.**
   - With *Start recording on first packet* the clock starts when the first packet of a mapped universe arrives.
   - If a file with this name already exists you are asked what to do (see below).
7. The recording **stops automatically** when the planned number of frames is reached; with duration `0` press
   **Stop Record Input Data**.
8. The file is **saved automatically** and the statistics block shows the path, the frame count and the file size.

---

## Output JSON format

```json
{
  "model_name": "Crystal Links Model 1",
  "model_chips_tag": "crystal_links_model_1_chip",
  "model_chips_count": "104",
  "sequence_name": "Random Colored Loop Flashing",
  "sequence_version": "1.0",
  "sequence_duration_sec": "6.0",
  "sequence_fps": "30",
  "sequence_frame_count": "180",
  "sequence_frame_stride": "312",
  "developer_notes": "Notes by developer or smb who worked...",
  "sequence": {
    "data": [
      0.276, 0.93, 0.603, 0.227, 0.872, 0.888, ...,
      ...
    ]
  }
}
```

- All header values are **strings**. `sequence_duration_sec` is calculated as `frames / fps`.
- `sequence.data` is a **flat array of floats** in the range 0..1 (DMX byte / 255, rounded to 3 decimals). The file has
  **one frame per line**, UTF-8 encoded, with `\n` line endings.
- Layout of the array (all indexes are 0-based):

```
value = data[ frame * stride + chip * channels_per_chip + channel ]
stride = chips * channels_per_chip          (sequence_frame_stride)
```

For 104 RGB chips the stride is 312, so the colour of chip `c` in frame `f` is
`R = data[f*312 + c*3]`, `G = data[f*312 + c*3 + 1]`, `B = data[f*312 + c*3 + 2]`.

---

## How it works (algorithms)

### 1. Receiving Art-Net

A UDP socket is bound to the chosen IP and port and read in a background thread with a 0.2 s timeout, so the server can
be stopped at any moment. Every packet is parsed as **ArtDMX**:

| Bytes | Content |
|---|---|
| 0-7 | ID `Art-Net\0` |
| 8-9 | OpCode `0x5000` (low byte first) |
| 10-11 | Protocol version |
| 12 | Sequence |
| 13 | Physical |
| 14 | SubUni (low 8 bits of the universe) |
| 15 | Net (7 bits) |
| 16-17 | Data length (high byte first) |
| 18 and up | DMX data |

`port-address = (Net << 8) | SubUni` (0-based; this is the number shown as `U<n>` in the statistics). The data is padded
with zeros to 512 channels and stored as the **latest value of that universe**. Packets that are not ArtDMX are counted
as ignored. The sequence number is not used to reorder packets. The application does not answer ArtPoll, so the source
must send to the IP address and port directly.

### 2. Channel mapping

The chips are laid out one after another over universes by `build_spans`:

- a universe holds `512 // channels_per_chip` chips, or fewer if *Chips per universe* is set;
- the first universe starts at *Start channel*; every following universe starts at channel 1;
- the result is a list of spans *(universe, first channel, number of channels, offset in the frame)*, so one frame is
  assembled by copying a slice of the latest data of each universe.

Chips never straddle a universe boundary.

### 3. Fixed frame rate sampling

The recorder thread takes a snapshot of the latest data at the moments `t0 + k / fps` (`k` = 0, 1, 2, ...) measured with
`time.perf_counter()`. Because every moment is computed from `t0`, errors do not accumulate (no drift). The thread sleeps
until about 2 ms before the moment and then waits actively for better accuracy.

- This is **sample and hold**: the newest received value is used, with no interpolation. A 60 Hz source recorded at
  30 fps simply skips every second packet. The time resolution is therefore limited by the source packet rate.
- A frame is counted as `late` if it is taken more than 1.5 periods after its planned moment, and as `missing` if a
  mapped universe has not been received yet.
- With *Start recording on first packet* the clock `t0` is set when the first packet of a mapped universe arrives.

### 4. Number of frames

- Duration `> 0`: exported frames `final = round(duration x fps)`. The recorder collects
  `skip + final + blend` frames and stops.
- Duration `= 0`: you stop manually, and `final = recorded - skip - blend`. If there are not enough frames, nothing is
  exported and an error is shown (the raw recording is kept in memory).

### 5. Post-processing

**Skip first frames.** The first `skip` frames are removed.

**Loop blend.** After the `final` exported frames, `blend` extra frames are recorded (the continuation of the animation).
The first `blend` exported frames are replaced by a linear cross-fade from that continuation to the original frames:

```
out[i] = round( tail[i] * (1 - i/N) + head[i] * (i/N) ),   i = 0 .. N-1,   N = blend
```

where `tail[i]` is the recorded frame number `final + i` (after skipping) and `head[i]` is the original frame `i`.
The first exported frame therefore equals the true continuation of the last one, so the step from the last frame to
the first one is as small as any other step, and the sequence loops without a visible jump. The remaining frames are
not changed. The blend is calculated on the 8-bit values and `blend` must not exceed the number of exported frames.

> If your animation is truly periodic and its period equals the exported duration, any window of that length loops
> seamlessly and you do not need *Loop blend*. For non-periodic animations (for example noise) use it.

### 6. Export

Every byte 0..255 is converted with a lookup table to the text of `round(byte / 255, 3)` (`0` becomes `0.0`, `255`
becomes `1.0`). The JSON text is assembled by hand to keep one frame per line, and the file is written as UTF-8.

---

## Protection of recordings and files

- **Starting a recording when the file already exists** (for example the previous recording was just saved there) opens
  a dialog with three choices: **Overwrite existing file**, **Save as new file...**, **Cancel**.
  *Save as new file...* opens a *Save as* dialog with a suggested free name (`sequence_1.json` becomes
  `sequence_2.json`; a name without a trailing number gets `_1`). Cancelling that dialog returns to the three choices;
  **Cancel** does not start the recording. The old file is never touched unless you choose to overwrite it.
- If the path is changed during a recording to a file that exists, the same dialog appears when the recording ends.
- **A recording that could not be saved** (the save was postponed, the disk write failed, or post-processing failed) is
  kept in memory, and the reason is shown in the *Errors* list. If post-processing fails, the raw recording is kept.
  The next **Start Record** offers *Save it now / Discard it / Cancel*, and so does closing the window.
- Pressing **Cancel** or **Don't save yet** in the dialogs never deletes anything.

---

## Test source: packets_tester

`packets_tester.py` sends ArtDMX packets with a running rainbow (loop period 6 s) at 60 packets per second.

```
python packets_tester.py [host] [port] [chips] [universe]
run_packets_tester.bat   [host] [port] [chips] [universe]
```

Defaults: `127.0.0.1 6454 104 1` (1 to 170 RGB chips in a single universe). The console log shows:

```
[12:00:00] Target 127.0.0.1:6454 | universe 1 | 104 chips (312 channels) | 60 packets/s | loop 6.0 s
[12:00:01] sent 60 packets (60.0 pkt/s) | total 60 | send errors 0 (+0)
```

Invalid arguments, an unresolvable host and send errors are reported in the log (new kinds of send errors immediately,
the running count every second). `Ctrl+C` stops it and prints the totals.

> UDP is connectionless: the tester cannot know whether anybody is listening. `send errors 0` does not prove that the
> packets are received - check the statistics block of the recorder.

---

## Troubleshooting and notes

| Problem | What to check |
|---|---|
| `Could not start the server ... Address already in use` | Another program (for example Unreal Engine) already listens on this IP and port. Choose another IP/port here and point the source to it. |
| Nothing arrives (`nothing received yet`) | IP and port, the destination set in the source, and the Windows firewall prompt on the first start. |
| `missing` is greater than 0 | *Start Universe* or the other mapping values do not match the universes in the `Universes received` list. |
| `late` is greater than 0 | The computer was too busy. A few late frames are harmless; many mean that the timing is poor. |
| The window does not appear with `run.bat` | Run `run.bat debug` and read the message. |

- **Universe numbering.** Art-Net universes are 0-based, while some programs show them from 1. Always use the numbers in
  the `Universes received` list. The default *Start Universe* is 1, to match the bundled tester.
- Only **ArtDMX** is supported (no sACN). Values have **8-bit** resolution; 16-bit channel pairs are stored as two
  separate values.
- **Developer Notes** has a default text that is written into every file unless you edit it.
- Errors are shown in the statistics block, not in pop-up windows. Pop-up dialogs are used only for choices about files
  and unsaved recordings.
- The recorder and the tester were verified with each other on a virtual display (core logic and the main window flows).
  The `.bat` files and the native Windows *Save as* dialog were not tested on Windows.

---

## Building an .exe

```
pip install pyinstaller
pyinstaller --onefile --noconsole main.py
```

The executable appears in the `dist` folder.
