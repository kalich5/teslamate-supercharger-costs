"""
Translations, following TeslaMate's web app.

The language is chosen like in TeslaMate: ``?locale=`` (the switcher in
Settings), then the choice stored in the session, then the browser's
Accept-Language header (strict matching, e.g. ``nn`` → ``nb``, ``zh-TW`` →
``zh_Hant``), and English otherwise.

Outside a web request (CLI, scheduler) messages stay in English.
"""

try:
    from flask_babel import gettext, lazy_gettext
except ImportError:  # CLI without the web dependencies
    def gettext(string, **variables):
        return string % variables if variables else string

    lazy_gettext = gettext

_ = gettext

DEFAULT_LOCALE = "en"

# Languages with a translation in translations/ (TeslaMate's English labels).
# To add one, see "Translations" in the README.
LANGUAGES = {
    "cs": "Czech",
    "en": "English",
}

# Close relatives that should map onto a supported locale.
_ALIASES = {
    "no": "nb", "nn": "nb",
    "zh": "zh_Hans",
    "zh_cn": "zh_Hans", "zh_sg": "zh_Hans", "zh_hans": "zh_Hans",
    "zh_tw": "zh_Hant", "zh_hk": "zh_Hant", "zh_mo": "zh_Hant", "zh_hant": "zh_Hant",
}
_LOWER = {code.lower(): code for code in LANGUAGES}


def match_locale(tag):
    """Return the supported locale for a language tag, or None (strict)."""
    code = _match(tag)
    return code if code in LANGUAGES else None


def _match(tag):
    if not tag or not isinstance(tag, str):
        return None
    tag = tag.strip().replace("-", "_").lower()
    if tag in _LOWER:
        return _LOWER[tag]
    parts = tag.split("_")
    # zh-Hant-TW, zh-Hans-CN, zh-TW, …
    if parts[0] == "zh":
        for part in parts[1:]:
            if f"zh_{part}" in _ALIASES:
                return _ALIASES[f"zh_{part}"]
        return _ALIASES["zh"]
    if tag in _ALIASES:
        return _ALIASES[tag]
    primary = parts[0]
    return _LOWER.get(primary) or _ALIASES.get(primary)


def select_locale(query_locale, session, accept_languages):
    """
    Pick the UI locale.  *session* is a dict-like store (Flask session),
    *accept_languages* an iterable of tags in preference order.
    """
    chosen = match_locale(query_locale)
    if chosen:
        session["locale"] = chosen
        return chosen
    stored = match_locale(session.get("locale"))
    if stored:
        return stored
    for tag in accept_languages:
        chosen = match_locale(tag)
        if chosen:
            return chosen
    return DEFAULT_LOCALE


def language_options():
    """[(code, label)] sorted by label, as in TeslaMate's switcher."""
    return sorted(LANGUAGES.items(), key=lambda item: item[1])
