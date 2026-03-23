# Sonos Jishi Configuration

## Required

**jishi_url**
The base URL of your node-sonos-http-api (Jishi) server.
Example: `http://zeus:5005`

## Optional — Favorites (slots 1–10)

Set `favorite_1` through `favorite_10` to the exact names of Sonos favorites
as they appear in your Sonos app. These map to the Play Favorite command slots.

Example:
- `favorite_1` = `96.9 | 97 LAV-FM (Classic Rock)`
- `favorite_2` = `A Prairie Home Companion 24/7`

If left blank, the nodeserver will auto-populate from Jishi on long poll.

## Optional — Playlists (slots 1–10)

Set `playlist_1` through `playlist_10` to the exact names of Sonos playlists.

Example:
- `playlist_1` = `White Noise`
- `playlist_2` = `Campfires`

## Optional — TTS Phrases (slots 1–10)

Set `tts_1` through `tts_10` to phrases you want to speak via the SAY command.
Jishi uses Google TTS to speak these on the selected speaker.

Example:
- `tts_1` = `Dinner is ready`
- `tts_2` = `Time to wake up`
