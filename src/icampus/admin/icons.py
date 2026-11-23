"""The admin UI's icons: 24-unit line drawings, inlined as SVG so no page asks anyone for an image.

Stroke 1.6, round caps and joins, drawn in the current text colour. Templates call icon("name") (a Jinja global);
a name that isn't here is a mistake in a template and fails loudly rather than leaving a hole in the page.
"""

from markupsafe import Markup

PATHS = {
    # navigation
    "home": '<path d="M3 10.2 12 3l9 7.2"/><path d="M5.5 8.6V21h13V8.6"/><path d="M10 21v-6h4v6"/>',
    "jobs": '<path d="M9 6h11M9 12h11M9 18h11"/><path d="m3.5 6 1 1 2-2"/><path d="m3.5 12 1 1 2-2"/><rect x="3.5" y="16.5" width="3" height="3" rx=".5"/>',
    "cache": '<ellipse cx="12" cy="5.5" rx="8" ry="3"/><path d="M4 5.5v13c0 1.7 3.6 3 8 3s8-1.3 8-3v-13"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
    "wave": '<path d="M4 10v4M8 6v12M12 3v18M16 8v8M20 11v2"/>',
    "settings": '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
    "signout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>',
    # theme
    "monitor": '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    "moon": '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
    # what is kept on disk
    "video": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m10 9 5 3-5 3z"/>',
    "audio": '<path d="M4 10v4M8 6v12M12 3v18M16 8v8M20 11v2"/>',
    "attachment": '<path d="m20.5 11.5-8.6 8.6a5 5 0 0 1-7-7l8.6-8.6a3.4 3.4 0 0 1 4.8 4.8l-8.5 8.5a1.8 1.8 0 0 1-2.6-2.6l7.9-7.8"/>',
    "transcript": '<path d="M6 3h9l4 4v14H6z"/><path d="M14.5 3v4.5H19"/><path d="M9 12.5h7M9 16.5h5"/>',
    "frames": '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8.5" cy="9.5" r="1.5"/><path d="m3.5 17 5-5 4 4 3-3 5 5"/>',
    # actions
    "arrow-right": '<path d="M5 12h14"/><path d="m13 6 6 6-6 6"/>',
    "arrow-up-right": '<path d="M7 17 17 7"/><path d="M8 7h9v9"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "check": '<path d="m4.5 12.5 4.5 4.5L19.5 6.5"/>',
    "chevron": '<path d="m6 9 6 6 6-6"/>',
    "more": '<circle cx="5" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="19" cy="12" r="1.2"/>',
    "pin": '<path d="M9 4h6l-1 6 3 3H7l3-3z"/><path d="M12 13v7"/>',
    "download": '<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M4 21h16"/>',
    "refresh": '<path d="M20 11a8 8 0 0 0-14.3-4.3L3 9.5"/><path d="M3 4.5v5h5"/><path d="M4 13a8 8 0 0 0 14.3 4.3l2.7-2.8"/><path d="M21 19.5v-5h-5"/>',
    "trash": '<path d="M4 7h16"/><path d="M9 7V4h6v3"/><path d="m6 7 1 13h10l1-13"/><path d="M10 11v6M14 11v6"/>',
    "play": '<path d="m8 5 11 7-11 7z"/>',
    "power": '<path d="M12 3v9"/><path d="M6.3 7.3a8 8 0 1 0 11.4 0"/>',
    "edit": '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="m13.5 6.5 4 4"/>',
    "eye": '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    "eye-off": '<path d="M3 3l18 18"/><path d="M10.6 5.1A10.7 10.7 0 0 1 12 5c6.5 0 10 7 10 7a17.6 17.6 0 0 1-3.2 4.2"/><path d="M6.6 6.6A17.4 17.4 0 0 0 2 12s3.5 7 10 7a9.7 9.7 0 0 0 5.4-1.6"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
    # messages
    "alert": '<path d="M12 3 2 20h20z"/><path d="M12 10v4.5M12 17.5h.01"/>',
    "alert-square": '<rect x="3" y="3" width="18" height="18" rx="3"/><path d="M12 7.5v5.5M12 16.5h.01"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
}
NOTICE = {"success": "check", "danger": "alert-square", "warning": "alert"}


def icon(name: str, size: int = 20, stroke: float = 1.6) -> Markup:
    return Markup(f'<svg width="{int(size)}" height="{int(size)}" viewBox="0 0 24 24" fill="none" '
                  f'stroke="currentColor" stroke-width="{float(stroke):g}" stroke-linecap="round" '
                  f'stroke-linejoin="round" aria-hidden="true">{PATHS[name]}</svg>')
