"""
BeoSound 5c — Tidal OAuth device-flow authentication.

Uses tidalapi's built-in device-code flow:
  1. call start_oauth_flow() → returns a URL for the user to visit on their phone
  2. await_login_blocking() blocks (in a thread executor) until the user approves
  3. session is saved to TOKEN_FILE for subsequent starts
"""

import logging
import os

log = logging.getLogger('beo-tidal.auth')

TOKEN_FILE = os.path.join(
    os.getenv('BS5C_CONFIG_DIR', '/etc/beosound5c'),
    'tidal_session.json',
)


class TidalAuth:
    """Wraps a tidalapi.Session with persistence and an async-friendly OAuth flow."""

    def __init__(self):
        import tidalapi  # deferred so import errors surface at runtime, not import-time
        self.session = tidalapi.Session()
        self._device_auth = None   # DeviceAuth object from login_oauth()
        self._login_future = None  # concurrent.futures.Future

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def is_authenticated(self) -> bool:
        try:
            return bool(self.session and self.session.check_login())
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self) -> bool:
        """Load a saved session from TOKEN_FILE.  Returns True if the session is valid."""
        try:
            if os.path.exists(TOKEN_FILE):
                self.session.load_session_from_file(TOKEN_FILE)
                if self.session.check_login():
                    log.info("Restored Tidal session from %s", TOKEN_FILE)
                    return True
                log.warning("Saved Tidal session is no longer valid")
        except Exception as e:
            log.warning("Could not load Tidal session: %s", e)
        return False

    def save(self):
        """Persist the current session to TOKEN_FILE."""
        try:
            os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
            self.session.save_session_to_file(TOKEN_FILE)
            log.info("Saved Tidal session to %s", TOKEN_FILE)
        except Exception as e:
            log.warning("Could not save Tidal session: %s", e)

    def logout(self):
        """Clear in-memory session and delete TOKEN_FILE."""
        import tidalapi
        self._device_auth  = None
        self._login_future = None
        self.session = tidalapi.Session()
        try:
            if os.path.exists(TOKEN_FILE):
                os.unlink(TOKEN_FILE)
                log.info("Deleted Tidal token file")
        except Exception as e:
            log.warning("Could not delete Tidal token file: %s", e)

    # ------------------------------------------------------------------
    # OAuth device-code flow
    # ------------------------------------------------------------------

    def start_oauth_flow(self) -> str:
        """
        Begin the OAuth device-code flow (blocking, run in an executor).
        Returns the URL the user must visit to authorise the device.
        The internal future resolves once the user completes login.
        """
        self._device_auth, self._login_future = self.session.login_oauth()
        url = getattr(
            self._device_auth,
            'verification_uri_complete',
            getattr(self._device_auth, 'verification_uri', ''),
        )
        log.info("Tidal OAuth URL: %s", url)
        return url

    def await_login_blocking(self, timeout: int = 300) -> bool:
        """
        Block until the device-code flow completes or times out.
        Must be called in a thread executor, not directly on the event loop.
        Returns True on success.
        """
        try:
            if self._login_future is None:
                log.error("await_login_blocking called before start_oauth_flow")
                return False
            self._login_future.result(timeout=timeout)
            if self.session.check_login():
                self.save()
                log.info("Tidal OAuth login successful")
                return True
            log.warning("Tidal login future resolved but session is still invalid")
        except Exception as e:
            log.error("Tidal OAuth flow failed: %s", e)
        return False

    @property
    def oauth_url(self) -> str:
        """The verification URL to display while waiting for the user to log in."""
        if self._device_auth is None:
            return ''
        return getattr(
            self._device_auth,
            'verification_uri_complete',
            getattr(self._device_auth, 'verification_uri', ''),
        )
