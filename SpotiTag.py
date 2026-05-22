#!/usr/bin/env python3
# SpotiTag.py
#
# spotitag.py is an interactive command-line Spotify metadata helper for local music files.
# It searches Spotify, previews candidate track metadata, shows album art, writes clean tags
# to supported audio files, can rename files using Spotify metadata, and can also export Spotify
# metadata to a plain .txt file when you search by title and artist.
#
# NOTE: spotitag.py uses the Spotify developer API to fetch metadata, and therefore requires an
# active Premium account. Directions below on setup.
#
# This script does NOT download song files. It is only used for metadata.

import os
import sys
import re
import shutil
import traceback
import tempfile
import subprocess
import platform
from spotipy import Spotify
from spotipy.oauth2 import SpotifyClientCredentials
from mutagen import File as MutagenFile
from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TDRC, TRCK, TPOS, TSRC, TXXX, APIC
from mutagen.flac import Picture
from mutagen.mp4 import MP4, MP4Cover
import requests # used to fetch album art
from io import BytesIO
# Colors: tag names = red, tag fields = white, input prompt = blue
C_TAG = '\033[91m' # red
C_FIELD = '\033[97m' # white
C_INPUT = '\033[94m' # blue
C_PURPLE = '\033[95m' # purple
C_RESET = '\033[0m'
try:
    # Prefer tkinter-based inline popup so we can reliably close it
    import tkinter as tk
    from PIL import Image, ImageTk
    TK_AVAILABLE = True
except Exception:
    TK_AVAILABLE = False
def normalize(s):
    return re.sub(r'\s+', ' ', s.strip())
def sanitize_filename(s):
    s = re.sub(r'[\/:*?"<>|]', '', s)
    return s.strip()
def ms_to_m_ss(ms):
    if ms is None:
        return None
    total_secs = int(round(ms / 1000.0))
    m = total_secs // 60
    s = total_secs % 60
    return f"{m}:{s:02d}"
def search_tracks(sp, title, artist, limit=5):
    """
    Search order optimized for maximum match rate:
    1. Stripped title + artist (primary)
    2. Exact title + artist
    3. Stripped title only
    4. Fuzzy title + artist
    5. Loose fuzzy title only
    Deduplicates results while preserving order.
    """
    queries = []

    # Strip parenthetical content like (Edit), (Remix), etc.
    stripped_title = re.sub(r'\s*\([^)]*\)', '', title).strip()
    stripped_title = re.sub(r'\s{2,}', ' ', stripped_title)

    # 1. Stripped title + artist (preferred)
    if stripped_title:
        if artist:
            queries.append(f'track:"{stripped_title}" artist:"{artist}"')
        queries.append(f'track:"{stripped_title}"')

    # 2. Exact title + artist
    if artist:
        queries.append(f'track:"{title}" artist:"{artist}"')
    queries.append(f'track:"{title}"')

    # 3. Fuzzy search
    if artist:
        queries.append(f'{stripped_title or title} {artist}')

    # 4. Loose fuzzy title search
    if stripped_title:
        queries.append(stripped_title)
    queries.append(title)

    # Remove duplicate queries while preserving order
    deduped_queries = []
    seen_queries = set()
    for q in queries:
        if q and q not in seen_queries:
            seen_queries.add(q)
            deduped_queries.append(q)

    seen_ids = set()
    collected = []

    for q in deduped_queries:
        try:
            results = sp.search(q=q, type='track', limit=limit)
            items = results.get('tracks', {}).get('items', [])

            for item in items:
                tid = item.get('id')
                if tid and tid not in seen_ids:
                    seen_ids.add(tid)
                    collected.append(item)

            if len(collected) >= limit:
                break

        except Exception:
            continue

    return collected[:limit]

def _strip_feat_clause(title):
    """
    Remove only feat/featuring clauses while preserving punctuation,
    remix/demo/edit/version text, and title formatting.
    """
    t = title

    # Remove parenthetical feat only
    t = re.sub(
        r'\s*\((?:[^)]*?)\b(feat|featuring)\b[^)]*\)\s*',
        ' ',
        t,
        flags=re.IGNORECASE
    )

    # Remove trailing feat. Artist
    t = re.sub(
        r'\s+\bfeat\.?\b.*$',
        '',
        t,
        flags=re.IGNORECASE
    )

    return re.sub(r'\s{2,}', ' ', t).strip()

def _normalize_for_match(s):
    # Lowercase, remove punctuation commonly used as separators and extra whitespace
    if not s:
        return ''
    s2 = re.sub(r'[\+\&\,\(\)\.\-_/]', ' ', s, flags=re.UNICODE)
    s2 = re.sub(r'\s+', ' ', s2).strip()
    return s2.lower()
def _title_contains_artist(title, artist_name):
    # Return True if artist_name (or its tokens) appears in title text meaningfully
    if not title or not artist_name:
        return False
    t = _normalize_for_match(title)
    a = _normalize_for_match(artist_name)
    if not a:
        return False
    # Check whole-word presence
    return re.search(r'\b' + re.escape(a) + r'\b', t) is not None
def _remove_artist_fragments_from_string(s, artist_names):
    """
    Remove artist fragments from a title/filename string. Handles forms like:
    - " + artist"
    - " &artist"
    - ", artist"
    - " (with artist)"
    - "(artist)"
    - "artist - ..."
    - "feat artist" inside parentheses or trailing
    Returns cleaned string with extra separators/spaces/punctuation normalized.
    """
    if not s or not artist_names:
        return s
    out = s
    for a in artist_names:
        if not a:
            continue
        # escape
        ae = re.escape(a)
        # patterns to remove (best-effort)
        patterns = [
            # (with artist) or (with Artist Name)
            r'\(\s*with\s+' + ae + r'\s*\)',
            # (feat Artist) or (feat. Artist)
            r'\(\s*feat\.?\s*' + ae + r'\s*\)',
            # feat artist at end
            r'\bfeat\.?\s*' + ae + r'\b',
            # separators like " + Artist", " & Artist", ", Artist", " / Artist"
            r'[\s\+\&\,\/\-]+'+ ae + r'\b',
            # artist in parentheses
            r'\(\s*' + ae + r'\s*\)',
            # artist as standalone token (with punctuation around)
            r'\b' + ae + r'\b',
        ]
        for p in patterns:
            out = re.sub(p, ' ', out, flags=re.IGNORECASE)
    # Remove leftover separators at ends, multiple spaces, stray punctuation
    out = re.sub(r'[\s\-\_\+,\/]+$', '', out).strip()
    out = re.sub(r'^[\s\-\_\+,\/]+', '', out).strip()
    out = re.sub(r'\s{2,}', ' ', out)
    # Remove leftover unmatched parentheses
    out = re.sub(r'\s*\(\s*\)\s*', ' ', out)
    out = out.strip()
    return out
def build_tag_values(track):
    out = {}

    t_name = track.get('name', '') or ''
    artists = [a.get('name', '') for a in track.get('artists', []) if a.get('name')]
    main_artist = artists[0] if artists else ''
    extra_artists = artists[1:] if len(artists) > 1 else []

    protected_terms = [
        'remix', 'mix', 'edit', 'version', 'demo',
        'extended', 'radio edit', 'vip', 'dub',
        'instrumental', 'acoustic', 'live',
        'rework', 'flip', 'bootleg', 'refix'
    ]

    lower_title = t_name.lower()
    preserve_title = any(term in lower_title for term in protected_terms)

    # Keep punctuation/title formatting intact
    base_title = _strip_feat_clause(t_name)

    # Only append artists not already in title
    to_append = []
    for ea in extra_artists:
        if not _title_contains_artist(t_name, ea):
            to_append.append(ea)

    # RULES:
    # - preserve remix/edit/demo/version formatting
    # - preserve punctuation
    # - append extra artists ONLY if not already present
    if to_append:
        feat = ', '.join(to_append)
        title_meta = f"{base_title} (feat. {feat})"
    else:
        title_meta = t_name if preserve_title else base_title

    out['TITLE'] = title_meta

    if main_artist:
        out['ARTIST'] = main_artist

    album = track.get('album', {}).get('name')
    if album:
        out['ALBUM'] = album

    album_artists = [
        a.get('name', '')
        for a in track.get('album', {}).get('artists', [])
        if a.get('name')
    ]

    if album_artists:
        out['ALBUMARTIST'] = album_artists[0]
    elif main_artist:
        out['ALBUMARTIST'] = main_artist

    release_date = track.get('album', {}).get('release_date')
    if release_date:
        out['DATE'] = release_date
        year_match = re.match(r'^(\d{4})', str(release_date))
        if year_match:
            out['YEAR'] = year_match.group(1)

    disc_number = track.get('disc_number')
    if disc_number is not None:
        out['DISCNUMBER'] = str(disc_number)

    track_number = track.get('track_number')
    if track_number is not None:
        out['TRACK'] = str(track_number)

    duration_ms = track.get('duration_ms')
    length_str = ms_to_m_ss(duration_ms)
    if length_str is not None:
        out['LENGTH'] = length_str

    isrc = track.get('external_ids', {}).get('isrc')
    if isrc:
        out['ISRC'] = isrc

    upc = track.get('album', {}).get('external_ids', {}).get('upc')
    if upc:
        out['UPC'] = upc

    if artists:
        out['ALL_ARTISTS'] = ', '.join(artists)

    return out

def read_existing_basic_tags(filepath):
    existing = {}
    ext = os.path.splitext(filepath)[1].lower()
    try:
        if ext == '.mp3':
            try:
                id3 = ID3(filepath)
                tit = id3.getall('TIT2')
                if tit:
                    existing['TITLE'] = tit[0].text[0] if hasattr(tit[0], 'text') else str(tit[0])
                art = id3.getall('TPE1')
                if art:
                    existing['ARTIST'] = art[0].text[0] if hasattr(art[0], 'text') else str(art[0])
                alb = id3.getall('TALB')
                if alb:
                    existing['ALBUM'] = alb[0].text[0] if hasattr(alb[0], 'text') else str(alb[0])
                date = id3.getall('TDRC') or id3.getall('TYER')
                if date:
                    existing['DATE'] = date[0].text[0] if hasattr(date[0], 'text') else str(date[0])
            except Exception:
                try:
                    from mutagen.easyid3 import EasyID3
                    easy = EasyID3(filepath)
                    if easy.get('title'):
                        existing['TITLE'] = easy.get('title')[0]
                    if easy.get('artist'):
                        existing['ARTIST'] = easy.get('artist')[0]
                    if easy.get('album'):
                        existing['ALBUM'] = easy.get('album')[0]
                    if easy.get('date'):
                        existing['DATE'] = easy.get('date')[0]
                except Exception:
                    pass
        else:
            f = MutagenFile(filepath, easy=True)
            if f and getattr(f, 'tags', None):
                if f.tags.get('title'):
                    existing['TITLE'] = f.tags.get('title')[0]
                if f.tags.get('artist'):
                    existing['ARTIST'] = f.tags.get('artist')[0]
                if f.tags.get('album'):
                    existing['ALBUM'] = f.tags.get('album')[0]
                if f.tags.get('date'):
                    existing['DATE'] = f.tags.get('date')[0]
                elif f.tags.get('year'):
                    existing['DATE'] = f.tags.get('year')[0]
    except Exception:
        pass
    return existing
def fetch_album_image(sp, track):
    images = track.get('album', {}).get('images', []) or []
    if not images:
        return None, None
    best = max(images, key=lambda i: (i.get('height') or 0, i.get('width') or 0))
    url = best.get('url')
    if not url:
        return None, None
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        content = resp.content
        mime = resp.headers.get('Content-Type')
        if not mime:
            if url.lower().endswith('.png'):
                mime = 'image/png'
            else:
                mime = 'image/jpeg'
        return content, mime
    except Exception:
        return None, None
# Show image via Tkinter window (so we can close it reliably). Fallback to external opener.
# Keep the popup fixed at a single screen position to avoid drifting.
IMAGE_WINDOW_POSITION = (100, 100)
def _show_image_tempwindow(sp, track):
    image_bytes, mime = fetch_album_image(sp, track)
    if not image_bytes:
        return lambda: None
    # Use Tkinter window that we control and can destroy
    if TK_AVAILABLE:
        try:
            img = Image.open(BytesIO(image_bytes))
            # Create a proper Tk root (instead of Toplevel) to avoid an extra implicit
            # root window showing up (which caused the stray 'tk' / blank tab).
            root = tk.Tk()
            root.title("Album Art (close to continue)")
            # Ensure window appears on top
            try:
                root.attributes('-topmost', True)
            except Exception:
                pass
            # keep window small if image huge:
            w, h = img.size
            max_w, max_h = 800, 800
            if w > max_w or h > max_h:
                ratio = min(max_w / w, max_h / h)
                img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
            tk_img = ImageTk.PhotoImage(img)
            lbl = tk.Label(root, image=tk_img)
            lbl.image = tk_img
            lbl.pack()
            # Fix window position so it doesn't drift each time
            try:
                x, y = IMAGE_WINDOW_POSITION
                root.update_idletasks()
                root.geometry(f"+{x}+{y}")
                root.resizable(False, False)
            except Exception:
                pass
            # Make the window non-blocking by not calling mainloop here.
            # Provide a cleanup function that destroys the root window.
            def cleanup():
                try:
                    root.destroy()
                except Exception:
                    pass
            return cleanup
        except Exception:
            pass
    # Fallback to external opener (best-effort). Try to keep handle for termination.
    suffix = '.png' if (mime and 'png' in (mime or '').lower()) else '.jpg'
    tf = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    path = tf.name
    try:
        tf.write(image_bytes)
    finally:
        tf.close()
    proc = None
    try:
        system = platform.system()
        if system == 'Darwin':
            proc = subprocess.Popen(['open', path])
        elif system == 'Windows':
            # Use os.startfile on Windows (preferred) to avoid launching two viewers.
            try:
                os.startfile(path)
                proc = None
            except Exception:
                # Fallback to start via shell (single attempt) if os.startfile unavailable
                try:
                    proc = subprocess.Popen(['start', path], shell=True)
                except Exception:
                    proc = None
        else:
            proc = subprocess.Popen(['xdg-open', path])
    except Exception:
        proc = None
    def cleanup():
        try:
            if proc:
                if proc.poll() is None:
                    try:
                        proc.terminate()
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
        except Exception:
            pass
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
    return cleanup
def prompt_for_track(filepath, track, index=None, total=None, pre_choice_hook=None):
    """
    Returns (choice, exclude_tags, skip_image) where:
    choice in ('y','n','a','s','q','x')
    exclude_tags: list of tags to exclude (or None)
    skip_image: True/False/None - if user chose not to import image
    """
    tags = build_tag_values(track)
    title = tags.get('TITLE', track.get('name', ''))
    artist_line = ', '.join([a.get('name', '') for a in track.get('artists', [])])
    album = tags.get('ALBUM', track.get('album', {}).get('name', ''))
    date = tags.get('DATE', track.get('album', {}).get('release_date', ''))
    existing = read_existing_basic_tags(filepath)
    print('\n' + '='*60)
    print(f"{C_FIELD}File:{C_RESET} {filepath}")
    if existing:
        print('\nExisting tags:')
        if 'TITLE' in existing:
            print(f' {C_TAG}Existing Title{C_RESET} : {C_FIELD}{existing["TITLE"]}{C_RESET}')
        if 'ARTIST' in existing:
            print(f' {C_TAG}Existing Artist{C_RESET}: {C_FIELD}{existing["ARTIST"]}{C_RESET}')
        if 'ALBUM' in existing:
            print(f' {C_TAG}Existing Album{C_RESET} : {C_FIELD}{existing["ALBUM"]}{C_RESET}')
        if 'DATE' in existing:
            print(f' {C_TAG}Existing Date{C_RESET} : {C_FIELD}{existing["DATE"]}{C_RESET}')
    print('\n' + '-'*60)
    if index is not None and total is not None:
        print(f'\nCandidate {index}/{total}:')
    else:
        print('\nCandidate:')
    print(f' {C_TAG}Title{C_RESET} : {C_FIELD}{title}{C_RESET}')
    print(f' {C_TAG}Artist{C_RESET}: {C_FIELD}{artist_line}{C_RESET}')
    print(f' {C_TAG}Album{C_RESET} : {C_FIELD}{album}{C_RESET}')
    print(f' {C_TAG}Date{C_RESET} : {C_FIELD}{date}{C_RESET}')
    print('\nMetadata to be written (these fields will be overwritten):')
    for k in sorted(tags.keys()):
        print(f' {C_TAG}{k}{C_RESET}: {C_FIELD}{tags[k]}{C_RESET}')

    # Show filename preview
    try:
        t_name = track.get('name', '') or ''
        artists = [a.get('name', '') for a in track.get('artists', []) if a.get('name')]
        main_artist = artists[0] if artists else ''
        fname_title = _strip_feat_clause(t_name)
        artists_to_remove = artists[1:] if len(artists) > 1 else []
        fname_title = _remove_artist_fragments_from_string(fname_title, artists_to_remove)
        filename_base = f"{fname_title} - {main_artist}" if main_artist else fname_title
        filename_base = sanitize_filename(filename_base)
        ext_orig = os.path.splitext(filepath)[1]
        suggested_name = f"{filename_base}{ext_orig}"

        print('\nFilename:')
        print(f' {C_TAG}Existing Filename{C_RESET}: {C_FIELD}{os.path.basename(filepath)}{C_RESET}')
        print(f' {C_TAG}Suggested Filename{C_RESET}: {C_FIELD}{suggested_name}{C_RESET}')
    except Exception:
        pass

    cleanup = None
    if pre_choice_hook:
        try:
            cleanup = pre_choice_hook()
        except Exception:
            cleanup = None
    try:
        print('\nOptions:')
        print(f' {C_INPUT}[y]{C_RESET} accept {C_INPUT}[n]{C_RESET} next {C_INPUT}[x]{C_RESET} skip-file')
        print(f' {C_INPUT}[a]{C_RESET} accept-all {C_INPUT}[s]{C_RESET} skip-all {C_INPUT}[q]{C_RESET} quit')
        while True:
            choice = input(f'\n{C_INPUT}Choice:{C_RESET} ').strip().lower()
            if choice in ('y','n','a','s','q','x'):
                exclude = None
                skip_image = None
                if choice in ('y','a'):
                    try:
                        if cleanup:
                            cleanup()
                            cleanup = None
                    except Exception:
                        pass
                    print(f'\n{C_INPUT}[1]{C_RESET} import all tags & image {C_INPUT}[2]{C_RESET} import tags & choose exclusions')
                    print(f'{C_INPUT}[3]{C_RESET} import tags but skip image {C_INPUT}[4]{C_RESET} go back to search results')
                    while True:
                        sel = input(f'\n{C_INPUT}Choice [1/2/3/4]:{C_RESET} ').strip()
                        if sel in ('1', '2', '3', '4'):
                            break
                        print(f'{C_PURPLE}Enter 1, 2, 3, or 4.{C_RESET}')
                    if sel == '4':
                        return 'n', None, None
                    if sel == '2':
                        ex = input(f'{C_PURPLE}Enter exclusions (comma-separated: title, artist, album, year, filename, image, etc.):{C_RESET} ').strip()
                        if ex:
                            exclude = [t.strip().upper() for t in re.split(r'[,\s]+', ex) if t.strip()]
                        else:
                            exclude = []
                        skip_image = 'IMAGE' in exclude
                    elif sel == '3':
                        skip_image = True
                    else:
                        skip_image = False
                else:
                    try:
                        if cleanup:
                            cleanup()
                            cleanup = None
                    except Exception:
                        pass
                return choice, exclude, skip_image
            print(f'{C_PURPLE}Enter y, n, x, a, s, or q.{C_RESET}')
    finally:
        try:
            if cleanup:
                cleanup()
        except Exception:
            pass
def clear_mp3_frames_for_keys(id3, keys):
    mapping = {
        'TITLE': ['TIT2'],
        'ARTIST': ['TPE1'],
        'ALBUM': ['TALB'],
        'ALBUMARTIST': ['TPE2'],
        'DATE': ['TDRC', 'TYER'],
        'YEAR': ['TDRC', 'TYER'],
        'TRACK': ['TRCK'],
        'DISCNUMBER': ['TPOS'],
        'ISRC': ['TSRC'],
        'UPC': ['TXXX:UPC'],
        'LENGTH': ['TXXX:LENGTH'],
        'APIC': ['APIC'],
    }
    for k in keys:
        frames = mapping.get(k, [])
        for f in frames:
            try:
                id3.delall(f)
            except Exception:
                pass
def has_existing_artist_mp3(filepath):
    try:
        id3 = ID3(filepath)
        if id3.getall('TPE1'):
            return True
    except Exception:
        pass
    try:
        from mutagen.easyid3 import EasyID3
        easy = EasyID3(filepath)
        return bool(easy.get('artist'))
    except Exception:
        return False
def has_existing_artist_vorbis(f):
    if f is None or f.tags is None:
        return False
    for existing in f.tags.keys():
        if existing.lower() == 'artist':
            return True
    return False
def write_tags(filepath, track, sp=None, exclude_tags=None, skip_image=False):
    tags = build_tag_values(track)
    if exclude_tags:
        exclude_set = set([t.upper() for t in exclude_tags])
        for k in list(tags.keys()):
            if k.upper() in exclude_set:
                del tags[k]
    fetch_image = sp is not None and not skip_image
    ext = os.path.splitext(filepath)[1].lower()
    image_bytes = None
    image_mime = None
    if fetch_image:
        image_bytes, image_mime = fetch_album_image(sp, track)
    if ext == '.mp3':
        originally_had_artist = has_existing_artist_mp3(filepath)
        try:
            id3 = ID3(filepath)
        except Exception:
            id3 = ID3()
        keys_to_clear = list(tags.keys())
        if image_bytes:
            keys_to_clear.append('APIC')
        clear_mp3_frames_for_keys(id3, keys_to_clear)
        if 'TITLE' in tags:
            id3.add(TIT2(encoding=3, text=tags['TITLE']))
        if 'ARTIST' in tags:
            id3.add(TPE1(encoding=3, text=tags['ARTIST']))
        if 'ALBUM' in tags:
            id3.add(TALB(encoding=3, text=tags['ALBUM']))
        if 'ALBUMARTIST' in tags:
            id3.add(TPE2(encoding=3, text=tags['ALBUMARTIST']))
        if 'DATE' in tags:
            id3.add(TDRC(encoding=3, text=tags['DATE']))
        elif 'YEAR' in tags:
            id3.add(TDRC(encoding=3, text=tags['YEAR']))
        if 'TRACK' in tags:
            id3.add(TRCK(encoding=3, text=tags['TRACK']))
        if 'DISCNUMBER' in tags:
            id3.add(TPOS(encoding=3, text=tags['DISCNUMBER']))
        if 'ISRC' in tags:
            id3.add(TSRC(encoding=3, text=tags['ISRC']))
        if 'UPC' in tags:
            id3.add(TXXX(encoding=3, desc='UPC', text=tags['UPC']))
        if 'LENGTH' in tags:
            id3.add(TXXX(encoding=3, desc='LENGTH', text=tags['LENGTH']))
        if (not originally_had_artist) and ('ALL_ARTISTS' in tags):
            try:
                id3.add(TXXX(encoding=3, desc='CONTRIBUTORS', text=tags['ALL_ARTISTS']))
            except Exception:
                pass
        if image_bytes:
            try:
                id3.add(APIC(encoding=3, mime=image_mime or 'image/jpeg', type=3, desc='Cover', data=image_bytes))
            except Exception:
                pass
        try:
            id3.save(filepath)
        except Exception:
            pass
        try:
            from mutagen.easyid3 import EasyID3
            try:
                easy = EasyID3(filepath)
            except Exception:
                easy = EasyID3()
            if 'TITLE' in tags:
                easy['title'] = tags['TITLE']
            if 'ARTIST' in tags:
                easy['artist'] = tags['ARTIST']
            if 'ALBUM' in tags:
                easy['album'] = tags['ALBUM']
            if 'TRACK' in tags:
                easy['tracknumber'] = tags['TRACK']
            if 'DISCNUMBER' in tags:
                easy['discnumber'] = tags['DISCNUMBER']
            if 'DATE' in tags:
                easy['date'] = tags['DATE']
            elif 'YEAR' in tags:
                easy['date'] = tags['YEAR']
            easy.save(filepath)
        except Exception:
            pass
    elif ext == '.flac':
        f = MutagenFile(filepath)
        if f is None:
            raise RuntimeError('Unsupported file type')
        originally_had_artist = has_existing_artist_vorbis(f)
        if f.tags is None:
            try:
                f.add_tags()
            except Exception:
                pass
        if getattr(f, 'tags', None) is None:
            f.tags = {}
        def remove_existing_keys_for_overwritten(tagmap, overwrite_keys):
            canon_to_keys = {
                'TITLE': ['title'],
                'ARTIST': ['artist'],
                'ALBUM': ['album'],
                'ALBUMARTIST': ['albumartist'],
                'DATE': ['date', 'year'],
                'YEAR': ['date', 'year'],
                'TRACK': ['tracknumber', 'track'],
                'DISCNUMBER': ['discnumber'],
                'ISRC': ['isrc'],
                'UPC': ['upc'],
                'LENGTH': ['length'],
            }
            to_remove = set()
            for k in overwrite_keys:
                for candidate in canon_to_keys.get(k, []):
                    to_remove.add(candidate)
            for existing in list(tagmap.keys()):
                try:
                    if existing.lower() in to_remove:
                        del tagmap[existing]
                except Exception:
                    pass
        remove_existing_keys_for_overwritten(f.tags, tags.keys())
        def set_vorbis_key(key, value):
            f.tags[key] = [value]
        if 'TITLE' in tags:
            set_vorbis_key('title', tags['TITLE'])
        if 'ARTIST' in tags:
            set_vorbis_key('artist', tags['ARTIST'])
        if 'ALBUM' in tags:
            set_vorbis_key('album', tags['ALBUM'])
        if 'ALBUMARTIST' in tags:
            set_vorbis_key('albumartist', tags['ALBUMARTIST'])
        if 'DATE' in tags:
            set_vorbis_key('date', tags['DATE'])
        elif 'YEAR' in tags:
            set_vorbis_key('date', tags['YEAR'])
        if 'TRACK' in tags:
            set_vorbis_key('tracknumber', tags['TRACK'])
        if 'DISCNUMBER' in tags:
            set_vorbis_key('discnumber', tags['DISCNUMBER'])
        if 'ISRC' in tags:
            set_vorbis_key('isrc', tags['ISRC'])
        if 'UPC' in tags:
            set_vorbis_key('upc', tags['UPC'])
        if 'LENGTH' in tags:
            set_vorbis_key('length', tags['LENGTH'])
        if (not originally_had_artist) and ('ALL_ARTISTS' in tags):
            set_vorbis_key('all_artists', tags['ALL_ARTISTS'])
        if image_bytes:
            try:
                if hasattr(f, 'pictures'):
                    f.clear_pictures()
                pic = Picture()
                pic.data = image_bytes
                pic.type = 3
                pic.mime = image_mime if image_mime else 'image/jpeg'
                f.add_picture(pic)
            except Exception:
                pass
        f.save()
    elif ext in ('.m4a', '.mp4'):
        try:
            mp4 = MP4(filepath)
            if 'covr' in mp4.tags:
                del mp4.tags['covr']
            if image_bytes:
                fmt = MP4Cover.FORMAT_JPEG
                if image_mime and 'png' in image_mime.lower():
                    fmt = MP4Cover.FORMAT_PNG
                mp4.tags['covr'] = [MP4Cover(image_bytes, imageformat=fmt)]
            if 'TITLE' in tags:
                mp4.tags['\xa9nam'] = [tags['TITLE']]
            if 'ARTIST' in tags:
                mp4.tags['\xa9ART'] = [tags['ARTIST']]
            if 'ALBUM' in tags:
                mp4.tags['\xa9alb'] = [tags['ALBUM']]
            if 'DATE' in tags:
                mp4.tags['\xa9day'] = [tags['DATE']]
            elif 'YEAR' in tags:
                mp4.tags['\xa9day'] = [tags['YEAR']]
            if 'TRACK' in tags:
                try:
                    mp4.tags['trkn'] = [(int(tags['TRACK']), 0)]
                except Exception:
                    pass
            mp4.save()
        except Exception:
            try:
                f = MutagenFile(filepath)
                if f and image_bytes:
                    pass
            except Exception:
                pass
    else:
        f = MutagenFile(filepath, easy=False)
        if f is None:
            raise RuntimeError('Unsupported file type')
        originally_had_artist = has_existing_artist_vorbis(f)
        if f.tags is None:
            try:
                f.add_tags()
            except Exception:
                pass
        if getattr(f, 'tags', None) is None:
            f.tags = {}
        def remove_existing_keys_for_overwritten(tagmap, overwrite_keys):
            canon_to_keys = {
                'TITLE': ['title'],
                'ARTIST': ['artist'],
                'ALBUM': ['album'],
                'ALBUMARTIST': ['albumartist'],
                'DATE': ['date', 'year'],
                'YEAR': ['date', 'year'],
                'TRACK': ['tracknumber', 'track'],
                'DISCNUMBER': ['discnumber'],
                'ISRC': ['isrc'],
                'UPC': ['upc'],
                'LENGTH': ['length'],
            }
            to_remove = set()
            for k in overwrite_keys:
                for candidate in canon_to_keys.get(k, []):
                    to_remove.add(candidate)
            for existing in list(tagmap.keys()):
                try:
                    if existing.lower() in to_remove:
                        del tagmap[existing]
                except Exception:
                    pass
        remove_existing_keys_for_overwritten(f.tags, tags.keys())
        def set_vorbis_key(key, value):
            f.tags[key] = [value]
        if 'TITLE' in tags:
            set_vorbis_key('title', tags['TITLE'])
        if 'ARTIST' in tags:
            set_vorbis_key('artist', tags['ARTIST'])
        if 'ALBUM' in tags:
            set_vorbis_key('album', tags['ALBUM'])
        if 'ALBUMARTIST' in tags:
            set_vorbis_key('albumartist', tags['ALBUMARTIST'])
        if 'DATE' in tags:
            set_vorbis_key('date', tags['DATE'])
        elif 'YEAR' in tags:
            set_vorbis_key('date', tags['YEAR'])
        if 'TRACK' in tags:
            set_vorbis_key('tracknumber', tags['TRACK'])
        if 'DISCNUMBER' in tags:
            set_vorbis_key('discnumber', tags['DISCNUMBER'])
        if 'ISRC' in tags:
            set_vorbis_key('isrc', tags['ISRC'])
        if 'UPC' in tags:
            set_vorbis_key('upc', tags['UPC'])
        if 'LENGTH' in tags:
            set_vorbis_key('length', tags['LENGTH'])
        if (not originally_had_artist) and ('ALL_ARTISTS' in tags):
            set_vorbis_key('all_artists', tags['ALL_ARTISTS'])
        f.save()
    # Rename: exclude feat from filename (use stripped base title) and remove featured fragments present in original title
    if exclude_tags and 'FILENAME' in {t.upper() for t in exclude_tags}:
        return filepath
    try:
        t_name = track.get('name', '') or ''
        artists = [a.get('name', '') for a in track.get('artists', []) if a.get('name')]
        main_artist = artists[0] if artists else ''
        protected_terms = [
            'remix', 'mix', 'edit', 'version', 'demo',
            'extended', 'radio edit', 'vip', 'dub',
            'instrumental', 'acoustic', 'live', 'rework',
            'flip', 'bootleg', 'refix'
        ]

        lower_title = t_name.lower()
        preserve_title = any(term in lower_title for term in protected_terms)

        # preserve original title for mixes/remixes/edits/etc.
        if preserve_title:
            fname_title = t_name
        else:
            fname_title = _strip_feat_clause(t_name)
        # Remove extra-artist fragments from filename if they appear in the title (handles "+", "&", "(with...)", etc.)
        artists_to_remove = artists[1:] if len(artists) > 1 else []
        fname_title = _remove_artist_fragments_from_string(fname_title, artists_to_remove)
        filename_base = f"{fname_title} - {main_artist}" if main_artist else fname_title
        filename_base = sanitize_filename(filename_base)
        ext_orig = os.path.splitext(filepath)[1]
        dirpath = os.path.dirname(filepath) or '.'
        new_name = f"{filename_base}{ext_orig}"
        new_path = os.path.join(dirpath, new_name)
        if os.path.abspath(new_path) != os.path.abspath(filepath):
            candidate = new_path
            i = 1
            while os.path.exists(candidate):
                try:
                    if os.path.samefile(candidate, filepath):
                        break
                except Exception:
                    pass
                candidate = os.path.join(dirpath, f"{filename_base} ({i}){ext_orig}")
                i += 1
            shutil.move(filepath, candidate)
            print(f"Renamed: {os.path.basename(filepath)} -> {os.path.basename(candidate)}")
            return candidate
    except Exception:
        print("Rename failed:", traceback.format_exc())
    return filepath
def parse_filename_guess(fname):
    base = os.path.splitext(os.path.basename(fname))[0]
    if ' - ' in base:
        left, right = [normalize(x) for x in base.split(' - ', 1)]
        return [(left, right), (right, left)]
    else:
        return [(base, '')]

def parse_search_input(user_input):
    """
    Parse manual search input.

    Supported forms:
    - title, artist
    - "title" "artist"
    - 'title' 'artist'

    Returns (title, artist) or None.
    """
    if not user_input:
        return None

    user_input = user_input.strip()

    if ',' in user_input:
        parts = [p.strip().strip('"\'') for p in user_input.split(',', 1)]
        if parts[0]:
            return (parts[0], parts[1] if len(parts) > 1 else '')

    try:
        import shlex
        parts = shlex.split(user_input)
    except Exception:
        parts = []

    if len(parts) >= 2:
        return (parts[0].strip(), ' '.join(parts[1:]).strip())

    return None


def parse_cli_args(argv):
    """
    Decide whether CLI args are a path search or title/artist metadata search.

    Supported command forms:
    - python3 st.py /path/to/file-or-folder
    - python3 st.py "Song Title" "Artist Name"
    """
    if not argv:
        return None, None

    if len(argv) == 1:
        single = argv[0].strip()
        if os.path.exists(single):
            return 'path', single
        parsed = parse_search_input(single)
        if parsed:
            return 'metadata', parsed
        return 'path', single

    # Two or more arguments are treated as title + artist, as requested.
    title = argv[0].strip()
    artist = ' '.join(argv[1:]).strip()
    return 'metadata', (title, artist)


def format_metadata_text(track):
    tags = build_tag_values(track)
    lines = []

    for key in sorted(tags.keys()):
        lines.append(f'{key}: {tags[key]}')

    spotify_url = track.get('external_urls', {}).get('spotify')
    if spotify_url:
        lines.append(f'SPOTIFY_URL: {spotify_url}')

    track_id = track.get('id')
    if track_id:
        lines.append(f'SPOTIFY_ID: {track_id}')

    return '\n'.join(lines) + '\n'


def save_metadata_text(track, output_dir=None):
    tags = build_tag_values(track)
    title = tags.get('TITLE') or track.get('name') or 'spotify_metadata'
    artist = tags.get('ARTIST') or 'unknown_artist'
    base = sanitize_filename(f'{title} - {artist}') or 'spotify_metadata'

    if output_dir is None:
        output_dir = os.getcwd()

    path = os.path.join(output_dir, f'{base}.txt')
    candidate = path
    i = 1
    while os.path.exists(candidate):
        candidate = os.path.join(output_dir, f'{base} ({i}).txt')
        i += 1

    with open(candidate, 'w', encoding='utf-8') as f:
        f.write(format_metadata_text(track))

    return candidate


def prompt_for_metadata_track(title, artist, track, index=None, total=None, pre_choice_hook=None):
    """
    Metadata-only prompt. Uses the same color scheme as the tag-writing prompt,
    but does not imply any audio file will be modified.
    """
    tags = build_tag_values(track)
    title_line = tags.get('TITLE', track.get('name', ''))
    artist_line = ', '.join([a.get('name', '') for a in track.get('artists', [])])
    album = tags.get('ALBUM', track.get('album', {}).get('name', ''))
    date = tags.get('DATE', track.get('album', {}).get('release_date', ''))

    print('\n' + '='*60)
    print(f"{C_FIELD}Search:{C_RESET} {title} - {artist}")
    print('\n' + '-'*60)
    if index is not None and total is not None:
        print(f'\nCandidate {index}/{total}:')
    else:
        print('\nCandidate:')
    print(f' {C_TAG}Title{C_RESET} : {C_FIELD}{title_line}{C_RESET}')
    print(f' {C_TAG}Artist{C_RESET}: {C_FIELD}{artist_line}{C_RESET}')
    print(f' {C_TAG}Album{C_RESET} : {C_FIELD}{album}{C_RESET}')
    print(f' {C_TAG}Date{C_RESET} : {C_FIELD}{date}{C_RESET}')
    print('\nMetadata to be saved as a text file:')
    for k in sorted(tags.keys()):
        print(f' {C_TAG}{k}{C_RESET}: {C_FIELD}{tags[k]}{C_RESET}')

    cleanup = None
    if pre_choice_hook:
        try:
            cleanup = pre_choice_hook()
        except Exception:
            cleanup = None

    try:
        print('\nOptions:')
        print(f' {C_INPUT}[y]{C_RESET} save metadata text file {C_INPUT}[n]{C_RESET} next {C_INPUT}[q]{C_RESET} quit')
        while True:
            choice = input(f'\n{C_INPUT}Choice:{C_RESET} ').strip().lower()
            if choice in ('y', 'n', 'q'):
                return choice
            print(f'{C_PURPLE}Enter y, n, or q.{C_RESET}')
    finally:
        try:
            if cleanup:
                cleanup()
        except Exception:
            pass


def metadata_search_mode(sp, title, artist, output_dir=None):
    """
    Search by title and artist, let the user pick a Spotify result, then save
    the selected metadata as a .txt file.
    """
    candidates = search_tracks(sp, title, artist, limit=10)

    if not candidates:
        print(f'No results found for: {title}, {artist}')
        return 1

    for idx, candidate in enumerate(candidates, start=1):
        def pre_hook():
            return _show_image_tempwindow(sp, candidate)

        choice = prompt_for_metadata_track(
            title,
            artist,
            candidate,
            idx,
            len(candidates),
            pre_choice_hook=pre_hook
        )

        if choice == 'y':
            txt_path = save_metadata_text(candidate, output_dir=output_dir)
            print(f'\n{C_TAG}Saved metadata text file{C_RESET}: {C_FIELD}{txt_path}{C_RESET}')
            return 0
        if choice == 'n':
            continue
        if choice == 'q':
            print('Quitting.')
            return 0

    print(f'No candidate accepted for: {title}, {artist}')
    return 0


def process_path_mode(sp, path):
    """
    Existing file/folder tagging flow. Kept separate so CLI and prompt modes can
    choose between path tagging and title/artist metadata export cleanly.
    """
    files = []
    if os.path.isdir(path):
        for f in sorted(os.listdir(path)):
            if f.lower().endswith(('.mp3', '.flac', '.ogg', '.m4a')):
                files.append(os.path.join(path, f))
    else:
        files = [path]
    apply_all = False
    skip_all = False
    for fp in files:
        if skip_all:
            print('Skipping (skip all):', fp)
            continue
        if not os.path.exists(fp):
            print('File does not exist:', fp)
            continue
        try:
            guesses = parse_filename_guess(fp)
            found_any = False
            selected_track = None
            selected_exclude = None
            selected_skip_image = False
            # candidate loop with support to repeat results if user skips all with 'n'
            repeat_search = True
            # We'll iterate guesses; for each guess produce candidates and handle user choices.
            # The outer loop allows repeating the presented candidates for that file if user chooses to.
            while repeat_search:
                repeat_search = False
                skip_file_by_x = False
                any_candidates_total = 0
                n_choice_count = 0
                # iterate guesses; stop early if selected/skip_all/skip_file_by_x
                for title_guess, artist_guess in guesses:
                    if selected_track or skip_all or skip_file_by_x:
                        break
                    candidates = search_tracks(sp, title_guess, artist_guess, limit=5)
                    if not candidates:
                        continue
                    found_any = True
                    any_candidates_total += len(candidates)
                    for idx, candidate in enumerate(candidates, start=1):
                        if apply_all:
                            def pre_hook():
                                return _show_image_tempwindow(sp, candidate)
                            choice, exclude, skip_image = prompt_for_track(fp, candidate, idx, len(candidates), pre_choice_hook=pre_hook)
                            if choice in ('y', 'a'):
                                selected_track = candidate
                                selected_exclude = exclude or []
                                selected_skip_image = bool(skip_image)
                            elif choice == 'n':
                                n_choice_count += 1
                                continue
                            elif choice == 'x':
                                selected_track = None
                                skip_file_by_x = True
                                break
                            elif choice == 's':
                                skip_all = True
                                break
                            elif choice == 'q':
                                print('Quitting.')
                                return 0
                            # apply_all handled by 'a' above; no need for else branch here
                        else:
                            def pre_hook():
                                return _show_image_tempwindow(sp, candidate)
                            choice, exclude, skip_image = prompt_for_track(fp, candidate, idx, len(candidates), pre_choice_hook=pre_hook)
                            if choice == 'y':
                                selected_track = candidate
                                selected_exclude = exclude or []
                                selected_skip_image = bool(skip_image)
                                break
                            elif choice == 'n':
                                n_choice_count += 1
                                continue
                            elif choice == 'a':
                                apply_all = True
                                selected_track = candidate
                                selected_exclude = exclude or []
                                selected_skip_image = bool(skip_image)
                                break
                            elif choice == 'x':
                                selected_track = None
                                skip_file_by_x = True
                                break
                            elif choice == 's':
                                skip_all = True
                                break
                            elif choice == 'q':
                                print('Quitting.')
                                return 0
                    if selected_track or skip_all or skip_file_by_x:
                        break
                # After iterating all guesses' candidates:
                # If found candidates (found_any) and user pressed 'n' for each candidate (n_choice_count == any_candidates_total)
                # and they did not choose 'x' to skip file, prompt to repeat results or move on.
                if found_any and not selected_track and not skip_all and not skip_file_by_x and any_candidates_total > 0 and n_choice_count >= any_candidates_total:
                    # Ask whether to repeat the results for this file
                    while True:
                        resp = input(f"\n{C_INPUT}All results skipped. Repeat these results? [y/N]:{C_RESET} ").strip().lower()
                        if resp in ('', 'y', 'n'):
                            break
                        print(f'{C_PURPLE}Enter y or n.{C_RESET}')
                    if resp == 'y':
                        # Reset counters and repeat the same guesses (will show images again)
                        repeat_search = True
                        # keep selected_track None, skip_all False, etc.
                    else:
                        # Move on to next file
                        pass
            if not found_any:
                print(f'No match for guesses: {guesses} — file: {fp}')
                continue
            if skip_all:
                print('Skipping (skip all):', fp)
                continue
            if selected_track is None:
                print('No candidate accepted for:', fp)
                continue
            new_fp = write_tags(fp, selected_track, sp=sp, exclude_tags=(selected_exclude or []), skip_image=selected_skip_image)
            print('Tagged:', new_fp)
        except Exception as e:
            print(f'Error {fp}: {e}')
            print(traceback.format_exc())
    return 0


def main(argv=None):
    os.environ.setdefault('SPOTIPY_CACHE_PATH', os.path.expanduser('~/.cache/spotify'))
    if not os.getenv('SPOTIPY_CLIENT_ID') or not os.getenv('SPOTIPY_CLIENT_SECRET'):
        print(f'{C_PURPLE}Spotify credentials are not set. Add your Spotify Client ID and Client Secret, then run this script again.{C_RESET}')
        print(f'{C_PURPLE}Create/get credentials at https://developer.spotify.com/dashboard, then export them in your terminal:{C_RESET}')
        print(f'{C_PURPLE}export SPOTIPY_CLIENT_ID="your_client_id"{C_RESET}')
        print(f'{C_PURPLE}export SPOTIPY_CLIENT_SECRET="your_client_secret"{C_RESET}')
        return 1

    if argv is None:
        argv = sys.argv[1:]

    sp = Spotify(auth_manager=SpotifyClientCredentials())
    mode, value = parse_cli_args(argv)

    if mode is None:
        print(f"{C_PURPLE}Enter a file/folder path to tag existing audio, or enter quoted search terms like 'title' 'artist' to save Spotify metadata as a text file.{C_RESET}")
        user_input = input(
            f"{C_INPUT}Path or 'title' 'artist':{C_RESET} "
        ).strip()

        if os.path.exists(user_input):
            mode, value = 'path', user_input
        else:
            parsed = parse_search_input(user_input)
            if parsed:
                mode, value = 'metadata', parsed
            else:
                mode, value = 'path', user_input

    if mode == 'metadata':
        title, artist = value
        return metadata_search_mode(sp, title, artist)

    return process_path_mode(sp, value)


if __name__ == '__main__':
    sys.exit(main())
