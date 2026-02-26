#!/usr/bin/env python3
"""
BeoSound 5c Tidal Source (beo-tidal)

Provides Tidal playback via the tidalapi library with OAuth device-code
authentication.  Audio plays locally through mpv — no Sonos/BlueSound
player service is required, though the player service will be used
automatically if available and the track URL is accessible.

Port: 8772
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

from aiohttp import web

# ── Sibling imports ───────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from auth import TidalAuth
from fetch import fetch_all

# ── Shared library ────────────────────────────────────────────────────────────
sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)
from lib.source_base import SourceBase

logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
log = logging.getLogger('beo-tidal')

# ── Constants ─────────────────────────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
PLAYLISTS_FILE = os.path.join(
    os.getenv('BS5C_BASE_PATH', PROJECT_ROOT),
    'web', 'json', 'tidal_playlists.json',
)
FETCH_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fetch.py')

PLAYLIST_REFRESH_COOLDOWN = 5 * 60   # seconds between background syncs
NIGHTLY_REFRESH_HOUR      = 3        # 3 am


# ── Service ───────────────────────────────────────────────────────────────────

def _is_port_open(host, port, timeout=0.5):
    """Sync TCP probe - avoids Windows IOCP CancelledError from aiohttp to closed port."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class TidalService(SourceBase):
    """Main Tidal source — registers with router, drives the arc UI."""

    id   = 'tidal'
    name = 'Tidal'
    port = 8772
    player = 'local'
    action_map = {
        'play':  'toggle',
        'pause': 'toggle',
        'go':    'toggle',
        'next':  'next',
        'prev':  'prev',
        'right': 'next',
        'left':  'prev',
        'stop':  'stop',
        'up':    'next',
        'down':  'prev',
    }

    def __init__(self):
        super().__init__()
        self.auth      = TidalAuth()
        self.playlists = []
        self.state     = 'stopped'    # stopped | playing | paused
        self.now_playing = None       # {title, artist, image}

        # Playback state
        self._mpv_process   = None
        self._watcher_task  = None
        self._current_queue = []
        self._current_idx   = 0

        # Background tasks
        self._refresh_task  = None
        self._nightly_task  = None
        self._login_task    = None

        # Refresh bookkeeping
        self._fetching              = False
        self._last_refresh          = 0      # monotonic
        self._last_refresh_wall     = None
        self._last_refresh_duration = None

    # ── SourceBase lifecycle ──────────────────────────────────────────────────

    async def on_start(self):
        has_session = self.auth.load()
        if has_session:
            self._load_playlists()
            self._refresh_task = asyncio.create_task(self._delayed_refresh(10))
            self._nightly_task = asyncio.create_task(self._nightly_refresh_loop())
        else:
            log.info('No Tidal session — waiting for OAuth via /setup')

        # Only register if router is listening - avoids Windows IOCP CancelledError
        if _is_port_open('127.0.0.1', 8770):
            asyncio.create_task(self._register_ignoring_errors('available'))
        else:
            log.info('Router not available - running in standalone mode')

    async def _register_ignoring_errors(self, state: str):
        try:
            await self.register(state)
        except BaseException as e:
            log.warning('Could not register with router (running standalone?): %s', e)

    async def on_stop(self):
        await self._stop_playback()
        for task in (self._refresh_task, self._nightly_task, self._login_task):
            if task:
                task.cancel()
                try:
                    await task
                except Exception:
                    pass
        await self.register('gone')

    # ── SourceBase hooks ──────────────────────────────────────────────────────

    def add_routes(self, app):
        app.router.add_get( '/playlists',  self._handle_playlists)
        app.router.add_get( '/setup',      self._handle_setup)
        app.router.add_post('/start-auth', self._handle_start_auth)
        app.router.add_post('/logout',     self._handle_logout)

    async def handle_status(self) -> dict:
        return {
            'state':                  self.state,
            'now_playing':            self.now_playing,
            'playlist_count':         len(self.playlists),
            'has_credentials':        self.auth.is_authenticated,
            'fetching':               self._fetching,
            'last_refresh':           self._last_refresh_wall.isoformat()
                                      if self._last_refresh_wall else None,
            'last_refresh_duration':  self._last_refresh_duration,
        }

    async def handle_resync(self) -> dict:
        state = self.state if self.state in ('playing', 'paused') else 'available'
        await self.register(state)
        await self._broadcast_update()
        return {'status': 'ok', 'resynced': True}

    async def handle_command(self, cmd: str, data: dict) -> dict:
        if cmd == 'play_playlist':
            await self._play_playlist(
                data.get('playlist_id', ''),
                data.get('track_index'),
            )

        elif cmd == 'play_track':
            track_id = data.get('uri') or data.get('track_id', '')
            await self._play_track(track_id)

        elif cmd == 'toggle':
            await self._toggle()

        elif cmd == 'next':
            await self._skip(+1)

        elif cmd == 'prev':
            await self._skip(-1)

        elif cmd == 'stop':
            await self._stop_playback()
            self.state = 'stopped'
            self.now_playing = None
            await self.register('available')
            await self._broadcast_update()

        elif cmd == 'refresh_playlists':
            asyncio.create_task(self._refresh_playlists())

        elif cmd == 'logout':
            await self._logout()

        else:
            return {'status': 'error', 'message': f'Unknown command: {cmd}'}

        return {'state': self.state}

    # ── Playlist persistence ──────────────────────────────────────────────────

    def _load_playlists(self):
        try:
            with open(PLAYLISTS_FILE) as f:
                self.playlists = json.load(f)
            log.info('Loaded %d playlists from disk', len(self.playlists))
        except (FileNotFoundError, json.JSONDecodeError) as e:
            log.warning('Could not load playlists from disk: %s', e)
            self.playlists = []

    # ── Playback ──────────────────────────────────────────────────────────────

    async def _play_playlist(self, playlist_id: str, track_index=None):
        playlist = next(
            (p for p in self.playlists if p['id'] == playlist_id), None
        )
        if not playlist:
            log.warning('Playlist %s not found', playlist_id)
            return

        tracks = playlist.get('tracks', [])
        if not tracks:
            log.warning("Playlist '%s' has no tracks", playlist['name'])
            return

        idx = track_index if track_index is not None else 0
        idx = max(0, min(idx, len(tracks) - 1))

        self._current_queue = tracks
        self._current_idx   = idx
        await self._play_track(tracks[idx]['id'])

    async def _play_track(self, track_id: str):
        await self._stop_playback()

        # Resolve the stream URL in a thread (tidalapi is synchronous)
        try:
            loop = asyncio.get_running_loop()
            url  = await loop.run_in_executor(
                None, self._get_track_url_blocking, track_id
            )
        except Exception as e:
            log.error('get_track_url failed: %s', e)
            return

        if not url:
            log.error('No stream URL for track %s', track_id)
            return

        # Find metadata in the current queue
        track_meta = next(
            (t for t in self._current_queue if str(t.get('id')) == str(track_id)),
            None,
        )

        # Launch mpv
        env = os.environ.copy()
        env.setdefault('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')
        self._mpv_process = subprocess.Popen(
            ['mpv', '--ao=pulse', '--no-video', '--no-terminal', url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )

        self.state = 'playing'
        self.now_playing = {
            'title':  track_meta.get('name', '') if track_meta else '',
            'artist': track_meta.get('artist', '') if track_meta else '',
            'image':  track_meta.get('image', '') if track_meta else '',
        }
        await self.register('playing')
        await self._broadcast_update()

        # Broadcast now-playing metadata to media WebSocket (cover art etc.)
        if track_meta:
            await self.broadcast('media_update', {
                'title':   track_meta.get('name', ''),
                'artist':  track_meta.get('artist', ''),
                'artwork': track_meta.get('image', ''),
                'state':   'PLAYING',
            })

        self._watcher_task = asyncio.create_task(self._watch_playback())
        log.info("Playing: %s — %s",
                 self.now_playing.get('artist', '?'),
                 self.now_playing.get('title', '?'))

    def _get_track_url_blocking(self, track_id: str) -> str:
        """Synchronous: ask tidalapi for the track's stream URL."""
        try:
            track = self.auth.session.track(int(track_id))
            url   = track.get_url()
            return url
        except Exception as e:
            log.error('track.get_url(%s) raised: %s', track_id, e)
            return ''

    async def _toggle(self):
        if self.state == 'playing':
            # mpv subprocess doesn't easily pause — stop and remember position
            await self._stop_playback()
            self.state = 'paused'
            await self.register('paused')
            await self._broadcast_update()
        elif self.state == 'paused' and self._current_queue:
            await self._play_track(self._current_queue[self._current_idx]['id'])
        elif self.state == 'stopped' and self.playlists:
            await self._play_playlist(self.playlists[0]['id'])

    async def _skip(self, delta: int):
        if not self._current_queue:
            return
        self._current_idx = (
            (self._current_idx + delta) % len(self._current_queue)
        )
        await self._play_track(self._current_queue[self._current_idx]['id'])

    async def _stop_playback(self):
        if self._watcher_task:
            self._watcher_task.cancel()
            self._watcher_task = None
        if self._mpv_process:
            try:
                self._mpv_process.terminate()
                self._mpv_process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._mpv_process.kill()
            except Exception:
                pass
            self._mpv_process = None

    async def _watch_playback(self):
        """Wait for mpv to finish, then auto-advance to the next track."""
        try:
            while self._mpv_process and self._mpv_process.poll() is None:
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return

        if self.state != 'playing':
            return   # stopped externally

        if self._current_queue:
            next_idx = self._current_idx + 1
            if next_idx < len(self._current_queue):
                self._current_idx = next_idx
                await self._play_track(self._current_queue[self._current_idx]['id'])
                return

        # End of queue
        self._mpv_process = None
        self.state        = 'stopped'
        self.now_playing  = None
        await self.register('available')
        await self._broadcast_update()

    # ── Playlist refresh ──────────────────────────────────────────────────────

    async def _refresh_playlists(self):
        """Run fetch.py as a subprocess to sync playlists from Tidal."""
        if not self.auth.is_authenticated:
            return
        self._fetching = True
        t0 = time.monotonic()
        try:
            os.makedirs(os.path.dirname(PLAYLISTS_FILE), exist_ok=True)
            proc = await asyncio.create_subprocess_exec(
                sys.executable, FETCH_SCRIPT,
                '--output', PLAYLISTS_FILE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=120
            )
            if proc.returncode == 0:
                self._load_playlists()
                self._last_refresh          = time.monotonic()
                self._last_refresh_wall     = datetime.now()
                self._last_refresh_duration = round(time.monotonic() - t0, 1)
                log.info('Playlist refresh complete (%d playlists, %.1fs)',
                         len(self.playlists), self._last_refresh_duration)
                await self.broadcast('tidal_update', {
                    'state':          self.state,
                    'playlist_count': len(self.playlists),
                })
            else:
                err = (stdout.decode() + stderr.decode())[-300:]
                log.error('fetch.py failed (rc=%d): %s', proc.returncode, err)
        except asyncio.TimeoutError:
            log.error('Playlist refresh timed out after 120 s')
        except Exception as e:
            log.error('Playlist refresh error: %s', e)
        finally:
            self._fetching = False

    async def _delayed_refresh(self, delay: float):
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await self._refresh_playlists()
        except asyncio.CancelledError:
            return

    async def _nightly_refresh_loop(self):
        try:
            while True:
                now    = datetime.now()
                target = now.replace(
                    hour=NIGHTLY_REFRESH_HOUR, minute=0,
                    second=0, microsecond=0,
                )
                if target <= now:
                    target += timedelta(days=1)
                log.info('Next nightly Tidal refresh at %s (in %.0fh)',
                         target.strftime('%H:%M'),
                         (target - now).total_seconds() / 3600)
                await asyncio.sleep((target - now).total_seconds())
                log.info('Nightly Tidal refresh starting')
                await self._refresh_playlists()
        except asyncio.CancelledError:
            return

    # ── Logout ────────────────────────────────────────────────────────────────

    async def _logout(self):
        log.info('Logging out of Tidal')
        await self._stop_playback()
        for task in (self._refresh_task, self._nightly_task, self._login_task):
            if task:
                task.cancel()
        self._refresh_task = self._nightly_task = self._login_task = None
        self.auth.logout()
        self.playlists    = []
        self.state        = 'stopped'
        self.now_playing  = None
        try:
            if os.path.exists(PLAYLISTS_FILE):
                os.unlink(PLAYLISTS_FILE)
        except Exception:
            pass
        await self.register('available')
        await self._broadcast_update()

    # ── Broadcast helper ──────────────────────────────────────────────────────

    async def _broadcast_update(self):
        await self.broadcast('tidal_update', {
            'state':       self.state,
            'now_playing': self.now_playing,
        })

    # ── Extra HTTP routes ─────────────────────────────────────────────────────

    async def _handle_playlists(self, request):
        if not self.auth.is_authenticated:
            local_ip  = self._get_local_ip()
            setup_url = self.auth.oauth_url or f'http://{local_ip}:{self.port}/setup'
            return web.json_response(
                {'setup_needed': True, 'setup_url': setup_url},
                headers=self._cors_headers(),
            )

        if self._fetching and not self.playlists:
            return web.json_response(
                {'loading': True}, headers=self._cors_headers()
            )

        # Trigger background sync if stale
        if (
            time.monotonic() - self._last_refresh > PLAYLIST_REFRESH_COOLDOWN
            and not self._fetching
        ):
            self._fetching = True
            log.info('Playlist view opened — refreshing in background')
            asyncio.create_task(self._refresh_playlists())

        return web.json_response(self.playlists, headers=self._cors_headers())

    async def _handle_setup(self, request):
        """Serve the Tidal OAuth setup page (opened on phone)."""
        local_ip  = self._get_local_ip()
        oauth_url = self.auth.oauth_url

        if oauth_url:
            instruction  = 'Visit the URL below on your phone to log in to Tidal.'
            qr_section   = f'''
                <div class="setup-qr" id="setup-qr"></div>
                <div class="setup-url">{oauth_url}</div>'''
            action_html  = ''
        else:
            instruction  = 'Tap the button below to start Tidal login.'
            qr_section   = ''
            action_html  = '''
                <form method="POST" action="/start-auth">
                    <button type="submit" class="submit-btn">Connect to Tidal</button>
                </form>'''

        qr_js = (
            f"""
            if (document.getElementById('setup-qr') && typeof QRCode !== 'undefined') {{
                new QRCode(document.getElementById('setup-qr'), {{
                    text: '{oauth_url}', width: 200, height: 200,
                    colorDark: '#000000', colorLight: '#ffffff'
                }});
            }}"""
            if oauth_url else ''
        )

        html = f'''<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BeoSound 5c – Tidal Setup</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Helvetica Neue',sans-serif;background:#000;color:#fff;padding:20px;line-height:1.7}}
.container{{max-width:500px;margin:0 auto}}
.header{{text-align:center;margin-bottom:30px;padding-bottom:20px;border-bottom:1px solid #333}}
h1{{font-size:24px;font-weight:300;letter-spacing:2px;margin-bottom:8px}}
.subtitle{{color:#666;font-size:14px}}
.card{{background:#111;border-radius:8px;padding:20px;margin-bottom:16px;border:1px solid #222}}
.setup-qr{{margin:16px auto;background:#fff;padding:12px;border-radius:8px;display:inline-block}}
.setup-url{{font-family:monospace;font-size:13px;color:#999;word-break:break-all;margin-top:8px}}
p{{color:#999;font-size:14px;margin-bottom:12px}}
.submit-btn{{display:block;width:100%;padding:14px;margin-top:16px;
             background:#00FFFF;border:none;border-radius:4px;
             color:#000;font-size:16px;font-weight:600;cursor:pointer;text-align:center}}
.submit-btn:hover{{background:#00D4D4}}
</style>
</head><body>
<div class="container">
  <div class="header">
    <h1>TIDAL SETUP</h1>
    <div class="subtitle">BeoSound 5c</div>
  </div>
  <div class="card">
    <p>{instruction}</p>
    {qr_section}
    {action_html}
  </div>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js" onerror="void 0"></script>
<script>
{qr_js}
// Poll until auth completes, then reload
const _poll = setInterval(async () => {{
    try {{
        const r = await fetch('/playlists');
        const d = await r.json();
        if (!d.setup_needed) {{ clearInterval(_poll); location.reload(); }}
        if (d.loading && !document.querySelector('.setup-qr')) {{
            clearInterval(_poll); location.reload();
        }}
    }} catch(e) {{}}
}}, 3000);
</script>
</body></html>'''
        return web.Response(text=html, content_type='text/html')

    async def _handle_start_auth(self, request):
        """POST /start-auth — begin the OAuth device-code flow."""
        try:
            loop    = asyncio.get_running_loop()
            oauth_url = await loop.run_in_executor(
                None, self.auth.start_oauth_flow
            )
        except Exception as e:
            log.error('start_oauth_flow failed: %s', e)
            return web.json_response({'error': str(e)}, status=500,
                                     headers=self._cors_headers())

        # Background task waits for the user to complete login
        self._login_task = asyncio.create_task(self._wait_for_oauth())

        # Redirect back to setup page so user sees the QR code immediately
        raise web.HTTPFound('/setup')

    async def _wait_for_oauth(self):
        """Background task: block an executor thread until OAuth resolves."""
        try:
            loop = asyncio.get_running_loop()
            ok   = await loop.run_in_executor(
                None, self.auth.await_login_blocking
            )
            if ok:
                log.info('OAuth complete — scheduling playlist refresh')
                self._refresh_task = asyncio.create_task(
                    self._delayed_refresh(0)
                )
                if not self._nightly_task or self._nightly_task.done():
                    self._nightly_task = asyncio.create_task(
                        self._nightly_refresh_loop()
                    )
                await self.register('available')
        except asyncio.CancelledError:
            return

    async def _handle_logout(self, request):
        await self._logout()
        return web.json_response(
            {'status': 'ok', 'message': 'Logged out from Tidal'},
            headers=self._cors_headers(),
        )

    # ── Utilities ─────────────────────────────────────────────────────────────

    @staticmethod
    def _get_local_ip() -> str:
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return '127.0.0.1'


if __name__ == '__main__':
    service = TidalService()
    asyncio.run(service.run())
