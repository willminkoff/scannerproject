/*
 * sb5 theme bridge for OpenWebRX+ — makes the Live IQ iframe feel native to /sb5.
 *
 * OWRX+ auto-loads static/plugins/receiver/init.js on the receiver page (see the
 * stock plugins.js loader).  The plugins dir is bind-mounted from the host
 * (/opt/owrx-docker/plugins), so dropping files here needs NO container rebuild —
 * unlike css/custom.css, which lives inside the image.  A browser reload picks it
 * up; we `docker restart owrxp` on deploy only to guarantee a clean pickup.
 *
 * Two stylesheets are injected, in cascade order:
 *   1. the /sb5 shared type scale from airband-ui.  CSS custom properties do NOT
 *      cross the iframe document boundary, so the only way this OWRX document can
 *      read var(--type-*) is to link the same file itself.  The host is taken from
 *      location.* so it tracks LAN IP / micro.local / Tailscale without hardcoding
 *      (see docs/openwebrx-pilot.md).
 *   2. sb5_override.css, served from this plugin dir — the warm-dark navy/cyan
 *      palette + the var(--type-*) applications.  Loaded last so it wins.
 *
 * We also tag <body> with OWRX's own theme hooks (has-theme + theme-sb5) so the
 * stock themes.css rules repaint panels / buttons / inputs from our --theme-color*
 * values, reusing OWRX's theming engine instead of fighting its selectors.
 */
(function () {
    // airband-ui serves /static/sb5_theme.css (op25 is :8080; icecast :8000).
    var AIRBAND_UI_PORT = 5050;

    function linkCss(href) {
        var l = document.createElement('link');
        l.rel = 'stylesheet';
        l.type = 'text/css';
        l.href = href;
        document.head.appendChild(l);
    }

    // 1) shared /sb5 type scale (cross-origin <link> applies without CORS headers)
    linkCss(location.protocol + '//' + location.hostname + ':' + AIRBAND_UI_PORT
            + '/static/sb5_theme.css');

    // 2) warm-dark palette + typography, served alongside this plugin
    linkCss('static/plugins/receiver/sb5_override.css');

    // 3) opt this document into OWRX's theme engine with the sb5 navy palette
    document.body.classList.add('has-theme', 'theme-sb5');
})();
