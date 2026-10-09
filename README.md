# HGLIVE — Douyin livestream recorder

Watches Douyin live rooms (`live.douyin.com`) and records the stream to disk with
ffmpeg. Ships a PySide6 GUI for watching many rooms at once, plus a CLI that runs on
the same engine.

- Paste a room link and the tool fetches room info and the available qualities.
- **Watch mode**: starts recording as soon as the streamer goes live, and resumes
  automatically if the stream drops mid-session.
- Records several rooms in parallel, with a queue once the concurrency limit is hit.
- No login, no `a_bogus` signing: only a `ttwid` cookie is needed, and the tool
  obtains one by itself.
- Optional **network lanes** (a proxy list or NordVPN WireGuard) so each room goes
  out through its own IP, which keeps the CDN from throttling you when recording
  many rooms.

> The GUI and log messages are in Vietnamese. Button names below are given as they
> appear in the app, with an English gloss.

## Requirements

- Python 3.9+
- ffmpeg on `PATH` (Windows: `winget install Gyan.FFmpeg`)

```bash
pip install -r requirements.txt
```

## Running

**GUI:** double-click `HGLIVE.bat`, or:

```bash
python hglive.py
```

**CLI:**

```bash
python hglive.py "https://live.douyin.com/578280456923" --list    # show room + qualities, don't record
python hglive.py 578280456923                                      # record until the stream ends
python hglive.py 578280456923 -q ld -t 600 --ext mp4               # record 10 minutes at "ld", as mp4
python hglive.py 578280456923 --wait                               # wait for the room to go live, then record
python hglive.py 578280456923 --url-only                           # just print the stream URL (for VLC)
python hglive.py 578280456923 --lanes "http://a:8080,http://b:8080" --no-direct
python hglive.py 578280456923 --lanes nordvpn --lane-country JP
```

`link` accepts `live.douyin.com/<rid>`, a short `v.douyin.com/...` link, or a bare
`web_rid`.

### CLI options

| Option | Meaning |
|---|---|
| `--list` | only list room info and qualities |
| `--url-only` | only print the selected stream URL |
| `-q`, `--quality` | `best` · `worst` · `origin` / `uhd` / `hd` / `sd` / `ld` / `md` · `ao` (audio only) |
| `--proto` | `flv` (default, low latency) · `hls` (more tolerant of flaky networks) |
| `--ext` | `ts` (recommended, still playable after a crash or power loss) · `mp4` · `flv` |
| `-t`, `--time` | record for N seconds (default: until the stream ends) |
| `--segment N` | split the file every N minutes |
| `--out DIR` | output folder |
| `--cookie FILE` | cookie file (Netscape `cookies.txt` or JSON export) — usually not needed |
| `--proxy URL` | a single proxy for everything |
| `--lanes MODE` | `off` · `nordvpn` · comma-separated proxy list · `.txt` file with one proxy per line |
| `--lane-country` | country for NordVPN lanes, e.g. `JP` |
| `--no-direct` | don't use the machine's own IP as a lane |
| `--wait` | wait until the room goes live |
| `--every N` | polling interval while waiting (seconds, minimum 15) |
| `--no-resume` | stop when the stream drops instead of resuming |
| `--no-meta` | don't save `.info.json` / cover image |
| `-v` | also print ffmpeg output |

## Using the GUI

1. Paste a link into the top box → **Dò phòng** (look up room).
2. **Ghi ngay** (record now) if the room is live, or **Canh sóng** (watch) to wait
   for it to go live and record automatically.
3. Each row has its own buttons:
   - **Tạm dừng** (pause, `Space`): stops recording and closes the file cleanly,
     *but keeps watching*.
   - **Ghi tiếp** (resume): records into a new file; the duration/size columns keep
     adding up.
   - **Dừng hẳn** (stop): stops recording and stops watching.
4. The three buttons in the footer apply to the selected rows, or to all rows when
   nothing is selected.
5. **Cài đặt** (settings): output folder, quality, protocol, container, file
   splitting, parallel recordings, filename template, network lanes.
6. **Làn mạng** (network lanes): shows which lane carries which room, and rotates the
   IP of an idle lane.

A room shown as **`xếp hàng`** (queued, yellow) is waiting for a free recording slot
because the parallel limit has been reached — it is not stuck.

On restart the app resumes watching the same rooms as last time.

## Configuration

Stored in `hglive.config.json` next to the tool (created on first run, already in
`.gitignore`). See [`hglive.config.example.json`](hglive.config.example.json).

| Key | Default | Meaning |
|---|---|---|
| `out_dir` | `recordings/` next to the tool | where files are saved |
| `quality` / `proto` / `ext` | `best` / `flv` / `ts` | same as the CLI options |
| `auto_record` | `true` | start recording as soon as a room goes live |
| `auto_resume` | `true` | resume if the stream drops while the room is still live |
| `poll_every` | `45` | polling interval while waiting (seconds; polling too often risks Douyin's risk-control) |
| `segment_min` | `0` | split every N minutes (0 = one continuous file) |
| `max_hours` | `0` | cap per recording (0 = unlimited) |
| `max_parallel` | `3` | number of rooms recorded at the same time |
| `name_tpl` | `{nick}_{date}_{time}_{quality}` | filename template; available keys: `{nick}` `{rid}` `{room}` `{title}` `{date}` `{time}` `{quality}` `{res}` |
| `room_subdir` | `true` | one subfolder per room |
| `save_meta` | `true` | also save `.info.json` + cover image |
| `proxy_mode` | `off` | `off` · `list` · `nordvpn` — see the [technical notes](docs/TECHNICAL.md#làn-mạng-proxy) |

### NordVPN (optional)

`nordvpn` mode only needs a NordVPN **access token** — the NordVPN app is not
required. The tool downloads `wireproxy` itself if it isn't found. The token is
looked up in this order: the Token field in Settings → the Token file field → the
`NORDVPN_TOKEN` environment variable → a `.nordvpn_token` or `.env` file next to the
tool. The token is never written to the log.

## Building an .exe

```bash
pip install pyinstaller
build_live.bat
```

Produces `dist\HGLIVE.exe` (onefile, no console). The exe keeps its config and
`recordings/` next to the `.exe` file.

## Layout

```
hglive.py            entry point when running from source
hglive_app.py        PyInstaller entry point (named differently so it doesn't shadow the hglive/ package)
HGLIVE.bat           double-click launcher
build_live.bat       builds the exe
hglive/
  live.py            room lookup, stream list parsing (network only, stateless)
  recorder.py        state machine: watch → record → drop → resume
  lanes.py           network lanes: assign rooms to exits, rotate IPs when idle
  nordvpn.py         WireGuard proxies via wireproxy
  gui.py             PySide6 GUI
  cli.py             command line
  config.py          reads/writes hglive.config.json
  cookies.py         cookie loading (Netscape / JSON / header string)
  util.py            helpers: safe filenames, formatting, ffmpeg lookup
docs/TECHNICAL.md    endpoint, where the streams live, known pitfalls, lane design (Vietnamese)
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `không tìm thấy ffmpeg trong PATH` (ffmpeg not found) | install ffmpeg and reopen the terminal so `PATH` is refreshed |
| `status = 4` / room has no stream | the streamer is offline — use **Canh sóng** (watch) |
| `服务器打瞌睡了` (10001) | Douyin's server is flaky for a moment; the tool retries |
| Recording many rooms, each stream only a few dozen KB/s | the CDN throttles per IP — enable network lanes or lower the quality |

Technical details: [docs/TECHNICAL.md](docs/TECHNICAL.md) (Vietnamese).
