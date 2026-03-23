#!/usr/bin/env python3
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
import threading
import sys

import requests
import udi_interface

LOGGER = udi_interface.LOGGER

# Path to profile directory (relative to this script)
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_PROFILE_DIR = os.path.join(_PLUGIN_DIR, 'profile')

# ---------------------------------------------------------------------------
# Static NLS content — written to en_us.txt alongside dynamic entries
# ---------------------------------------------------------------------------

_STATIC_NLS = """\
# Node Server Names
ND-sonos-controller-NAME = Sonos Controller
ND-sonos-speaker-NAME = Sonos Speaker

# Controller Drivers
ST-sonos-controller-ST-NAME = Status

# Speaker Drivers
ST-sonos-speaker-ST-NAME = Playback State
ST-sonos-speaker-SVOL-NAME = Volume
ST-sonos-speaker-GV1-NAME = Bass
ST-sonos-speaker-GV2-NAME = Treble
ST-sonos-speaker-GV3-NAME = Mute
ST-sonos-speaker-GV4-NAME = Shuffle
ST-sonos-speaker-GV5-NAME = Repeat
ST-sonos-speaker-GV6-NAME = Crossfade
ST-sonos-speaker-GV7-NAME = Loudness
ST-sonos-speaker-GV8-NAME = Track Title
ST-sonos-speaker-GV9-NAME = Artist
ST-sonos-speaker-GV10-NAME = Album / Station

# Controller Commands
CMD-sonos-controller-DISCOVER-NAME = Re-Discover
CMD-sonos-controller-PAUSE_ALL-NAME = Pause All
CMD-sonos-controller-RESUME_ALL-NAME = Resume All

# Speaker Commands
CMD-sonos-speaker-DON-NAME = Play
CMD-sonos-speaker-DOF-NAME = Pause
CMD-sonos-speaker-STOP-NAME = Stop
CMD-sonos-speaker-NEXT-NAME = Next Track
CMD-sonos-speaker-PREV-NAME = Previous Track
CMD-sonos-speaker-SET_VOL-NAME = Set Volume
CMD-sonos-speaker-VOL_UP-NAME = Volume Up
CMD-sonos-speaker-VOL_DOWN-NAME = Volume Down
CMD-sonos-speaker-SET_BASS-NAME = Set Bass
CMD-sonos-speaker-SET_TREBLE-NAME = Set Treble
CMD-sonos-speaker-MUTE-NAME = Mute
CMD-sonos-speaker-UNMUTE-NAME = Unmute
CMD-sonos-speaker-SHUFFLE_ON-NAME = Shuffle On
CMD-sonos-speaker-SHUFFLE_OFF-NAME = Shuffle Off
CMD-sonos-speaker-REPEAT-NAME = Set Repeat
CMD-sonos-speaker-CROSSFADE-NAME = Toggle Crossfade
CMD-sonos-speaker-PLAY_FAVORITE-NAME = Play Favorite
CMD-sonos-speaker-PLAY_PLAYLIST-NAME = Play Playlist
CMD-sonos-speaker-SAY-NAME = Say (TTS)
CMD-sonos-speaker-SLEEP-NAME = Sleep Timer

# Playback State index values (UOM 25)
CUST_PB-0 = Stopped
CUST_PB-1 = Playing
CUST_PB-2 = Paused
CUST_PB-3 = Transitioning

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

# Static editors XML block (volume, EQ, booleans, etc.)
_STATIC_EDITORS = """\
  <!-- Volume: 0-100 percent -->
  <editor id="E_VOL">
    <range uom="51" subset="0,100" step="1" prec="0"/>
  </editor>

  <!-- Bass/Treble: -10 to 10 -->
  <editor id="E_EQ">
    <range uom="56" subset="-10,10" step="1" prec="0"/>
  </editor>

  <!-- Playback State -->
  <editor id="E_PLAYBACK">
    <range uom="25" subset="0,1,2,3" nls="CUST_PB"/>
  </editor>

  <!-- Boolean -->
  <editor id="E_BOOL">
    <range uom="2" subset="0,1"/>
  </editor>

  <!-- Repeat -->
  <editor id="E_REPEAT">
    <range uom="25" subset="0,1,2" nls="CUST_REPEAT"/>
  </editor>

  <!-- Sleep timer in minutes -->
  <editor id="E_SLEEP">
    <range uom="25" subset="0,15,30,45,60,90" nls="CUST_SLEEP"/>
  </editor>

  <!-- Controller status -->
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
    'PAUSED_PLAYBACK': 2,
    'TRANSITIONING':   3,
}

REPEAT_MAP = {
    'none': 0,
    'one':  1,
    'all':  2,
}

SLEEP_MINUTES = [0, 15, 30, 45, 60, 90]


def _jishi_get(base_url, path, timeout=5):
    """GET from Jishi, return parsed JSON or None on error."""
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        LOGGER.warning(f"Jishi GET {url} failed: {e}")
        return None


def _jishi_cmd(base_url, path, timeout=5):
    """GET a Jishi command endpoint, return True on success."""
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return True
    except Exception as e:
        LOGGER.warning(f"Jishi command {url} failed: {e}")
        return False


def _zone_path(zone_name):
    """URL-encode a zone name for Jishi paths."""
    from urllib.parse import quote
    return quote(zone_name, safe='')


def _zone_address(zone_name):
    """Convert zone name to a valid ISY node address (<=14 chars, lowercase alphanumeric)."""
    addr = re.sub(r'[^a-z0-9]', '', zone_name.lower())
    return addr[:14]


def _write_profile_files(favorites, playlists, tts_phrases):
    """
    Write dynamic NLS and editors.xml based on current content lists.
    Call poly.updateProfile() after this to push changes to ISY.
    """
    # --- NLS ---
    lines = [_STATIC_NLS]

    lines.append('# Dynamic — Favorites (auto-fetched from Jishi)')
    if favorites:
        for i, name in enumerate(favorites):
            lines.append(f'CUST_FAV-{i} = {name}')
    else:
        lines.append('CUST_FAV-0 = (none configured)')

    lines.append('')
    lines.append('# Dynamic — Playlists (auto-fetched from Jishi)')
    if playlists:
        for i, name in enumerate(playlists):
            lines.append(f'CUST_PL-{i} = {name}')
    else:
        lines.append('CUST_PL-0 = (none configured)')

    lines.append('')
    lines.append('# Dynamic — TTS Phrases (configured via Custom Parameters)')
    if tts_phrases:
        for i, phrase in enumerate(tts_phrases):
            lines.append(f'CUST_TTS-{i} = {phrase}')
    else:
        lines.append('CUST_TTS-0 = (none configured)')

    nls_path = os.path.join(_PROFILE_DIR, 'nls', 'en_us.txt')
    with open(nls_path, 'w') as f:
        f.write('\n'.join(lines) + '\n')

    # --- Editors ---
    def _subset(lst):
        return ','.join(str(i) for i in range(len(lst))) if lst else '0'

    fav_subset = _subset(favorites)
    pl_subset  = _subset(playlists)
    tts_subset = _subset(tts_phrases)

    editors_xml = f"""<editors>
{_STATIC_EDITORS}

  <!-- Dynamic — Favorites -->
  <editor id="E_FAV">
    <range uom="25" subset="{fav_subset}" nls="CUST_FAV"/>
  </editor>

  <!-- Dynamic — Playlists -->
  <editor id="E_PLAYLIST">
    <range uom="25" subset="{pl_subset}" nls="CUST_PL"/>
  </editor>

  <!-- Dynamic — TTS Phrases -->
  <editor id="E_TTS">
    <range uom="25" subset="{tts_subset}" nls="CUST_TTS"/>
  </editor>
</editors>
"""
    editors_path = os.path.join(_PROFILE_DIR, 'editor', 'editors.xml')
    with open(editors_path, 'w') as f:
        f.write(editors_xml)

    LOGGER.info(f"Profile files updated: {len(favorites)} favorites, "
                f"{len(playlists)} playlists, {len(tts_phrases)} TTS phrases")


# ---------------------------------------------------------------------------
# Speaker Node
# ---------------------------------------------------------------------------

class SpeakerNode(udi_interface.Node):
    """One node per Jishi zone (stereo pairs already merged by Jishi)."""

    id = 'sonos-speaker'

    drivers = [
        {'driver': 'ST',   'value': 0, 'uom': 25},  # Playback state
        {'driver': 'SVOL', 'value': 0, 'uom': 51},  # Volume
        {'driver': 'GV1',  'value': 0, 'uom': 56},  # Bass
        {'driver': 'GV2',  'value': 0, 'uom': 56},  # Treble
        {'driver': 'GV3',  'value': 0, 'uom': 2},   # Mute
        {'driver': 'GV4',  'value': 0, 'uom': 2},   # Shuffle
        {'driver': 'GV5',  'value': 0, 'uom': 25},  # Repeat
        {'driver': 'GV6',  'value': 0, 'uom': 2},   # Crossfade
        {'driver': 'GV7',  'value': 0, 'uom': 2},   # Loudness
        {'driver': 'GV8',  'value': 0, 'uom': 25},  # Track title
        {'driver': 'GV9',  'value': 0, 'uom': 25},  # Artist
        {'driver': 'GV10', 'value': 0, 'uom': 25},  # Album / Station
    ]

    commands = {
        'DON':           'cmd_play',
        'DOF':           'cmd_pause',
        'STOP':          'cmd_stop',
        'NEXT':          'cmd_next',
        'PREV':          'cmd_prev',
        'SET_VOL':       'cmd_set_vol',
        'VOL_UP':        'cmd_vol_up',
        'VOL_DOWN':      'cmd_vol_down',
        'SET_BASS':      'cmd_set_bass',
        'SET_TREBLE':    'cmd_set_treble',
        'MUTE':          'cmd_mute',
        'UNMUTE':        'cmd_unmute',
        'SHUFFLE_ON':    'cmd_shuffle_on',
        'SHUFFLE_OFF':   'cmd_shuffle_off',
        'REPEAT':        'cmd_repeat',
        'CROSSFADE':     'cmd_crossfade',
        'PLAY_FAVORITE': 'cmd_play_favorite',
        'PLAY_PLAYLIST': 'cmd_play_playlist',
        'SAY':           'cmd_say',
        'SLEEP':         'cmd_sleep',
        'QUERY':         'query',
    }

    def __init__(self, polyglot, primary, address, name, zone_name, jishi_url, controller):
        super().__init__(polyglot, primary, address, name)
        self.zone_name = zone_name
        self.jishi_url = jishi_url
        self._controller = controller   # back-ref to get current favorites/playlists/tts
        self._zone_path = _zone_path(zone_name)

    def _cmd(self, path):
        return _jishi_cmd(self.jishi_url, f"/{self._zone_path}/{path}")

    def update_from_state(self, state):
        """Update all drivers from a Jishi zone state dict."""
        pb_raw = state.get('playbackState', 'STOPPED')
        self.setDriver('ST', PLAYBACK_MAP.get(pb_raw, 0))
        self.setDriver('SVOL', state.get('volume', 0))

        eq = state.get('equalizer', {})
        self.setDriver('GV1', eq.get('bass', 0))
        self.setDriver('GV2', eq.get('treble', 0))
        self.setDriver('GV3', 1 if state.get('mute', False) else 0)

        pm = state.get('playMode', {})
        self.setDriver('GV4', 1 if pm.get('shuffle', False) else 0)
        self.setDriver('GV5', REPEAT_MAP.get(pm.get('repeat', 'none'), 0))
        self.setDriver('GV6', 1 if pm.get('crossfade', False) else 0)
        self.setDriver('GV7', 1 if eq.get('loudness', False) else 0)

        track = state.get('currentTrack', {})
        title  = track.get('title', '') or track.get('stationName', '')
        artist = track.get('artist', '')
        album  = track.get('album', '') or track.get('stationName', '')

        LOGGER.debug(f"{self.zone_name}: {pb_raw} | {artist} - {title} | {album}")

    def query(self, command=None):
        state = _jishi_get(self.jishi_url, f"/{self._zone_path}/state")
        if state:
            self.update_from_state(state)
            self.reportDrivers()

    # --- Transport ---

    def cmd_play(self, command):   self._cmd('play')
    def cmd_pause(self, command):  self._cmd('pause')
    def cmd_stop(self, command):   self._cmd('stop')
    def cmd_next(self, command):   self._cmd('next')
    def cmd_prev(self, command):   self._cmd('previous')

    # --- Volume ---

    def cmd_set_vol(self, command):
        self._cmd(f"volume/{int(command.get('value', 0))}")

    def cmd_vol_up(self, command):   self._cmd('volume/+2')
    def cmd_vol_down(self, command): self._cmd('volume/-2')

    # --- EQ ---

    def cmd_set_bass(self, command):
        self._cmd(f"bass/{int(command.get('value', 0))}")

    def cmd_set_treble(self, command):
        self._cmd(f"treble/{int(command.get('value', 0))}")

    # --- Mute ---

    def cmd_mute(self, command):   self._cmd('mute')
    def cmd_unmute(self, command): self._cmd('unmute')

    # --- Play modes ---

    def cmd_shuffle_on(self, command):  self._cmd('shuffle/on')
    def cmd_shuffle_off(self, command): self._cmd('shuffle/off')

    def cmd_repeat(self, command):
        modes = {0: 'none', 1: 'one', 2: 'all'}
        self._cmd(f"repeat/{modes.get(int(command.get('value', 0)), 'none')}")

    def cmd_crossfade(self, command): self._cmd('crossfade/toggle')

    # --- Content (index is 0-based, matching NLS CUST_FAV-N etc.) ---

    def cmd_play_favorite(self, command):
        idx = int(command.get('value', 0))
        favs = self._controller.favorites
        if idx < len(favs):
            from urllib.parse import quote
            self._cmd(f"favorite/{quote(favs[idx], safe='')}")
        else:
            LOGGER.warning(f"{self.zone_name}: favorite index {idx} not available")

    def cmd_play_playlist(self, command):
        idx = int(command.get('value', 0))
        pls = self._controller.playlists
        if idx < len(pls):
            from urllib.parse import quote
            self._cmd(f"playlist/{quote(pls[idx], safe='')}")
        else:
            LOGGER.warning(f"{self.zone_name}: playlist index {idx} not available")

    def cmd_say(self, command):
        idx = int(command.get('value', 0))
        tts = self._controller.tts_phrases
        if idx < len(tts):
            from urllib.parse import quote
            self._cmd(f"say/{quote(tts[idx], safe='')}")
        else:
            LOGGER.warning(f"{self.zone_name}: TTS index {idx} not configured")

    def cmd_sleep(self, command):
        minutes = int(command.get('value', 0))
        if minutes == 0:
            self._cmd('sleep/off')
        else:
            self._cmd(f"sleep/{minutes * 60}")


# ---------------------------------------------------------------------------
# Controller Node
# ---------------------------------------------------------------------------

class Controller(udi_interface.Node):

    id = 'sonos-controller'

    drivers = [
        {'driver': 'ST', 'value': 0, 'uom': 2},
    ]

    commands = {
        'DISCOVER':   'discover',
        'PAUSE_ALL':  'cmd_pause_all',
        'RESUME_ALL': 'cmd_resume_all',
        'QUERY':      'query',
    }

    def __init__(self, polyglot, primary, address, name):
        super().__init__(polyglot, primary, address, name)
        self.poly = polyglot
        self._speakers = {}       # address -> SpeakerNode
        self._jishi_url = ''
        self.favorites = []       # auto-fetched from Jishi /favorites
        self.playlists = []       # auto-fetched from Jishi /playlists
        self.tts_phrases = []     # user-configured via custom params
        self._poll_lock = threading.Lock()
        self._initialized = False

        polyglot.subscribe(polyglot.START,        self.start)
        polyglot.subscribe(polyglot.CUSTOMPARAMS, self.param_handler)
        polyglot.subscribe(polyglot.POLL,         self.poll)
        polyglot.subscribe(polyglot.STOP,         self.stop)

        polyglot.ready()
        polyglot.addNode(self, conn_status='ST')

    # --- Lifecycle ---

    def start(self):
        LOGGER.info('Sonos Jishi NodeServer starting')
        self.setDriver('ST', 1)
        if not self._initialized:
            self.discover()

    def stop(self):
        LOGGER.info('Sonos Jishi NodeServer stopping')
        self.setDriver('ST', 0)

    # --- Parameters ---

    def param_handler(self, params):
        self.poly.Notices.clear()

        jishi_url = params.get('jishi_url', '').strip().rstrip('/')
        if not jishi_url:
            self.poly.Notices['config'] = (
                'Set jishi_url in Custom Parameters (e.g. http://zeus:5005)')
            return

        self._jishi_url = jishi_url

        # TTS phrases are user-defined (tts_1 .. tts_10)
        self.tts_phrases = [
            v for i in range(1, 11)
            if (v := params.get(f'tts_{i}', '').strip())
        ]

        LOGGER.info(f"Jishi URL: {self._jishi_url}")
        LOGGER.info(f"TTS phrases: {self.tts_phrases}")

        self._initialized = False
        self.discover()

    # --- Discovery ---

    def discover(self, command=None):
        """Create/update speaker nodes for all zones reported by Jishi."""
        if not self._jishi_url:
            LOGGER.warning('No jishi_url configured — skipping discover')
            return

        zones = _jishi_get(self._jishi_url, '/zones')
        if zones is None:
            self.poly.Notices['jishi'] = f"Cannot reach Jishi at {self._jishi_url}"
            LOGGER.error(f"Cannot reach Jishi at {self._jishi_url}")
            return

        self.poly.Notices.clear()
        self._initialized = True

        # Fetch favorites and playlists for dynamic profile
        self._refresh_content_lists()

        for zone in zones:
            zone_name = (zone.get('coordinator', {}).get('roomName')
                         or zone.get('roomName', ''))
            if not zone_name:
                continue

            address = _zone_address(zone_name)

            if address not in self._speakers:
                LOGGER.info(f"Adding speaker node: {zone_name} ({address})")
                node = SpeakerNode(
                    self.poly, self.address, address, zone_name,
                    zone_name, self._jishi_url, self)
                self.poly.addNode(node)
                self._speakers[address] = node

            state = zone.get('coordinator', zone)
            self._speakers[address].update_from_state(state)

        LOGGER.info(f"Discovery complete — {len(self._speakers)} zones")

    def _refresh_content_lists(self):
        """Fetch favorites and playlists from Jishi, update ISY profile if changed."""
        new_favs = _jishi_get(self._jishi_url, '/favorites') or []
        new_pls  = _jishi_get(self._jishi_url, '/playlists') or []

        # Jishi returns list of strings for favorites/playlists
        if not isinstance(new_favs, list):
            new_favs = []
        if not isinstance(new_pls, list):
            new_pls = []

        changed = (new_favs != self.favorites or new_pls != self.playlists)
        self.favorites = new_favs
        self.playlists = new_pls

        if changed:
            LOGGER.info(f"Content changed — rebuilding ISY profile: "
                        f"{len(self.favorites)} favorites, {len(self.playlists)} playlists, "
                        f"{len(self.tts_phrases)} TTS")
            _write_profile_files(self.favorites, self.playlists, self.tts_phrases)
            self.poly.updateProfile()

    # --- Polling ---

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
        """Fetch all zone states in one /zones call."""
        zones = _jishi_get(self._jishi_url, '/zones')
        if zones is None:
            LOGGER.warning('Short poll: could not reach Jishi')
            return
        for zone in zones:
            zone_name = (zone.get('coordinator', {}).get('roomName')
                         or zone.get('roomName', ''))
            if not zone_name:
                continue
            address = _zone_address(zone_name)
            if address in self._speakers:
                self._speakers[address].update_from_state(zone.get('coordinator', zone))

    def _long_poll(self):
        """Re-check favorites/playlists from Jishi; update ISY profile if changed."""
        LOGGER.debug('Long poll: refreshing content lists from Jishi')
        self._refresh_content_lists()

        # Also re-write profile if TTS phrases changed (from param updates)
        # _refresh_content_lists handles favorites/playlists changes;
        # a full rewrite here ensures TTS is always current.
        _write_profile_files(self.favorites, self.playlists, self.tts_phrases)
        self.poly.updateProfile()

    # --- Global commands ---

    def cmd_pause_all(self, command):
        _jishi_cmd(self._jishi_url, '/pauseall')

    def cmd_resume_all(self, command):
        _jishi_cmd(self._jishi_url, '/resumeall')

    def query(self, command=None):
        self.reportDrivers()
        for node in self._speakers.values():
            node.query()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    try:
        poly = udi_interface.Interface([])
        poly.start('3.0.0')
        Controller(poly, 'controller', 'controller', 'Sonos')
        poly.runForever()
    except (KeyboardInterrupt, SystemExit):
        sys.exit(0)
    except Exception as e:
        LOGGER.exception(f"Fatal error: {e}")
        sys.exit(1)
