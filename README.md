# udi-sonos-jishi-poly

MIT License — Polyglot v3 NodeServer for Sonos, using [node-sonos-http-api](https://github.com/jishi/node-sonos-http-api) (Jishi) as the backend.

## Why not the other Sonos plugins?

**udi-sonos-poly** uses the `soco` Python library for direct UPnP communication. It crashes on stereo pairs (L/R satellites return empty UPnP responses), leaks threads, and requires multicast discovery — all of which fail across VLANs.

**ST-Sonos** uses the `sonos-discovery` Node.js library which relies on UPnP push subscriptions: speakers must initiate TCP callbacks to the PG3x server. If your Sonos speakers and PG3x server are on different VLANs and inbound connections from speakers are blocked, state updates never arrive and ISY goes stale.

**This plugin** polls Jishi's `/zones` endpoint every 10 seconds. One HTTP call returns the current state of every zone simultaneously. No UPnP subscriptions, no multicast, no inbound connections from speakers required — just outbound HTTP from PG3x to your Jishi host. State in ISY is always accurate within one poll cycle.

## Why Jishi?

- **Works across VLANs** — only needs outbound HTTP from PG3x to Jishi (port 5005)
- **Stereo pairs handled natively** — Jishi merges L/R pairs and surround sets into single zones
- **Real-time state detection** — polling catches state changes initiated from the Sonos app, Alexa, or any other source within 10 seconds
- **Dynamic ISY UI** — favorites, playlists, TTS phrases, and zone names are fetched from Jishi and shown by their real names in ISY Admin Console dropdowns
- **TTS built in** — Jishi handles Google TTS; the Jishi host serves audio directly to speakers on the Sonos VLAN
- **One call covers everything** — `/zones` returns all zone states in a single request

## Requirements

- [node-sonos-http-api](https://github.com/jishi/node-sonos-http-api) running and reachable from your PG3x server
- Jishi host must be on the same network as your Sonos speakers (or otherwise able to reach them)
- Python 3.9+ with `requests` and `udi_interface`

## Installation

Install via the Polyglot v3 local store (requires UDI developer account), or clone directly on your eisy:

```bash
cd /home/admin
git clone https://github.com/csteenwyk/udi-sonos-jishi-poly.git
cd udi-sonos-jishi-poly
pip3 install -r requirements.txt
chmod +x sonos-poly.py
```

## Configuration

Set these in the NodeServer's **Custom Parameters** in the PG3x UI:

| Key | Example | Description |
|-----|---------|-------------|
| `jishi_url` | `http://192.168.1.100:5005` | URL of your Jishi server **(required)** |
| `tts_1` … `tts_10` | `Dinner is ready` | TTS phrases for the SAY / SAY ALL commands |

Favorites and playlists are **automatically fetched from Jishi** — no manual configuration needed. They appear by name in the ISY UI and refresh on every long poll (default: every 2 minutes).

## Nodes

### Controller
| Command | Description |
|---------|-------------|
| Re-Discover | Re-query Jishi, create any new zone nodes |
| Pause All | Pause every zone |
| Resume All | Resume all previously playing zones |
| Ungroup All | Break every group — all zones become independent |
| Party Mode | Join all zones to the first zone |
| Say All | Speak a TTS phrase on every speaker simultaneously |

### Speaker (one per Jishi zone)

**Drivers:**

| Driver | Description |
|--------|-------------|
| ST | Playback state: Stopped / Playing / Transitioning / Paused |
| SVOL | Player volume (0–100) |
| GV1 | Group volume (0–100) |
| GV2 | Bass (-10 to 10) |
| GV3 | Treble (-10 to 10) |
| GV4 | Mute |
| GV5 | Group Mute |
| GV6 | Shuffle |
| GV7 | Repeat (None / One / All) |
| GV8 | Crossfade |
| GV9 | Loudness |
| GV10 | Nightmode |
| GV11 | Speech Enhancement |
| GV12 | Members in group |

**Commands:**

| Command | Description |
|---------|-------------|
| Play / Pause / Stop | Transport control |
| Next / Previous | Skip tracks |
| Set Volume / Up / Down | Volume control |
| Set Bass / Treble | EQ control |
| Mute / Unmute | Mute toggle |
| Shuffle On / Off | Shuffle control |
| Set Repeat | None / One / All |
| Toggle Crossfade | Crossfade on/off |
| Play Favorite | Pick by name from ISY dropdown |
| Play Playlist | Pick by name from ISY dropdown |
| Say (TTS) | Speak a configured phrase on this speaker |
| Sleep Timer | Off / 15 / 30 / 45 / 60 / 90 min |
| Join Zone | Join this speaker's audio to any other zone (by name) |
| Leave Group | Remove this speaker from its current group |
| Party Mode | Join all other zones to this speaker |

## Polling Behavior

This plugin uses two poll intervals, both configurable in the PG3x NodeServer settings.

### Short Poll (default: 10 seconds) — State Sync

On every short poll the plugin calls Jishi's `/zones` endpoint once. That single response contains the current state of every zone simultaneously. Each speaker node's drivers are updated:

- **ST** — playback state (Stopped / Playing / Transitioning / Paused)
- **SVOL / GV1** — player volume and group volume
- **GV2 / GV3** — bass and treble
- **GV4 / GV5** — player mute and group mute
- **GV6 / GV7 / GV8** — shuffle, repeat, crossfade
- **GV9 / GV10 / GV11** — loudness, nightmode, speech enhancement
- **GV12** — members in group

ISY program conditions that test playback state (e.g. `ST is Playing`) reflect reality within one poll cycle.

### Long Poll (default: 120 seconds) — Content Refresh

On every long poll the plugin fetches the current favorites and playlists from Jishi. If either list has changed since the last fetch, it rewrites the profile NLS and editor files and calls `poly.updateProfile()` to push the updated dropdown options to ISY. This keeps the **Play Favorite** and **Play Playlist** dropdowns in ISY Admin Console accurate if you add or rename content in the Sonos app.

TTS phrases come from Custom Parameters (`tts_1`…`tts_10`) and are refreshed automatically whenever parameters change — no poll needed.

### Manual Refresh

The **Refresh Content** button on the controller node triggers an immediate content refresh outside the long poll cycle. Use it any time you add favorites or playlists in the Sonos app and want the ISY dropdowns updated right away without waiting up to two minutes.

## ISY Programs

Because playback state is a proper driver (ST), you can trigger ISY programs directly:

```
If 'Living Room' ST is Playing
Then ...

If 'Office' ST is not Playing
Then ...
```

State updates within ~10 seconds of any change — whether initiated from the Sonos app, Alexa, another plugin, or an ISY program.

## Network Notes

```
ISY / PG3x server  →  (outbound HTTP port 5005)  →  Jishi host  →  (UPnP port 1400)  →  Sonos speakers
```

PG3x only needs outbound HTTP access to the Jishi host. The Jishi host needs to be on the same VLAN as your speakers (or otherwise reachable via UPnP). No inbound connections from speakers to PG3x are required.

For TTS, Jishi fetches audio from Google and serves it locally. Speakers retrieve the audio directly from the Jishi host — no internet access required from speakers at playback time.
