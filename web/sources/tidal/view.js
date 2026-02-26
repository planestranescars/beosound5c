/**
 * Tidal Source Preset for BeoSound 5c
 *
 * Registers window.SourcePresets.tidal so the main UI treats Tidal as a
 * first-class source.  All hardware events (laser, volume arc, nav wheel,
 * left/right/go buttons, haptic clicks) are handled by the parent's
 * hardware-input.js / ws-dispatcher.js pipeline — no duplication needed.
 *
 * Loaded dynamically by UIStore._loadSourceScript('tidal') when the router
 * broadcasts:  {type:'menu_item', data:{action:'add', preset:'tidal'}}
 *
 * Nav / button events flow:
 *   input.py WS → ws-dispatcher → hardware-input → IframeMessenger.sendNavEvent
 *     → postMessage → tidal.html (ArcList.handleNavFromParent / handleButtonFromParent)
 *
 * Haptic clicks:
 *   tidal.html ArcList.sendClickCommand → postMessage({ type:'click' }) → parent
 *     → uiStore.sendClickCommand → WS → input.py do_click()
 *
 * Playing view:
 *   Tidal service broadcasts media_update (title/artist/artwork/state) —
 *   the standard format shared by Spotify.  The existing now-playing template
 *   in the main UI picks it up automatically.
 */

window.SourcePresets = window.SourcePresets || {};

window.SourcePresets.tidal = {

    // ── Menu item ─────────────────────────────────────────────────────────────
    item:  { title: 'TIDAL', path: 'menu/tidal' },
    after: 'menu/playing',

    // ── Browse view (menu/tidal) ───────────────────────────────────────────────
    view: {
        title: 'TIDAL',
        content: `
            <div id="tidal-container" style="
                position: absolute; top: 0; left: 0;
                width: 100%; height: 100%;
            "></div>`,

        // _webpage tells updateView() to create (or reuse) an iframe.
        // The id starts with "preload-" so the rescue logic in updateView()
        // moves it to the preload container instead of destroying it when
        // navigating away — state (scroll position, view mode) is preserved.
        _webpage: {
            iframeId:    'preload-tidal',
            containerId: 'tidal-container',
            url:         'softarc/tidal.html'
        }
    },

    // ── Lifecycle hooks ───────────────────────────────────────────────────────

    onAdd() {
        // Register with IframeMessenger so routeNavToView / routeButtonToView
        // forward wheel and button events to the iframe via postMessage instead
        // of trying to scrollBy() the page.
        if (window.IframeMessenger) {
            window.IframeMessenger.registerIframe('menu/tidal', 'preload-tidal');
            console.log('[TIDAL] IframeMessenger registered for menu/tidal');
        }
    },

    onMount() {
        // The softarc iframe initialises itself via its own DOMContentLoaded.
    },

    onRemove() {
        if (window.IframeMessenger) {
            window.IframeMessenger.unregisterIframe('menu/tidal');
            console.log('[TIDAL] IframeMessenger unregistered for menu/tidal');
        }
    },

    // ── Playing sub-preset (menu/playing while Tidal is active) ───────────────
    //
    // Tidal service broadcasts media_update events in the same format as the
    // Spotify service:  { title, artist, artwork, state }
    // The main UI's ArtworkManager / now-playing template handles these
    // automatically — no custom onMount / onUpdate needed here.
    playing: {
        eventType: 'media_update'
    }
};
