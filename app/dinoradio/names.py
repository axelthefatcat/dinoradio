"""Simplifying artist and song names so different spellings of the same song match."""
import re
import unicodedata


def normal(text: str) -> str:
    """Lower case, no accents, '&' as 'and', no leading 'the', letters and digits only."""
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("&", " and ")
    s = s.strip()
    if s.startswith("the "):
        s = s[4:]
    return "".join(c for c in s if c.isalnum())


def base_title(title: str) -> str:
    """'Song (Remastered 2011)' / 'Song - Live' / 'Song [Single Version]' -> 'Song'"""
    t = title or ""
    for mark in (" (", " [", " - "):
        i = t.find(mark)
        if i > 0:
            t = t[:i]
    return t.strip()


_ARTIST_SPLIT = [" feat. ", " feat ", " ft. ", " featuring ", " with ", " x ", ", ", " & ", " and ", " / ", "; "]


def primary_artist(artist: str) -> str:
    """'Artist feat. Someone' / 'Artist & Someone' -> 'Artist'"""
    a = artist or ""
    for mark in _ARTIST_SPLIT:
        i = a.lower().find(mark)
        if i > 0:
            a = a[:i]
    return a.strip()


def chart_key(artist: str, title: str) -> str:
    return normal(primary_artist(artist)) + "|" + normal(base_title(title))


def artist_key(artist: str) -> str:
    return normal(primary_artist(artist))


_TRACKNO = re.compile(r"^\s*\d{1,3}([.\-]\d{1,3})?\s*[-._ ]\s*")


def from_filename(path: str):
    """Best guess at (artist, title) from '01.05 - Carpenters - Goodbye To Love.flac'."""
    import os
    name = os.path.splitext(os.path.basename(path))[0]
    name = _TRACKNO.sub("", name)
    parts = [p.strip() for p in name.split(" - ") if p.strip()]
    if len(parts) >= 2:
        return parts[0], " - ".join(parts[1:])
    return "", name.strip()
