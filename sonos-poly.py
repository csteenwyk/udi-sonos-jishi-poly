#!/usr/bin/env python3
# MIT License — Copyright (c) 2026 csteenwyk
# https://github.com/csteenwyk/udi-sonos-jishi-poly/blob/main/LICENSE
"""
Sonos Polyglot v3 NodeServer - Jishi backend
Polls the node-sonos-http-api (Jishi) server for all zone state.
No UPnP subscriptions required — works across VLANs.

Favorites and playlists are auto-fetched from Jishi and surfaced as
named options in the ISY Admin Console via dynamic profile updates.
TTS phrases are user-configured via Custom Parameters (tts_1..tts_10).
"""

import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import requests
import udi_interface

LOGGER = udi_interface.LOGGER

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_PROFILE_DIR = os.path.join(_PLUGIN_DIR, 'profile')

# ---------------------------------------------------------------------------
# Static NLS content
# ---------------------------------------------------------------------------

_STATIC_NLS = """\
# Node Server Names
ND-sonos_controller-NAME = Sonos Controller
ND-sonos_speaker-NAME = Sonos Speaker

# Controller Drivers
ST-sonos_controller-ST-NAME = Status

# Controller Commands
CMD-sonos_controller-DISCOVER-NAME = Re-Discover
CMD-sonos_controller-PAUSE_ALL-NAME = Pause All
CMD-sonos_controller-RESUME_ALL-NAME = Resume All
CMD-sonos_controller-UNGROUP_ALL-NAME = Ungroup All
CMD-sonos_controller-PARTY-NAME = Party Mode
CMD-sonos_controller-SAY_ALL-NAME = Say All
CMD-sonos_controller-CLIP_ALL-NAME = Play Clip All
CMD-sonos_controller-REFRESH_CONTENT-NAME = Refresh Content

# Speaker Drivers
ST-sonos_speaker-ST-NAME = Playback State
ST-sonos_speaker-SVOL-NAME = Volume
ST-sonos_speaker-GV1-NAME = Group Volume
ST-sonos_speaker-GV2-NAME = Bass
ST-sonos_speaker-GV3-NAME = Treble
ST-sonos_speaker-GV4-NAME = Mute
ST-sonos_speaker-GV5-NAME = Group Mute
ST-sonos_speaker-GV6-NAME = Shuffle
ST-sonos_speaker-GV7-NAME = Repeat
ST-sonos_speaker-GV8-NAME = Crossfade
ST-sonos_speaker-GV9-NAME = Loudness
ST-sonos_speaker-GV10-NAME = Nightmode
ST-sonos_speaker-GV11-NAME = Speech Enhancement
ST-sonos_speaker-GV12-NAME = Members
ST-sonos_speaker-GV13-NAME = Alert Volume

# Speaker Commands
CMD-sonos_speaker-PLAY-NAME = Play
CMD-sonos_speaker-PAUSE-NAME = Pause
CMD-sonos_speaker-NEXT-NAME = Next Track
CMD-sonos_speaker-PREV-NAME = Previous Track
CMD-sonos_speaker-MUTE-NAME = Mute
CMD-sonos_speaker-UNMUTE-NAME = Unmute
CMD-sonos_speaker-SET_VOL-NAME = Set Volume
CMD-sonos_speaker-SET_GRP_VOL-NAME = Set Group Volume
CMD-sonos_speaker-SET_BASS-NAME = Set Bass
CMD-sonos_speaker-SET_TREBLE-NAME = Set Treble
CMD-sonos_speaker-SET_MUTE-NAME = Set Mute
CMD-sonos_speaker-SET_GRP_MUTE-NAME = Set Group Mute
CMD-sonos_speaker-SHUFFLE-NAME = Shuffle
CMD-sonos_speaker-REPEAT-NAME = Set Repeat
CMD-sonos_speaker-CROSSFADE-NAME = Crossfade
CMD-sonos_speaker-LOUDNESS-NAME = Loudness
CMD-sonos_speaker-NIGHTMODE-NAME = Night Mode
CMD-sonos_speaker-SPEECH_ENH-NAME = Speech Enhancement
CMD-sonos_speaker-PLAY_FAVORITE-NAME = Play Favorite
CMD-sonos_speaker-PLAY_PLAYLIST-NAME = Play Playlist
CMD-sonos_speaker-SAY-NAME = Say (TTS)
CMD-sonos_speaker-CLIP-NAME = Play Clip
CMDP-phrase-NAME = Phrase
CMDP-E_TTS-phrase-NAME = Phrase
CMDP-clip-NAME = Clip
CMDP-E_CLIP-clip-NAME = Clip
CMDP-vol-NAME = Volume
CMDP-E_VOL-vol-NAME = Volume
CMD-sonos_speaker-SLEEP-NAME = Sleep Timer
CMD-sonos_speaker-JOIN-NAME = Join Zone
CMD-sonos_speaker-LEAVE-NAME = Leave Group
CMD-sonos_speaker-PARTY-NAME = Party Mode

# Playback State index values (UOM 25)
CUST_PB-0 = Stopped
CUST_PB-1 = Playing
CUST_PB-2 = Transitioning
CUST_PB-3 = Paused

# Repeat mode index values (UOM 25)
CUST_REPEAT-0 = None
CUST_REPEAT-1 = One
CUST_REPEAT-2 = All

# Sleep timer index values (UOM 25)
CUST_SLEEP-0 = Off
CUST_SLEEP-15 = 15 min
CUST_SLEEP-30 = 30 min
CUST_SLEEP-45 = 45 min
CUST_SLEEP-60 = 60 min
CUST_SLEEP-90 = 90 min
"""

_STATIC_EDITORS = """\
  <editor id="E_VOL">
    <range uom="51" min="0" max="100" prec="0"/>
  </editor>
  <editor id="E_EQ">
    <range uom="56" min="-10" max="10" step="1"/>
  </editor>
  <editor id="E_PLAYBACK">
    <range uom="25" subset="0,1,2,3" nls="CUST_PB"/>
  </editor>
  <editor id="E_BOOL">
    <range uom="2" subset="0,1"/>
  </editor>
  <editor id="E_REPEAT">
    <range uom="25" subset="0,1,2" nls="CUST_REPEAT"/>
  </editor>
  <editor id="E_SLEEP">
    <range uom="25" subset="0,15,30,45,60,90" nls="CUST_SLEEP"/>
  </editor>
  <editor id="E_STATUS">
    <range uom="2" subset="0,1"/>
  </editor>\
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PLAYBACK_MAP = {
    'STOPPED':         0,
    'PLAYING':         1,
    'TRANSITIONING':   2,
    'PAUSED_PLAYBACK': 3,
}

REPEAT_MAP = {'none': 0, 'one': 1, 'all': 2}


def _jishi_get(base_url, path, timeout=5):
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        LOGGER.warning(f"Jishi {url} failed: {e}")
        return None


def _jishi_cmd(base_url, path, timeout=5):
    return _jishi_get(base_url, path, timeout) is not None


def _enc(s):
    return quote(s, safe='')


def _zone_address(zone_name):
    addr = re.sub(r'[^a-z0-9]', '', zone_name.lower())
    return addr[:14]


def _zone_room_name(zone):
    """Extract room name from a Jishi zone dict (coordinator or bare zone)."""
    return zone.get('coordinator', {}).get('roomName') or zone.get('roomName', '')


def _cmd_val(command):
    """Extract the integer parameter value from an ISY command dict."""
    return int(command.get('value', 0))


def _cmd_param(command, param_id, uom, default=0):
    """Extract a parameter from a multi-param ISY command dict.
    Handles both named params (id='phrase') and unnamed params (id=''),
    falling back to single-param 'value' format."""
    query = command.get('query', {})
    # Named param: phrase.uom25
    key = f'{param_id}.uom{uom}'
    if key in query:
        return int(float(query[key]))
    # Unnamed param (id=""): .uom25
    key = f'.uom{uom}'
    if key in query:
        return int(float(query[key]))
    return int(command.get('value', default))


def _subset(lst):
    return ','.join(str(i) for i in range(len(lst))) if lst else '0'


def _write_profile_files(favorites, playlists, tts_phrases, clip_names, zone_names):
    """Write dynamic NLS and editors.xml, then call poly.updateProfile()."""

    # --- NLS ---
    lines = [_STATIC_NLS]

    lines.append('# Dynamic — Favorites')
    for i, name in enumerate(favorites):
        lines.append(f'CUST_FAV-{i} = {name}')
    if not favorites:
        lines.append('CUST_FAV-0 = (loading...)')

    lines.append('\n# Dynamic — Playlists')
    for i, name in enumerate(playlists):
        lines.append(f'CUST_PL-{i} = {name}')
    if not playlists:
        lines.append('CUST_PL-0 = (loading...)')

    lines.append('\n# Dynamic — TTS Phrases')
    for i, phrase in enumerate(tts_phrases):
        lines.append(f'CUST_TTS-{i} = {phrase}')
    if not tts_phrases:
        lines.append('CUST_TTS-0 = (not configured)')

    lines.append('\n# Dynamic — Clips')
    for i, name in enumerate(clip_names):
        lines.append(f'CUST_CLIP-{i} = {name}')
    if not clip_names:
        lines.append('CUST_CLIP-0 = (not configured)')

    lines.append('\n# Dynamic — Zones (for Join)')
    for i, name in enumerate(zone_names):
        lines.append(f'CUST_ZONE-{i} = {name}')
    if not zone_names:
        lines.append('CUST_ZONE-0 = (loading...)')

    with open(os.path.join(_PROFILE_DIR, 'nls', 'en_us.txt'), 'w') as f:
        f.write('\n'.join(lines) + '\n')

    # --- Editors ---
    editors_xml = f"""<editors>
{_STATIC_EDITORS}

  <!-- Dynamic — Favorites (auto-fetched from Jishi) -->
  <editor id="E_FAV">
    <range uom="25" subset="{_subset(favorites)}" nls="CUST_FAV"/>
  </editor>

  <!-- Dynamic — Playlists (auto-fetched from Jishi) -->
  <editor id="E_PLAYLIST">
    <range uom="25" subset="{_subset(playlists)}" nls="CUST_PL"/>
  </editor>

  <!-- Dynamic — TTS Phrases (from custom params tts_1..tts_10) -->
  <editor id="E_TTS">
    <range uom="25" subset="{_subset(tts_phrases)}" nls="CUST_TTS"/>
  </editor>

  <!-- Dynamic — Clips (from custom params clip_1..clip_5) -->
  <editor id="E_CLIP">
    <range uom="25" subset="{_subset(clip_names)}" nls="CUST_CLIP"/>
  </editor>

  <!-- Dynamic — Zones (for Join command) -->
  <editor id="E_ZONES">
    <range uom="25" subset="{_subset(zone_names)}" nls="CUST_ZONE"/>
  </editor>
</editors>
"""
    with open(os.path.join(_PROFILE_DIR, 'editor', 'editors.xml'), 'w') as f:
        f.write(editors_xml)

    LOGGER.info(f"Profile updated: {len(favorites)} favs, {len(playlists)} playlists, "
                f"{len(tts_phrases)} TTS, {len(clip_names)} clips, {len(zone_names)} zones")


# ---------------------------------------------------------------------------
# Speaker Node
# ---------------------------------------------------------------------------

class SpeakerNode(udi_interface.Node):
    """One node per Jishi zone (stereo pairs already merged by Jishi)."""

    id = 'sonos_speaker'

    drivers = [
        {'driver': 'ST',   'value': 0, 'uom': 25},  # Playback state
        {'driver': 'SVOL', 'value': 0, 'uom': 51},  # Player volume
        {'driver': 'GV1',  'value': 0, 'uom': 51},  # Group volume
        {'driver': 'GV2',  'value': 0, 'uom': 56},  # Bass
        {'driver': 'GV3',  'value': 0, 'uom': 56},  # Treble
        {'driver': 'GV4',  'value': 0, 'uom': 2},   # Mute
        {'driver': 'GV5',  'value': 0, 'uom': 2},   # Group mute
        {'driver': 'GV6',  'value': 0, 'uom': 2},   # Shuffle
        {'driver': 'GV7',  'value': 0, 'uom': 25},  # Repeat
        {'driver': 'GV8',  'value': 0, 'uom': 2},   # Crossfade
        {'driver': 'GV9',  'value': 0, 'uom': 2},   # Loudness
        {'driver': 'GV10', 'value': 0, 'uom': 2},   # Nightmode
        {'driver': 'GV11', 'value': 0, 'uom': 2},   # Speech enhancement
        {'driver': 'GV12', 'value': 0,  'uom': 56},  # Members in group
        {'driver': 'GV13', 'value': 50, 'uom': 51},  # Alert volume default (hidden)
    ]

    def __init__(self, polyglot, primary, address, name, zone_name, jishi_url, controller):
        super().__init__(polyglot, primary, address, name)
        self.zone_name = zone_name
        self.jishi_url = jishi_url
        self._ctrl = controller
        self._zp = _enc(zone_name)
        self._driver_cache: dict = {}

    def _cmd(self, path):
        return _jishi_cmd(self.jishi_url, f"/{self._zp}/{path}")

    def _set(self, driver, value):
        """setDriver with change detection — skips ISY update when value unchanged."""
        if self._driver_cache.get(driver) != value:
            self._driver_cache[driver] = value
            self.setDriver(driver, value)

    def update_from_state(self, state):
        pb = state.get('playbackState', 'STOPPED')
        self._set('ST', PLAYBACK_MAP.get(pb, 0))
        self._set('SVOL', state.get('volume', 0))

        eq = state.get('equalizer', {})
        self._set('GV2', eq.get('bass', 0))
        self._set('GV3', eq.get('treble', 0))
        self._set('GV4', 1 if state.get('mute', False) else 0)
        self._set('GV9', 1 if eq.get('loudness', False) else 0)
        self._set('GV10', 1 if eq.get('nightMode', False) else 0)
        self._set('GV11', 1 if eq.get('speechEnhancement', False) else 0)

        pm = state.get('playMode', {})
        self._set('GV6', 1 if pm.get('shuffle', False) else 0)
        self._set('GV7', REPEAT_MAP.get(pm.get('repeat', 'none'), 0))
        self._set('GV8', 1 if pm.get('crossfade', False) else 0)

        members = state.get('members', [])
        self._set('GV12', len(members) if members else 1)

        track = state.get('currentTrack', {})
        title  = track.get('title', '') or track.get('stationName', '')
        artist = track.get('artist', '')
        LOGGER.debug(f"{self.zone_name}: {pb} | {artist} - {title}")

    def update_group_state(self, group_state):
        if group_state:
            self._set('GV1', group_state.get('volume', 0))
            self._set('GV5', 1 if group_state.get('mute', False) else 0)

    def query(self, command=None):
        data = _jishi_get(self.jishi_url, f"/{self._zp}/state")
        if data:
            self.update_from_state(data)
            self.reportDrivers()

    # --- Transport ---
    def cmd_play(self, command):  self._cmd('play')
    def cmd_pause(self, command): self._cmd('pause')
    def cmd_next(self, command):  self._cmd('next')
    def cmd_prev(self, command):  self._cmd('previous')

    # --- Volume ---
    def cmd_set_vol(self, command):
        self._cmd(f"volume/{_cmd_val(command)}")
    def cmd_set_group_vol(self, command):
        self._cmd(f"groupVolume/{_cmd_val(command)}")

    # --- EQ ---
    def cmd_set_bass(self, command):
        self._cmd(f"bass/{_cmd_val(command)}")
    def cmd_set_treble(self, command):
        self._cmd(f"treble/{_cmd_val(command)}")

    # --- Mute: explicit no-param buttons + bool-param SET_MUTE for programs ---
    def cmd_mute(self, command):   self._cmd('mute')
    def cmd_unmute(self, command): self._cmd('unmute')

    def cmd_set_mute(self, command):
        self._cmd('mute' if _cmd_val(command) else 'unmute')

    def cmd_set_grp_mute(self, command):
        self._cmd('groupMute' if _cmd_val(command) else 'groupUnmute')

    def cmd_shuffle(self, command):
        self._cmd('shuffle/on' if _cmd_val(command) else 'shuffle/off')

    def cmd_repeat(self, command):
        # Jishi only supports repeat on/off; 0 (none) → off, 1/2 (one/all) → on
        self._cmd(f"repeat/{'off' if _cmd_val(command) == 0 else 'on'}")

    def cmd_crossfade(self, command):
        self._cmd('crossfade/on' if _cmd_val(command) else 'crossfade/off')

    def cmd_loudness(self, command):
        self._cmd('loudness/on' if _cmd_val(command) else 'loudness/off')

    def cmd_nightmode(self, command):
        self._cmd('nightMode/on' if _cmd_val(command) else 'nightMode/off')

    def cmd_speech_enh(self, command):
        self._cmd('speechEnhancement/on' if _cmd_val(command) else 'speechEnhancement/off')

    # --- Content (0-based index matches NLS CUST_FAV-N etc.) ---
    def _cmd_indexed(self, command, items, verb, label, threaded=False):
        """Dispatch a Jishi command by index into a list (favorites/playlists/TTS)."""
        idx = _cmd_val(command)
        if idx < len(items):
            path = f"{verb}/{_enc(items[idx])}"
            if threaded:
                threading.Thread(target=self._cmd, args=(path,), daemon=True).start()
            else:
                self._cmd(path)
        else:
            LOGGER.warning(f"{self.zone_name}: {label} index {idx} out of range")

    def cmd_play_favorite(self, command):
        self._cmd_indexed(command, self._ctrl.favorites, 'favorite', 'favorite')

    def cmd_play_playlist(self, command):
        self._cmd_indexed(command, self._ctrl.playlists, 'playlist', 'playlist')

    def cmd_say(self, command):
        idx = _cmd_param(command, 'phrase', 25)
        vol = _cmd_param(command, 'vol', 51)
        if idx < len(self._ctrl.tts_phrases):
            phrase = self._ctrl.tts_phrases[idx]
            path = f"say/{_enc(phrase)}/en-us"
            if vol > 0:
                path += f"/{vol}"
            threading.Thread(target=self._cmd, args=(path,), daemon=True).start()
        else:
            LOGGER.warning(f"{self.zone_name}: TTS index {idx} out of range")

    def cmd_clip(self, command):
        idx = _cmd_param(command, 'clip', 25)
        vol = _cmd_param(command, 'vol', 51)
        if idx < len(self._ctrl.clip_names):
            name = self._ctrl.clip_names[idx]
            path = f"clip/{_enc(name)}"
            if vol > 0:
                path += f"/{vol}"
            threading.Thread(target=self._cmd, args=(path,), daemon=True).start()
        else:
            LOGGER.warning(f"{self.zone_name}: clip index {idx} out of range")

    def cmd_sleep(self, command):
        minutes = _cmd_val(command)
        self._cmd('sleep/off' if minutes == 0 else f"sleep/{minutes * 60}")

    # --- Grouping ---
    def cmd_join(self, command):
        idx = _cmd_val(command)
        zones = self._ctrl.zone_names
        if idx < len(zones):
            target = zones[idx]
            if target != self.zone_name:
                _jishi_cmd(self.jishi_url, f"/{_enc(target)}/join/{self._zp}")
            else:
                LOGGER.warning(f"{self.zone_name}: cannot join self")
        else:
            LOGGER.warning(f"{self.zone_name}: join zone index {idx} out of range")

    def cmd_leave(self, command):
        self._cmd('leave')

    def cmd_party(self, command):
        """Join all other zones to this one."""
        others = [n for n in self._ctrl.zone_names if n != self.zone_name]
        with ThreadPoolExecutor(max_workers=len(others) or 1) as ex:
            for name in others:
                ex.submit(_jishi_cmd, self.jishi_url, f"/{_enc(name)}/join/{self._zp}")

    # udi_interface calls fun(self, command) with unbound references
    commands = {
        'PLAY':          cmd_play,
        'PAUSE':         cmd_pause,
        'NEXT':          cmd_next,
        'PREV':          cmd_prev,
        'MUTE':          cmd_mute,
        'UNMUTE':        cmd_unmute,
        'SET_VOL':       cmd_set_vol,
        'SET_GRP_VOL':   cmd_set_group_vol,
        'SET_BASS':      cmd_set_bass,
        'SET_TREBLE':    cmd_set_treble,
        'SET_MUTE':      cmd_set_mute,
        'SET_GRP_MUTE':  cmd_set_grp_mute,
        'SHUFFLE':       cmd_shuffle,
        'REPEAT':        cmd_repeat,
        'CROSSFADE':     cmd_crossfade,
        'LOUDNESS':      cmd_loudness,
        'NIGHTMODE':     cmd_nightmode,
        'SPEECH_ENH':    cmd_speech_enh,
        'PLAY_FAVORITE': cmd_play_favorite,
        'PLAY_PLAYLIST': cmd_play_playlist,
        'SAY':           cmd_say,
        'CLIP':          cmd_clip,
        'SLEEP':         cmd_sleep,
        'JOIN':          cmd_join,
        'LEAVE':         cmd_leave,
        'PARTY':         cmd_party,
        'QUERY':         query,
    }


# ---------------------------------------------------------------------------
# Controller Node
# ---------------------------------------------------------------------------

class Controller(udi_interface.Node):

    id = 'sonos_controller'

    drivers = [
        {'driver': 'ST', 'value': 0, 'uom': 2},
    ]

    def __init__(self, polyglot, primary, address, name):
        super().__init__(polyglot, primary, address, name)
        self.poly = polyglot
        self._speakers = {}       # address -> SpeakerNode
        self._jishi_url = ''
        self.favorites = []
        self.playlists = []
        self.tts_phrases = []
        self.clip_names = []
        self.zone_names = []      # ordered list of zone room names for JOIN
        self._poll_lock = threading.Lock()
        self._initialized = False
        self._controller_added = False   # True once controller node lands in ISY
        self._node_added = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix='sonos')

        polyglot.subscribe(polyglot.START,        self.start)
        polyglot.subscribe(polyglot.CUSTOMPARAMS, self.param_handler)
        polyglot.subscribe(polyglot.POLL,         self.poll)
        polyglot.subscribe(polyglot.STOP,         self.stop)
        polyglot.subscribe(polyglot.ADDNODEDONE,  self._on_node_added)

        polyglot.ready()

    def _on_node_added(self, data):
        """Called by udi_interface when a node is fully added to ISY."""
        LOGGER.debug(f"ADDNODEDONE: {data}")
        self._node_added.set()

    def _add_node_wait(self, node, timeout=15):
        """Add a node and wait for ISY to confirm it before continuing."""
        self._node_added.clear()
        self.poly.addNode(node)
        if not self._node_added.wait(timeout=timeout):
            LOGGER.warning(f"Timeout waiting for node {getattr(node, 'address', '?')} to be added to ISY")

    def start(self):
        LOGGER.info('Sonos Jishi NodeServer starting')
        if not self._initialized:
            self.discover()

    def stop(self):
        LOGGER.info('Sonos Jishi NodeServer stopping')
        self.setDriver('ST', 0)
        self._pool.shutdown(wait=False)

    def param_handler(self, params):
        self.poly.Notices.clear()

        jishi_url = params.get('jishi_url', '').strip().rstrip('/')
        if not jishi_url:
            self.poly.Notices['config'] = (
                'Set jishi_url in Custom Parameters (e.g. http://zeus:5005)')
            return

        self._jishi_url = jishi_url
        self.tts_phrases = [
            v for i in range(1, 11)
            if (v := params.get(f'tts_{i}', '').strip())
        ]
        self.clip_names = [
            v for i in range(1, 21)
            if (v := params.get(f'clip_{i}', '').strip())
        ]
        LOGGER.info(f"Jishi URL: {self._jishi_url}, TTS: {self.tts_phrases}, Clips: {self.clip_names}")
        self._initialized = False
        self.discover()

    def discover(self, command=None):
        if not self._jishi_url:
            LOGGER.warning('No jishi_url configured — skipping discover')
            return

        # Serialize discover against polls; poll uses non-blocking acquire so it
        # will skip cleanly while discover holds the lock.
        with self._poll_lock:
            self._do_discover()

    def _do_discover(self):
        zones = _jishi_get(self._jishi_url, '/zones')
        if zones is None:
            self.poly.Notices['jishi'] = f"Cannot reach Jishi at {self._jishi_url}"
            return

        self.poly.Notices.clear()
        self._initialized = True

        # Collect zone names for JOIN support (must be done before _refresh_content
        # so the CUST_ZONE editor is populated correctly).
        self.zone_names = [n for z in zones if (n := _zone_room_name(z))]

        # Refresh content and push updated profile to ISY before adding nodes,
        # matching the tutorial pattern: updateProfile → addNode → wait ADDNODEDONE.
        self._refresh_content(force=True)

        # Add controller node only on first install; it persists across restarts.
        if not self._controller_added:
            self._add_node_wait(self)
            self._controller_added = True
        self.setDriver('ST', 1)

        # Add speaker nodes one at a time, waiting for each to land in ISY.
        for zone in zones:
            zone_name = _zone_room_name(zone)
            if not zone_name:
                continue

            address = _zone_address(zone_name)
            if address not in self._speakers:
                LOGGER.info(f"Adding speaker node: {zone_name} ({address})")
                node = SpeakerNode(
                    self.poly, self.address, address, zone_name,
                    zone_name, self._jishi_url, self)
                self._add_node_wait(node)
                self._speakers[address] = node

            self._apply_zone_state(zone)

        LOGGER.info(f"Discovery complete — {len(self._speakers)} zones")

    def _apply_zone_state(self, zone):
        """Update a speaker node's drivers from a Jishi zone dict."""
        zone_name = _zone_room_name(zone)
        if not zone_name:
            return
        address = _zone_address(zone_name)
        if address not in self._speakers:
            return
        coordinator = zone.get('coordinator', zone)
        self._speakers[address].update_from_state(coordinator.get('state', coordinator))
        self._speakers[address].update_group_state(zone.get('groupState', {}))

    def _refresh_content(self, force=False):
        """Fetch favorites/playlists from Jishi; update ISY profile if changed."""
        favs_f = self._pool.submit(_jishi_get, self._jishi_url, '/favorites')
        pls_f  = self._pool.submit(_jishi_get, self._jishi_url, '/playlists')
        new_favs = favs_f.result() or []
        new_pls  = pls_f.result() or []
        if not isinstance(new_favs, list): new_favs = []
        if not isinstance(new_pls, list):  new_pls = []

        # TTS phrases are not compared here — they come from param_handler, not
        # Jishi. When TTS changes, param_handler → discover(force=True) covers it.
        changed = force or new_favs != self.favorites or new_pls != self.playlists
        self.favorites = new_favs
        self.playlists = new_pls

        if changed:
            _write_profile_files(
                self.favorites, self.playlists,
                self.tts_phrases, self.clip_names, self.zone_names)
            self.poly.updateProfile()
            return True
        return False

    def poll(self, flag):
        if not self._jishi_url:
            return
        if not self._poll_lock.acquire(blocking=False):
            LOGGER.debug('Poll already running, skipping')
            return
        try:
            if flag == 'shortPoll':
                self._short_poll()
            else:
                self._long_poll()
        finally:
            self._poll_lock.release()

    def _short_poll(self):
        zones = _jishi_get(self._jishi_url, '/zones')
        if zones is None:
            LOGGER.warning('Short poll: could not reach Jishi')
            return
        for zone in zones:
            self._apply_zone_state(zone)

    def _long_poll(self):
        LOGGER.debug('Long poll: refreshing content lists')
        self._refresh_content()

    # --- Global commands ---
    def cmd_pause_all(self, command):
        _jishi_cmd(self._jishi_url, '/pauseall')

    def cmd_resume_all(self, command):
        _jishi_cmd(self._jishi_url, '/resumeall')

    def cmd_ungroup_all(self, command):
        with ThreadPoolExecutor(max_workers=len(self.zone_names) or 1) as ex:
            for name in self.zone_names:
                ex.submit(_jishi_cmd, self._jishi_url, f"/{_enc(name)}/leave")

    def cmd_party_all(self, command):
        """Join all zones to the first zone (party mode)."""
        if not self.zone_names:
            return
        host = self.zone_names[0]
        enc_host = _enc(host)
        others = self.zone_names[1:]
        with ThreadPoolExecutor(max_workers=len(others) or 1) as ex:
            for name in others:
                ex.submit(_jishi_cmd, self._jishi_url, f"/{_enc(name)}/join/{enc_host}")

    def cmd_say_all(self, command):
        """Say a TTS phrase on all speakers."""
        idx = _cmd_param(command, 'phrase', 25)
        vol = _cmd_param(command, 'vol', 51)
        if idx < len(self.tts_phrases):
            phrase = self.tts_phrases[idx]
            path = f"sayall/{_enc(phrase)}"
            if vol > 0:
                path += f"/en-us/{vol}"
            threading.Thread(
                target=_jishi_cmd, args=(self._jishi_url, f"/{path}"),
                daemon=True).start()
        else:
            LOGGER.warning(f"SAY_ALL: TTS index {idx} not configured")

    def cmd_clip_all(self, command):
        """Play a clip on all speakers."""
        idx = _cmd_param(command, 'clip', 25)
        vol = _cmd_param(command, 'vol', 51)
        if idx >= len(self.clip_names):
            LOGGER.warning(f"CLIP_ALL: clip index {idx} not configured")
            return
        clip = self.clip_names[idx]
        def _play(name):
            path = f"clip/{_enc(clip)}"
            if vol > 0:
                path += f"/{vol}"
            _jishi_cmd(self._jishi_url, f"/{_enc(name)}/{path}")
        with ThreadPoolExecutor(max_workers=len(self.zone_names) or 1) as ex:
            for name in self.zone_names:
                ex.submit(_play, name)

    def cmd_refresh_content(self, command):
        """Manually trigger an immediate content refresh (favorites, playlists, TTS)."""
        LOGGER.info('Manual content refresh triggered')
        self._refresh_content(force=True)

    def query(self, command=None):
        self.reportDrivers()
        for node in self._speakers.values():
            node.query()

    # udi_interface calls fun(self, command) with unbound references
    commands = {
        'DISCOVER':        discover,
        'PAUSE_ALL':       cmd_pause_all,
        'RESUME_ALL':      cmd_resume_all,
        'UNGROUP_ALL':     cmd_ungroup_all,
        'PARTY':           cmd_party_all,
        'SAY_ALL':         cmd_say_all,
        'CLIP_ALL':        cmd_clip_all,
        'REFRESH_CONTENT': cmd_refresh_content,
        'QUERY':           query,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    try:
        poly = udi_interface.Interface([])
        poly.start()
        Controller(poly, 'controller', 'controller', 'Sonos')
        poly.runForever()
    except (KeyboardInterrupt, SystemExit):
        sys.exit(0)
    except Exception as e:
        LOGGER.exception(f"Fatal error: {e}")
        sys.exit(1)
