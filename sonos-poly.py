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
import threading
import sys

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
ND-sonos-controller-NAME = Sonos Controller
ND-sonos-speaker-NAME = Sonos Speaker

# Controller Drivers
ST-sonos-controller-ST-NAME = Status

# Controller Commands
CMD-sonos-controller-DISCOVER-NAME = Re-Discover
CMD-sonos-controller-PAUSE_ALL-NAME = Pause All
CMD-sonos-controller-RESUME_ALL-NAME = Resume All
CMD-sonos-controller-UNGROUP_ALL-NAME = Ungroup All
CMD-sonos-controller-PARTY-NAME = Party Mode
CMD-sonos-controller-SAY_ALL-NAME = Say All
CMD-sonos-controller-REFRESH_CONTENT-NAME = Refresh Content

# Speaker Drivers
ST-sonos-speaker-ST-NAME = Playback State
ST-sonos-speaker-SVOL-NAME = Volume
ST-sonos-speaker-GV1-NAME = Group Volume
ST-sonos-speaker-GV2-NAME = Bass
ST-sonos-speaker-GV3-NAME = Treble
ST-sonos-speaker-GV4-NAME = Mute
ST-sonos-speaker-GV5-NAME = Group Mute
ST-sonos-speaker-GV6-NAME = Shuffle
ST-sonos-speaker-GV7-NAME = Repeat
ST-sonos-speaker-GV8-NAME = Crossfade
ST-sonos-speaker-GV9-NAME = Loudness
ST-sonos-speaker-GV10-NAME = Nightmode
ST-sonos-speaker-GV11-NAME = Speech Enhancement
ST-sonos-speaker-GV12-NAME = Members

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
CMD-sonos-speaker-PLAY_PAUSE-NAME = Play / Pause Toggle
CMD-sonos-speaker-SET_GRP_VOL-NAME = Set Group Volume
CMD-sonos-speaker-SHUFFLE_ON-NAME = Shuffle On
CMD-sonos-speaker-SHUFFLE_OFF-NAME = Shuffle Off
CMD-sonos-speaker-REPEAT-NAME = Set Repeat
CMD-sonos-speaker-CROSSFADE_ON-NAME = Crossfade On
CMD-sonos-speaker-CROSSFADE_OFF-NAME = Crossfade Off
CMD-sonos-speaker-PLAY_FAVORITE-NAME = Play Favorite
CMD-sonos-speaker-PLAY_PLAYLIST-NAME = Play Playlist
CMD-sonos-speaker-SAY-NAME = Say (TTS)
CMD-sonos-speaker-SLEEP-NAME = Sleep Timer
CMD-sonos-speaker-JOIN-NAME = Join Zone
CMD-sonos-speaker-LEAVE-NAME = Leave Group
CMD-sonos-speaker-PARTY-NAME = Party Mode

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
    <range uom="51" subset="0,100" step="1" prec="0"/>
  </editor>
  <editor id="E_EQ">
    <range uom="56" subset="-10,10" step="1" prec="0"/>
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
        LOGGER.warning(f"Jishi GET {url} failed: {e}")
        return None


def _jishi_cmd(base_url, path, timeout=5):
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return True
    except Exception as e:
        LOGGER.warning(f"Jishi command {url} failed: {e}")
        return False


def _enc(s):
    from urllib.parse import quote
    return quote(s, safe='')


def _zone_address(zone_name):
    addr = re.sub(r'[^a-z0-9]', '', zone_name.lower())
    return addr[:14]


def _subset(lst):
    return ','.join(str(i) for i in range(len(lst))) if lst else '0'


def _write_profile_files(favorites, playlists, tts_phrases, zone_names):
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

  <!-- Dynamic — Zones (for Join command) -->
  <editor id="E_ZONES">
    <range uom="25" subset="{_subset(zone_names)}" nls="CUST_ZONE"/>
  </editor>
</editors>
"""
    with open(os.path.join(_PROFILE_DIR, 'editor', 'editors.xml'), 'w') as f:
        f.write(editors_xml)

    LOGGER.info(f"Profile updated: {len(favorites)} favs, {len(playlists)} playlists, "
                f"{len(tts_phrases)} TTS, {len(zone_names)} zones")


# ---------------------------------------------------------------------------
# Speaker Node
# ---------------------------------------------------------------------------

class SpeakerNode(udi_interface.Node):
    """One node per Jishi zone (stereo pairs already merged by Jishi)."""

    id = 'sonos-speaker'

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
        {'driver': 'GV12', 'value': 0, 'uom': 56},  # Members in group
    ]

    commands = {
        'DON':           'cmd_play',
        'DOF':           'cmd_pause',
        'PLAY_PAUSE':    'cmd_playpause',
        'STOP':          'cmd_stop',
        'NEXT':          'cmd_next',
        'PREV':          'cmd_prev',
        'SET_VOL':       'cmd_set_vol',
        'VOL_UP':        'cmd_vol_up',
        'VOL_DOWN':      'cmd_vol_down',
        'SET_GRP_VOL':   'cmd_set_group_vol',
        'SET_BASS':      'cmd_set_bass',
        'SET_TREBLE':    'cmd_set_treble',
        'MUTE':          'cmd_mute',
        'UNMUTE':        'cmd_unmute',
        'SHUFFLE_ON':    'cmd_shuffle_on',
        'SHUFFLE_OFF':   'cmd_shuffle_off',
        'REPEAT':        'cmd_repeat',
        'CROSSFADE_ON':  'cmd_crossfade_on',
        'CROSSFADE_OFF': 'cmd_crossfade_off',
        'PLAY_FAVORITE': 'cmd_play_favorite',
        'PLAY_PLAYLIST': 'cmd_play_playlist',
        'SAY':           'cmd_say',
        'SLEEP':         'cmd_sleep',
        'JOIN':          'cmd_join',
        'LEAVE':         'cmd_leave',
        'PARTY':         'cmd_party',
        'QUERY':         'query',
    }

    def __init__(self, polyglot, primary, address, name, zone_name, jishi_url, controller):
        super().__init__(polyglot, primary, address, name)
        self.zone_name = zone_name
        self.jishi_url = jishi_url
        self._ctrl = controller
        self._zp = _enc(zone_name)

    def _cmd(self, path):
        return _jishi_cmd(self.jishi_url, f"/{self._zp}/{path}")

    def update_from_state(self, state):
        pb = state.get('playbackState', 'STOPPED')
        self.setDriver('ST', PLAYBACK_MAP.get(pb, 0))
        self.setDriver('SVOL', state.get('volume', 0))

        eq = state.get('equalizer', {})
        self.setDriver('GV2', eq.get('bass', 0))
        self.setDriver('GV3', eq.get('treble', 0))
        self.setDriver('GV4', 1 if state.get('mute', False) else 0)
        self.setDriver('GV9', 1 if eq.get('loudness', False) else 0)
        self.setDriver('GV10', 1 if eq.get('nightMode', False) else 0)
        self.setDriver('GV11', 1 if eq.get('speechEnhancement', False) else 0)

        pm = state.get('playMode', {})
        self.setDriver('GV6', 1 if pm.get('shuffle', False) else 0)
        self.setDriver('GV7', REPEAT_MAP.get(pm.get('repeat', 'none'), 0))
        self.setDriver('GV8', 1 if pm.get('crossfade', False) else 0)

        members = state.get('members', [])
        self.setDriver('GV12', len(members) if members else 1)

        track = state.get('currentTrack', {})
        title  = track.get('title', '') or track.get('stationName', '')
        artist = track.get('artist', '')
        LOGGER.debug(f"{self.zone_name}: {pb} | {artist} - {title}")

    def update_group_state(self, group_state):
        if group_state:
            self.setDriver('GV1', group_state.get('volume', 0))
            self.setDriver('GV5', 1 if group_state.get('mute', False) else 0)

    def query(self, command=None):
        data = _jishi_get(self.jishi_url, f"/{self._zp}/state")
        if data:
            self.update_from_state(data)
            self.reportDrivers()

    # --- Transport ---
    def cmd_play(self, command):      self._cmd('play')
    def cmd_pause(self, command):     self._cmd('pause')
    def cmd_playpause(self, command): self._cmd('playpause')
    def cmd_stop(self, command):      self._cmd('stop')
    def cmd_next(self, command):      self._cmd('next')
    def cmd_prev(self, command):      self._cmd('previous')

    # --- Volume ---
    def cmd_set_vol(self, command):
        self._cmd(f"volume/{int(command.get('value', 0))}")
    def cmd_vol_up(self, command):   self._cmd('volume/+2')
    def cmd_vol_down(self, command): self._cmd('volume/-2')
    def cmd_set_group_vol(self, command):
        self._cmd(f"groupVolume/{int(command.get('value', 0))}")

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
        mode = modes.get(int(command.get('value', 0)), 'none')
        # Jishi repeat only supports on/off — map none/all → off, one → on
        self._cmd(f"repeat/{'off' if mode == 'none' else 'on'}")

    def cmd_crossfade_on(self, command):  self._cmd('crossfade/on')
    def cmd_crossfade_off(self, command): self._cmd('crossfade/off')

    # --- Content (0-based index matches NLS CUST_FAV-N etc.) ---
    def cmd_play_favorite(self, command):
        idx = int(command.get('value', 0))
        favs = self._ctrl.favorites
        if idx < len(favs):
            self._cmd(f"favorite/{_enc(favs[idx])}")
        else:
            LOGGER.warning(f"{self.zone_name}: favorite index {idx} out of range")

    def cmd_play_playlist(self, command):
        idx = int(command.get('value', 0))
        pls = self._ctrl.playlists
        if idx < len(pls):
            self._cmd(f"playlist/{_enc(pls[idx])}")
        else:
            LOGGER.warning(f"{self.zone_name}: playlist index {idx} out of range")

    def cmd_say(self, command):
        idx = int(command.get('value', 0))
        tts = self._ctrl.tts_phrases
        if idx < len(tts):
            self._cmd(f"say/{_enc(tts[idx])}")
        else:
            LOGGER.warning(f"{self.zone_name}: TTS index {idx} not configured")

    def cmd_sleep(self, command):
        minutes = int(command.get('value', 0))
        self._cmd('sleep/off' if minutes == 0 else f"sleep/{minutes * 60}")

    # --- Grouping ---
    def cmd_join(self, command):
        idx = int(command.get('value', 0))
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
        for name in self._ctrl.zone_names:
            if name != self.zone_name:
                _jishi_cmd(self.jishi_url, f"/{_enc(name)}/join/{self._zp}")


# ---------------------------------------------------------------------------
# Controller Node
# ---------------------------------------------------------------------------

class Controller(udi_interface.Node):

    id = 'sonos-controller'

    drivers = [
        {'driver': 'ST', 'value': 0, 'uom': 2},
    ]

    commands = {
        'DISCOVER':        'discover',
        'PAUSE_ALL':       'cmd_pause_all',
        'RESUME_ALL':      'cmd_resume_all',
        'UNGROUP_ALL':     'cmd_ungroup_all',
        'PARTY':           'cmd_party_all',
        'SAY_ALL':         'cmd_say_all',
        'REFRESH_CONTENT': 'cmd_refresh_content',
        'QUERY':           'query',
    }

    def __init__(self, polyglot, primary, address, name):
        super().__init__(polyglot, primary, address, name)
        self.poly = polyglot
        self._speakers = {}       # address -> SpeakerNode
        self._jishi_url = ''
        self.favorites = []
        self.playlists = []
        self.tts_phrases = []
        self.zone_names = []      # ordered list of zone room names for JOIN
        self._poll_lock = threading.Lock()
        self._initialized = False

        polyglot.subscribe(polyglot.START,        self.start)
        polyglot.subscribe(polyglot.CUSTOMPARAMS, self.param_handler)
        polyglot.subscribe(polyglot.POLL,         self.poll)
        polyglot.subscribe(polyglot.STOP,         self.stop)

        polyglot.ready()
        polyglot.addNode(self, conn_status='ST')

    def start(self):
        LOGGER.info('Sonos Jishi NodeServer starting')
        self.setDriver('ST', 1)
        if not self._initialized:
            self.discover()

    def stop(self):
        LOGGER.info('Sonos Jishi NodeServer stopping')
        self.setDriver('ST', 0)

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
        LOGGER.info(f"Jishi URL: {self._jishi_url}, TTS: {self.tts_phrases}")
        self._initialized = False
        self.discover()

    def discover(self, command=None):
        if not self._jishi_url:
            LOGGER.warning('No jishi_url configured — skipping discover')
            return

        zones = _jishi_get(self._jishi_url, '/zones')
        if zones is None:
            self.poly.Notices['jishi'] = f"Cannot reach Jishi at {self._jishi_url}"
            return

        self.poly.Notices.clear()
        self._initialized = True

        # Collect zone names for JOIN support
        new_zone_names = []
        for zone in zones:
            name = (zone.get('coordinator', {}).get('roomName')
                    or zone.get('roomName', ''))
            if name:
                new_zone_names.append(name)
        self.zone_names = new_zone_names

        # Fetch content lists and update profile
        self._refresh_content(force=True)

        # Add/update speaker nodes
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

            coordinator = zone.get('coordinator', zone)
            self._speakers[address].update_from_state(coordinator)
            self._speakers[address].update_group_state(zone.get('groupState', {}))

        LOGGER.info(f"Discovery complete — {len(self._speakers)} zones")

    def _refresh_content(self, force=False):
        """Fetch favorites/playlists from Jishi; update ISY profile if changed."""
        new_favs = _jishi_get(self._jishi_url, '/favorites') or []
        new_pls  = _jishi_get(self._jishi_url, '/playlists') or []
        if not isinstance(new_favs, list): new_favs = []
        if not isinstance(new_pls, list):  new_pls = []

        changed = force or new_favs != self.favorites or new_pls != self.playlists
        self.favorites = new_favs
        self.playlists = new_pls

        if changed:
            _write_profile_files(
                self.favorites, self.playlists,
                self.tts_phrases, self.zone_names)
            self.poly.updateProfile()

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
            zone_name = (zone.get('coordinator', {}).get('roomName')
                         or zone.get('roomName', ''))
            if not zone_name:
                continue
            address = _zone_address(zone_name)
            if address in self._speakers:
                self._speakers[address].update_from_state(zone.get('coordinator', zone))
                self._speakers[address].update_group_state(zone.get('groupState', {}))

    def _long_poll(self):
        LOGGER.debug('Long poll: refreshing content lists')
        self._refresh_content()

    # --- Global commands ---
    def cmd_pause_all(self, command):
        _jishi_cmd(self._jishi_url, '/pauseall')

    def cmd_resume_all(self, command):
        _jishi_cmd(self._jishi_url, '/resumeall')

    def cmd_ungroup_all(self, command):
        for name in self.zone_names:
            _jishi_cmd(self._jishi_url, f"/{_enc(name)}/leave")

    def cmd_party_all(self, command):
        """Join all zones to the first zone (party mode)."""
        if not self.zone_names:
            return
        host = self.zone_names[0]
        for name in self.zone_names[1:]:
            _jishi_cmd(self._jishi_url, f"/{_enc(name)}/join/{_enc(host)}")

    def cmd_say_all(self, command):
        """Say a TTS phrase on all speakers."""
        idx = int(command.get('value', 0))
        if idx < len(self.tts_phrases):
            phrase = self.tts_phrases[idx]
            _jishi_cmd(self._jishi_url, f"/sayall/{_enc(phrase)}")
        else:
            LOGGER.warning(f"SAY_ALL: TTS index {idx} not configured")

    def cmd_refresh_content(self, command):
        """Manually trigger an immediate content refresh (favorites, playlists, TTS)."""
        LOGGER.info('Manual content refresh triggered')
        self._refresh_content(force=True)

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
