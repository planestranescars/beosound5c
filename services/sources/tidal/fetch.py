"""
BeoSound 5c — Tidal playlist/track fetcher.

Can be run as a standalone script (called as a subprocess from service.py)
or imported directly.

Output JSON format matches the Spotify playlists file so tidal.html can
reuse the same ArcList config:

  [
    {
      "id":     "<playlist-id>",
      "name":   "Playlist Name",
      "image":  "https://...",
      "tracks": [
        { "id": "<track-id>", "name": "Title", "artist": "Artist", "image": "...", "uri": "<track-id>" },
        ...
      ]
    },
    ...
  ]
"""

import json
import logging
import os
import sys

log = logging.getLogger('beo-tidal.fetch')

_IMAGE_SIZE = 320   # pixel dimension for tidalapi .image() calls


# ── helpers ──────────────────────────────────────────────────────────────────

def _cover_url(obj) -> str:
    """Return a cover-art URL for any tidalapi media object, or empty string."""
    for size in (_IMAGE_SIZE, 160, 80):
        try:
            url = obj.image(size)
            if url:
                return url
        except Exception:
            continue
    return ''


def _track_dict(track) -> dict:
    return {
        'id':     str(track.id),
        'name':   track.name,
        'artist': track.artist.name if track.artist else '',
        'image':  _cover_url(track.album) if track.album else '',
        'uri':    str(track.id),   # used by tidal.html command payload
    }


# ── public API ───────────────────────────────────────────────────────────────

def fetch_playlists(session) -> list:
    """Return the user's playlists (with tracks) as a list of dicts."""
    result = []
    try:
        playlists = session.user.playlists()
        log.info("Fetching %d playlists…", len(playlists))
        for pl in playlists:
            tracks = []
            try:
                for track in pl.tracks():
                    tracks.append(_track_dict(track))
            except Exception as e:
                log.warning("Could not fetch tracks for '%s': %s", pl.name, e)
            result.append({
                'id':     str(pl.id),
                'name':   pl.name,
                'image':  _cover_url(pl),
                'tracks': tracks,
            })
    except Exception as e:
        log.error("fetch_playlists failed: %s", e)
    return result


def fetch_favorites(session) -> dict | None:
    """Return the user's liked tracks as a synthetic 'My Tracks' playlist."""
    try:
        tracks = []
        for track in session.user.favorites.tracks():
            tracks.append(_track_dict(track))
        if tracks:
            return {
                'id':     'favorites',
                'name':   'My Tracks',
                'image':  '',
                'tracks': tracks,
            }
    except Exception as e:
        log.warning("Could not fetch favorite tracks: %s", e)
    return None


def fetch_all(session) -> list:
    """Fetch playlists + favorites. Favorites prepended if non-empty."""
    data = fetch_playlists(session)
    fav  = fetch_favorites(session)
    if fav:
        data.insert(0, fav)
    return data


# ── CLI entry-point (called as subprocess from service.py) ───────────────────

if __name__ == '__main__':
    import argparse

    logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')

    parser = argparse.ArgumentParser(description='Fetch Tidal playlists to JSON')
    parser.add_argument('--output', default='-',
                        help='Output file path (- = stdout)')
    args = parser.parse_args()

    # Locate token file (same env-var logic as auth.py)
    import pathlib as _pl
    _default_dir = (
        _pl.Path.home() / '.config' / 'beosound5c'
        if sys.platform == 'win32'
        else _pl.Path('/etc/beosound5c')
    )
    TOKEN_FILE = _pl.Path(os.getenv('BS5C_CONFIG_DIR', str(_default_dir))) / 'tidal_session.json'

    try:
        import tidalapi
        session = tidalapi.Session()
        session.load_session_from_file(TOKEN_FILE)
        if not session.check_login():
            print("ERROR: Tidal session is no longer valid", file=sys.stderr)
            sys.exit(1)
    except FileNotFoundError:
        print(f"ERROR: Token file not found: {TOKEN_FILE}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Could not load Tidal session: {e}", file=sys.stderr)
        sys.exit(1)

    data = fetch_all(session)

    if args.output == '-':
        json.dump(data, sys.stdout, indent=2)
        print()   # trailing newline
    else:
        os.makedirs(os.path.dirname(args.output), exist_ok=True)
        with open(args.output, 'w') as f:
            json.dump(data, f, indent=2)
        total_tracks = sum(len(p.get('tracks', [])) for p in data)
        log.info("Wrote %d playlists (%d tracks) to %s",
                 len(data), total_tracks, args.output)
