#!/usr/bin/env python3
"""
Sonos Polyglot v3 NodeServer - Jishi backend
Polls the node-sonos-http-api (Jishi) server for all zone state.
No UPnP subscriptions required — works across VLANs.
"""

import udi_interface
import sys
import time
import threading
import requests

LOGGER = udi_interface.LOGGER


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PLAYBACK_MAP = {
    'STOPPED':      0,
    'PLAYING':      1,
    'PAUSED_PLAYBACK': 2,
    'TRANSITIONING': 3,
}

REPEAT_MAP = {
    'none': 0,
    'one':  1,
    'all':  2,
}


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


def _zone_name_to_path(zone_name):
    """URL-encode a zone name for Jishi paths."""
    from urllib.parse import quote
    return quote(zone_name, safe='')


# ---------------------------------------------------------------------------
# Speaker Node
# ---------------------------------------------------------------------------

class SpeakerNode(udi_interface.Node):
    """One node per Jishi zone (stereo pairs already merged by Jishi)."""

    id = 'sonos-speaker'

    drivers = [
        {'driver': 'ST',   'value': 0, 'uom': 25},   # Playback state
        {'driver': 'SVOL', 'value': 0, 'uom': 51},   # Volume
        {'driver': 'GV1',  'value': 0, 'uom': 56},   # Bass
        {'driver': 'GV2',  'value': 0, 'uom': 56},   # Treble
        {'driver': 'GV3',  'value': 0, 'uom': 2},    # Mute
        {'driver': 'GV4',  'value': 0, 'uom': 2},    # Shuffle
        {'driver': 'GV5',  'value': 0, 'uom': 25},   # Repeat
        {'driver': 'GV6',  'value': 0, 'uom': 2},    # Crossfade
        {'driver': 'GV7',  'value': 0, 'uom': 2},    # Loudness
        {'driver': 'GV8',  'value': 0, 'uom': 25},   # Track title (display only)
        {'driver': 'GV9',  'value': 0, 'uom': 25},   # Artist (display only)
        {'driver': 'GV10', 'value': 0, 'uom': 25},   # Album/Station (display only)
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

    def __init__(self, polyglot, primary, address, name, zone_name, jishi_url,
                 favorites, playlists, tts_phrases):
        super().__init__(polyglot, primary, address, name)
        self.zone_name = zone_name
        self.jishi_url = jishi_url
        self.favorites = favorites      # list of favorite name strings
        self.playlists = playlists      # list of playlist name strings
        self.tts_phrases = tts_phrases  # list of TTS phrase strings
        self._zone_path = _zone_name_to_path(zone_name)

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

        # Text fields — ISY can't display strings natively so we log them;
        # GV8/9/10 are left as 0 (index) but the values are visible in the
        # node's NLS label when ISY Admin Console shows the driver name.
        track = state.get('currentTrack', {})
        title = track.get('title', '')
        artist = track.get('artist', '')
        # Use stationName if it's a radio stream with no track title
        if not title:
            title = track.get('stationName', '')
        album = track.get('album', '')
        if not album:
            album = track.get('stationName', '')

        LOGGER.debug(f"{self.zone_name}: {pb_raw} | {artist} - {title} | {album}")

    def query(self, command=None):
        state = _jishi_get(self.jishi_url, f"/{self._zone_path}/state")
        if state:
            self.update_from_state(state)
            self.reportDrivers()

    # --- Transport ---

    def cmd_play(self, command):
        self._cmd('play')

    def cmd_pause(self, command):
        self._cmd('pause')

    def cmd_stop(self, command):
        self._cmd('stop')

    def cmd_next(self, command):
        self._cmd('next')

    def cmd_prev(self, command):
        self._cmd('previous')

    # --- Volume ---

    def cmd_set_vol(self, command):
        val = int(command.get('value', 0))
        self._cmd(f"volume/{val}")

    def cmd_vol_up(self, command):
        self._cmd('volume/+2')

    def cmd_vol_down(self, command):
        self._cmd('volume/-2')

    # --- EQ ---

    def cmd_set_bass(self, command):
        val = int(command.get('value', 0))
        self._cmd(f"bass/{val}")

    def cmd_set_treble(self, command):
        val = int(command.get('value', 0))
        self._cmd(f"treble/{val}")

    # --- Mute ---

    def cmd_mute(self, command):
        self._cmd('mute')

    def cmd_unmute(self, command):
        self._cmd('unmute')

    # --- Play modes ---

    def cmd_shuffle_on(self, command):
        self._cmd('shuffle/on')

    def cmd_shuffle_off(self, command):
        self._cmd('shuffle/off')

    def cmd_repeat(self, command):
        val = int(command.get('value', 0))
        modes = {0: 'none', 1: 'one', 2: 'all'}
        mode = modes.get(val, 'none')
        self._cmd(f"repeat/{mode}")

    def cmd_crossfade(self, command):
        self._cmd('crossfade/toggle')

    # --- Content ---

    def cmd_play_favorite(self, command):
        slot = int(command.get('value', 1)) - 1  # 1-based -> 0-based index
        if slot < len(self.favorites):
            from urllib.parse import quote
            name = quote(self.favorites[slot], safe='')
            self._cmd(f"favorite/{name}")
        else:
            LOGGER.warning(f"{self.zone_name}: favorite slot {slot+1} not configured")

    def cmd_play_playlist(self, command):
        slot = int(command.get('value', 1)) - 1
        if slot < len(self.playlists):
            from urllib.parse import quote
            name = quote(self.playlists[slot], safe='')
            self._cmd(f"playlist/{name}")
        else:
            LOGGER.warning(f"{self.zone_name}: playlist slot {slot+1} not configured")

    def cmd_say(self, command):
        slot = int(command.get('value', 1)) - 1
        if slot < len(self.tts_phrases):
            from urllib.parse import quote
            phrase = quote(self.tts_phrases[slot], safe='')
            self._cmd(f"say/{phrase}")
        else:
            LOGGER.warning(f"{self.zone_name}: TTS slot {slot+1} not configured")

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
        self._speakers = {}          # address -> SpeakerNode
        self._jishi_url = ''
        self._favorites = []
        self._playlists = []
        self._tts_phrases = []
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
        """Called when custom params change. Only re-init once per change set."""
        self.poly.Notices.clear()

        jishi_url = params.get('jishi_url', 'http://localhost:5005').rstrip('/')
        if not jishi_url:
            self.poly.Notices['config'] = 'Set jishi_url custom parameter (e.g. http://zeus:5005)'
            return

        self._jishi_url = jishi_url

        # Favorites: favorite_1 ... favorite_10
        self._favorites = []
        for i in range(1, 11):
            v = params.get(f'favorite_{i}', '').strip()
            if v:
                self._favorites.append(v)

        # Playlists: playlist_1 ... playlist_10
        self._playlists = []
        for i in range(1, 11):
            v = params.get(f'playlist_{i}', '').strip()
            if v:
                self._playlists.append(v)

        # TTS phrases: tts_1 ... tts_10
        self._tts_phrases = []
        for i in range(1, 11):
            v = params.get(f'tts_{i}', '').strip()
            if v:
                self._tts_phrases.append(v)

        LOGGER.info(f"Jishi URL: {self._jishi_url}")
        LOGGER.info(f"Favorites: {self._favorites}")
        LOGGER.info(f"Playlists: {self._playlists}")
        LOGGER.info(f"TTS phrases: {self._tts_phrases}")

        # Re-run discovery when config changes
        self._initialized = False
        self.discover()

    # --- Discovery ---

    def discover(self, command=None):
        """Create speaker nodes for all zones reported by Jishi."""
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

        for zone in zones:
            zone_name = zone.get('coordinator', {}).get('roomName') or zone.get('roomName', '')
            if not zone_name:
                continue

            address = self._zone_address(zone_name)
            name = zone_name

            if address not in self._speakers:
                LOGGER.info(f"Adding speaker node: {name} ({address})")
                node = SpeakerNode(
                    self.poly,
                    self.address,
                    address,
                    name,
                    zone_name,
                    self._jishi_url,
                    self._favorites,
                    self._playlists,
                    self._tts_phrases,
                )
                self.poly.addNode(node)
                self._speakers[address] = node

            # Update state immediately on discovery
            state = zone.get('coordinator', zone)
            self._speakers[address].update_from_state(state)

        LOGGER.info(f"Discovery complete — {len(self._speakers)} zones")

    @staticmethod
    def _zone_address(zone_name):
        """Convert zone name to a valid ISY node address (<=14 chars, lowercase, alphanumeric)."""
        import re
        addr = re.sub(r'[^a-z0-9]', '', zone_name.lower())
        return addr[:14]

    # --- Polling ---

    def poll(self, flag):
        """Called by PG3 on shortPoll and longPoll intervals."""
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
        """Fetch all zone states in a single /zones call."""
        zones = _jishi_get(self._jishi_url, '/zones')
        if zones is None:
            LOGGER.warning('Short poll: could not reach Jishi')
            return

        for zone in zones:
            zone_name = zone.get('coordinator', {}).get('roomName') or zone.get('roomName', '')
            if not zone_name:
                continue
            address = self._zone_address(zone_name)
            if address in self._speakers:
                state = zone.get('coordinator', zone)
                self._speakers[address].update_from_state(state)

    def _long_poll(self):
        """Refresh favorites/playlists in case they changed on the Sonos system."""
        LOGGER.debug('Long poll: refreshing favorites and playlists from Jishi')
        favs = _jishi_get(self._jishi_url, '/favorites')
        if favs and not self._favorites:
            # Only auto-populate if user hasn't configured manual slots
            self._favorites = favs
        playlists = _jishi_get(self._jishi_url, '/playlists')
        if playlists and not self._playlists:
            self._playlists = playlists

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
