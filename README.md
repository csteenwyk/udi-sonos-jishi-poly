# udi-sonos-jishi-poly

Polyglot v3 NodeServer for Sonos, using [node-sonos-http-api](https://github.com/jishi/node-sonos-http-api) (Jishi) as the backend.

## Why Jishi?

- Works across VLANs — no UPnP subscriptions, no multicast discovery needed
- Stereo pairs (L/R) and surround sets handled as a single zone automatically
- TTS (Google text-to-speech) built in
- One `/zones` HTTP call updates all speakers simultaneously

## Requirements

- [node-sonos-http-api](https://github.com/jishi/node-sonos-http-api) running and reachable from your PG3 server
- Python 3 + `requests` + `udi_interface`

## Installation

Install via the Polyglot v3 store, or manually:

```bash
cd /path/to/pg3/nodeservers
git clone https://github.com/csteenwyk/udi-sonos-jishi-poly
cd udi-sonos-jishi-poly
pip3 install -r requirements.txt
```

## Configuration

Set these in the NodeServer's **Custom Parameters** in the PG3 UI:

| Key | Example | Description |
|-----|---------|-------------|
| `jishi_url` | `http://zeus:5005` | URL of your Jishi server |
| `favorite_1` … `favorite_10` | `96.9 \| 97 LAV-FM` | Sonos favorite names (exact match) |
| `playlist_1` … `playlist_10` | `White Noise` | Sonos playlist names (exact match) |
| `tts_1` … `tts_10` | `Dinner is ready` | TTS phrases for the SAY command |

Favorite and playlist names must match exactly what appears in your Sonos app.
If no favorites/playlists are configured, the nodeserver will auto-populate from Jishi on long poll.

## Nodes

### Controller
- **Pause All** — pauses every zone
- **Resume All** — resumes all previously playing zones
- **Re-Discover** — re-queries Jishi and creates any new zone nodes

### Speaker (one per Jishi zone)

**Drivers:**

| Driver | Description |
|--------|-------------|
| ST | Playback state (Stopped / Playing / Paused / Transitioning) |
| SVOL | Volume (0–100) |
| GV1 | Bass (-10 to 10) |
| GV2 | Treble (-10 to 10) |
| GV3 | Mute |
| GV4 | Shuffle |
| GV5 | Repeat (None / One / All) |
| GV6 | Crossfade |
| GV7 | Loudness |
| GV8 | Track Title |
| GV9 | Artist |
| GV10 | Album / Station |

**Commands:** Play, Pause, Stop, Next, Previous, Set Volume, Volume Up/Down, Set Bass, Set Treble, Mute, Unmute, Shuffle On/Off, Set Repeat, Toggle Crossfade, Play Favorite (slot 1-10), Play Playlist (slot 1-10), Say/TTS (slot 1-10), Sleep Timer

## Network Notes

The PG3 server only needs outbound HTTP access to the Jishi host (default port 5005).
Jishi must be on a network that can reach your Sonos speakers (typically the same VLAN as the speakers).
