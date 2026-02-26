/**
 * Auto-source probe for standalone / dev mode
 *
 * On a deployed device the beo-router service calls /router/menu → the UI
 * fetches it in _fetchMenu() → source scripts are loaded dynamically.
 *
 * When the router is NOT running (Windows dev, laptop testing) this file
 * replicates that bootstrapping by directly probing each known source service.
 * If a service responds it loads its view script and adds it to the menu,
 * exactly as _fetchMenu() would have done.
 *
 * Safe to load on a real device: it skips any source that is already in the
 * menu (router got there first) and fails silently if services are down.
 */

const AUTO_SOURCES = [
    {
        preset: 'tidal',
        statusUrl: () => (window.AppConfig?.tidalServiceUrl   || 'http://localhost:8772') + '/status'
    },
    {
        preset: 'spotify',
        statusUrl: () => (window.AppConfig?.spotifyServiceUrl || 'http://localhost:8771') + '/status'
    },
];

async function probeAndAddSource({ preset, statusUrl }) {
    // Probe the service — 1.5 s timeout so the page isn't held up
    try {
        const resp = await fetch(statusUrl(), {
            signal: AbortSignal.timeout(1500)
        });
        if (!resp.ok) return;
    } catch (e) {
        return; // service not running or unreachable
    }

    const uiStore = window.uiStore;
    if (!uiStore) return;

    // Router may have already added it via _fetchMenu() — don't duplicate
    if (uiStore.menuItems.some(m => m.path === `menu/${preset}`)) {
        console.log(`[AUTO-SOURCE] ${preset} already in menu (router added it)`);
        return;
    }

    // Load the view script if not yet present
    if (!window.SourcePresets?.[preset]) {
        await uiStore._loadSourceScript(preset);
    }

    const sourcePreset = window.SourcePresets?.[preset];
    if (!sourcePreset) {
        console.warn(`[AUTO-SOURCE] No SourcePresets.${preset} after script load`);
        return;
    }

    // Add to menu and fire lifecycle hooks
    uiStore.addMenuItem(sourcePreset.item, sourcePreset.after, sourcePreset.view);
    if (sourcePreset.onAdd) sourcePreset.onAdd();

    console.log(`[AUTO-SOURCE] Added ${preset} to menu (service is running)`);
}

document.addEventListener('DOMContentLoaded', () => {
    // Delay slightly so UIStore, ws-dispatcher, and _fetchMenu() all finish first
    // (_fetchMenu uses a 100 ms delay; router response takes ~200 ms on LAN)
    setTimeout(async () => {
        for (const src of AUTO_SOURCES) {
            await probeAndAddSource(src);
        }
    }, 1000);
});
