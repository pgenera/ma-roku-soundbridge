# Roku SoundBridge Provider for Music Assistant

Player provider for **Roku** and **Pinnacle SoundBridge** network music players (M500, M1000, M1001, M2000, R1000 / SoundBridge Radio, M400 HomeMusic) over the TCP **Roku Control Protocol (RCP, port 5555)**.

Zero external Python dependencies (`requirements: []` — pure `asyncio` TCP).

## Features

- **Optional opt-in provider**: Nothing runs until you install and enable **Roku SoundBridge** in Music Assistant.
- **Multiple SoundBridges via dual discovery**:
  - **Automatic Bonjour / mDNS discovery** (`_roku-rcp._tcp.local.`) on the local subnet.
  - **Manual host list** (`host` or `host:port`, default port `5555`) for SoundBridges across VLANs/subnets where multicast mDNS does not reach, automatically deduplicated against mDNS-discovered devices.
- **Fast MP3 + ICY streaming by default**:
  - Defaults new SoundBridge players to **MP3** with **basic ICY** in-stream metadata (~3s click-to-audio, reliable continuous flow streaming, live track/artist updates on VFD line 1 and shared album name for single tracks or full-album queues—or `"Music Assistant"` for mixed-album flows—on line 2).
  - Also supports uncompressed **WAV** via player audio settings, automatically switching to per-track lossless `/single/` playback with end-of-track enqueue handoff and native RCP `SetWorkingSongInfo` metadata (`title`, `artist`, `album`, and `trackLength` per track).
  - In continuous flow mode, sends total duration (`trackLength`) only for single-item queues so multi-track flows never display a stale duration (while per-track `/single/` WAV sends each track's duration).
- **Full transport & power control**: Standby wake (`SetPowerState on no` with automatic `Internet Radio` source connect) and standby power-off, volume (`0–100`), mute, play, pause, and stop, with a buffering grace window that suppresses false `stop`/`connecting` transitions while the device fills its initial audio buffer.

## Installation (Music Assistant Marketplace)

1. In Music Assistant (as an administrator), open **Settings → Marketplace** and enable the **Community Catalog**.
2. Find **Roku SoundBridge** and click **Install**, then **Add / Enable** the provider.
3. SoundBridges on the same LAN appear automatically via mDNS (`_roku-rcp._tcp.local.`).
4. *(Optional)* To add SoundBridges on other subnets/VLANs, open the **Roku SoundBridge** provider settings and add one `host` or `host:port` per line under **Manual SoundBridge Hosts**.

## Notes & Known Hardware Limitations

- **Supported codecs**: SoundBridge firmware does not decode FLAC natively and rejects WAV streams carrying ICY headers or `remoteStream 1`, while continuous `/flow/` WAV triggers probe reconnects. **MP3 + Basic ICY** is the default for gapless continuous flow mode with live VFD track updates; switching Output Codec to **WAV** (or **Lossless Uncompressed** on newer Music Assistant servers) switches to per-track lossless `/single/` playback with native VFD track, artist, album, and progress display (and automatically disables ICY framing if flow mode is forced).
- **IPv4 only**: SoundBridge network hardware is IPv4-only.
- **Multi-track flow duration**: In continuous queue flow mode, SoundBridge firmware locks `trackLength` when `QueueAndPlayOne` starts; total track length is therefore only sent for single-item queues in flow mode so multi-track flows never display a stale duration, whereas per-track `/single/` WAV playback sends each track's duration.

## Local Development

Requires Python 3.14+, `ffmpeg` 6.1+, and [`uv`](https://docs.astral.sh/uv/):

```bash
uv sync
uv run pre-commit install

# Run lint, type checks, manifest/icon/strings checks, and unit + integration tests
uv run ruff check .
uv run mypy
uv run pytest

# Start a local dev Music Assistant server loading this provider (UI on http://localhost:8095)
./scripts/run.sh
```

To run the optional live-hardware end-to-end test against a running Music Assistant instance and physical SoundBridge:

```bash
MA_TOKEN="<long-lived-token>" MA_PLAYER_ID="soundbridge_192_168_1_50" uv run pytest tests/test_e2e_playback.py -v
```

## License

Apache-2.0
