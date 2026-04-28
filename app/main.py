# ===================================================
# LJUDBUSTER - Redaktionsverktyg
# Version: 1.8.9-dev av Johan Hörnqvist
# Uppdaterad: 2026-04-28
#
# Changelog:
# - SVT DOM-first Resolver (1.8.9-dev): Scope:ar SVT livepost mot exakt inläggs-id, läser video[src]/manifestUrl ur DOM och blockerar global fallback för att undvika fel video.
# - SVT Scoped Livepost Fix (1.8.8-dev): Tog bort lokal urlparse/parse_qs-import som skuggade global import och kraschade scoped resolver.
# - SVT Scoped-Only Livepost Resolver (1.8.7-dev): SVT ?inlagg= får bara hämta ström från exakt rätt renderad inläggscontainer.
# - SVT Rendered Livepost Manifest (1.8.6-dev): Läser manifest direkt ur renderad rätt SVT livepost-div innan generell kandidatinsamling.
# - SVT Livepost Direct Manifest (1.8.5-dev): Plockar manifestUrl direkt ur rätt livepost-div innan Playwright-klick.
# - Clean Slate (1.8.4): Städat versionsheadern så APP_VERSION och UI-version visar samma version.
# - Robust historik (1.8.4): history.json skrivs atomiskt med thread-lock och job_id-baserade statusuppdateringar.
# - Jobbkö (1.8.4): Begränsar tunga hämtningar via LJUDBUSTER_MAX_CONCURRENT_JOBS (default 1) för färre Playwright/yt-dlp-krockar.
# - Snygga outputnamn (1.8.4): Publicerade filer får lowercase kebab-case, ASCII-normalisering och säkra tecken.
# - Säkrare filhantering (1.8.4): Manifest/textrester räknas inte som färdiga mediafiler; final move sker med unik final path.
# - Smart Detector Timeout (1.8.4): ffmpeg-konvertering har timeout och tydligare felhantering.
# - SVT Switcher Hint (1.8.4): När Fiskenätet ser en SVT DASH-switcher försöker den även lägga till HLS/AVC-syskonet.
# ===================================================

from contextlib import asynccontextmanager
from fastapi import FastAPI, Form, Request, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from urllib.parse import urlparse, unquote, parse_qs
import html as html_parser
import subprocess
import os
import uuid
import shutil
import time
import urllib.request
import json
from typing import List, Optional, Tuple, Dict, Any
import re
import logging
import threading
import unicodedata

APP_VERSION = "1.8.9-dev"

OUTPUT_DIR = "/output"
ALLOWED_MODES = {"audio", "video"}
ALLOWED_FORMATS = {"auto", "wav", "m4a", "mp4", "original"}

# Valfri cookie-fil (Netscape-format), mountas t.ex. via /app/state/cookies.txt
YTDLP_COOKIES = os.environ.get("YTDLP_COOKIES", "").strip()

# Historikfil som överlever omstarter
HISTORY_FILE = "/app/state/history.json"
HISTORY_LOCK = threading.RLock()

# Begränsar tunga parallella jobb. Höj till 2 om NAS:en orkar flera Playwright/yt-dlp-jobb samtidigt.
def _env_int(name: str, default: int, min_value: int = 1, max_value: int = 8) -> int:
    try:
        value = int(os.environ.get(name, str(default)).strip())
        return max(min_value, min(max_value, value))
    except Exception:
        return default

MAX_CONCURRENT_DOWNLOADS = _env_int("LJUDBUSTER_MAX_CONCURRENT_JOBS", 1)
DOWNLOAD_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT_DOWNLOADS)
JOB_LOCK = threading.RLock()

JOBS: Dict[str, Dict[str, Any]] = {}
JOB_TTL_SECONDS = 12 * 60 * 60  # 12h
JOB_MAX_ENTRIES = 500

# --- Startup cleanup config ---
PROCESSING_PREFIX = ".processing_"
PROCESSING_TTL_SECONDS = 6 * 60 * 60  # 6 hours

# --- Logging (docker-friendly) ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ljudbuster")

# --- AB resolver config ---
_AB_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
_AB_REFERER = "https://www.aftonbladet.se/"
_AB_MAX_BODY = 2_000_000

_AB_AKAMAI_ORIGIN = "https://amd-ab.akamaized.net"
_AB_BAD_URL_HINTS = (
    "cookie", "consent", "cmp", "gdpr",
    "ad", "ads", "advert", "promo", "teaser", "bumper",
    "prebid", "freewheel", "videoplaza", "doubleclick",
    "scorecardresearch",
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        now = int(time.time())

        for name in os.listdir(OUTPUT_DIR):
            if not name.startswith(PROCESSING_PREFIX):
                continue

            path = os.path.join(OUTPUT_DIR, name)
            if not os.path.isdir(path):
                continue

            try:
                mtime = int(os.path.getmtime(path))
            except Exception:
                continue

            age = now - mtime
            if age >= PROCESSING_TTL_SECONDS:
                shutil.rmtree(path, ignore_errors=True)

    except Exception:
        pass

    yield

app = FastAPI(lifespan=lifespan)

base_dir = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(base_dir, "templates"))

def _fallback_url_title(url: str) -> str:
    try:
        parsed = urlparse(url)
        path = unquote(parsed.path).strip("/")
        if not path: return parsed.netloc.replace("www.", "")
        
        parts = [p for p in path.split("/") if not re.fullmatch(r"\d{5,15}|[A-Za-z0-9_-]{6,12}", p)]
        clean_parts = [re.sub(r'\s+', ' ', p.replace("-", " ").replace("_", " ")).strip().capitalize() for p in parts if p.lower() not in {"video", "klipp", "artikel", "avsnitt", "a", "live", "film", "play", "watch"}]
        
        if clean_parts: return " – ".join(clean_parts[-2:])
    except Exception: pass
    return url

def _fetch_universal_title(url: str) -> str:
    try:
        from curl_cffi import requests
        resp = requests.get(url, impersonate="chrome", timeout=5.0)
        if resp.status_code == 200:
            page_html = resp.text
            for meta in re.findall(r'<meta[^>]+>', page_html, flags=re.IGNORECASE):
                if 'og:title' in meta:
                    m = re.search(r'content=["\']([^"\']+)["\']', meta, flags=re.IGNORECASE)
                    if m: return html_parser.unescape(m.group(1)).strip()
            
            t_match = re.search(r'<title[^>]*>([^<]+)</title>', page_html, flags=re.IGNORECASE)
            if t_match: return html_parser.unescape(t_match.group(1)).strip()
    except Exception: pass
    return _fallback_url_title(url)

def _read_history_unlocked() -> List[Dict[str, Any]]:
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.error(f"Kunde inte läsa historik: {e}")
        return []


def _write_history_unlocked(history: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    tmp_path = f"{HISTORY_FILE}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, HISTORY_FILE)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def _update_history_item(job_id: str, url: str, updates: Dict[str, Any]) -> None:
    try:
        with HISTORY_LOCK:
            history = _read_history_unlocked()
            if not history:
                return

            target = None
            if job_id:
                for item in history:
                    if item.get("job_id") == job_id:
                        target = item
                        break

            # Fallback för äldre historikrader som saknar job_id.
            if target is None:
                for item in history:
                    if item.get("url") == url:
                        target = item
                        break

            if target is None:
                return

            target.update(updates)
            _write_history_unlocked(history[:50])
    except Exception as e:
        logger.error(f"Kunde inte uppdatera historik: {e}")


def _update_history_title(job_id: str, url: str, real_title: str):
    if real_title:
        _update_history_item(job_id, url, {"title": real_title})


def _update_history_status(job_id: str, url: str, status: str, filename: str = ""):
    updates: Dict[str, Any] = {"status": status}
    if filename:
        updates["filename"] = filename
    _update_history_item(job_id, url, updates)


def _add_to_history(job_id: str, url: str, mode: str, actual_format: str):
    try:
        record = {
            "job_id": job_id,
            "ts": int(time.time()),
            "url": url,
            "title": _fallback_url_title(url),
            "mode": mode,
            "format": actual_format,
            "status": "processing"
        }

        with HISTORY_LOCK:
            history = _read_history_unlocked()
            history.insert(0, record)
            _write_history_unlocked(history[:50])
    except Exception as e:
        logger.error(f"Kunde inte spara historik: {e}")

@app.get("/api/history")
async def get_history():
    with HISTORY_LOCK:
        return _read_history_unlocked()

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "app_version": APP_VERSION})

def _prune_jobs() -> None:
    now = int(time.time())
    try:
        with JOB_LOCK:
            stale = []
            for jid, data in list(JOBS.items()):
                ts = int(data.get("_ts", now))
                if now - ts > JOB_TTL_SECONDS:
                    stale.append(jid)
            for jid in stale:
                JOBS.pop(jid, None)

            if len(JOBS) > JOB_MAX_ENTRIES:
                sorted_jobs = sorted(JOBS.items(), key=lambda kv: int(kv[1].get("_ts", 0)))
                to_drop = len(JOBS) - JOB_MAX_ENTRIES
                for jid, _ in sorted_jobs[:to_drop]:
                    JOBS.pop(jid, None)
    except Exception:
        pass

def _set_job(job_id: str, payload: Dict[str, Any]) -> None:
    payload["_ts"] = int(time.time())
    with JOB_LOCK:
        JOBS[job_id] = payload
    _prune_jobs()

def _clean_last_stderr_line(stderr: str) -> str:
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    return lines[-1] if lines else "Okänt fel i yt-dlp"

def _filename_stem_from_text(value: str, max_len: int = 120) -> str:
    """Returnerar säker lowercase kebab-case utan konstiga tecken."""
    s = html_parser.unescape(str(value or "")).strip()
    for splitter in [" - ", " – ", " | "]:
        if splitter in s:
            s = s.split(splitter, 1)[0]
            break

    # Svensk translitterering före ASCII-normalisering.
    s = s.translate(str.maketrans({
        "å": "a", "ä": "a", "ö": "o",
        "Å": "a", "Ä": "a", "Ö": "o",
        "é": "e", "É": "e",
    }))
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = s.replace("&", " och ")
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    if not s:
        s = "media"
    return s[:max_len].rstrip("-") or "media"


def _sanitize_filename(name: str) -> str:
    # Används av yt-dlp output_template. Behåller funktionsnamnet för minimal diff.
    return _filename_stem_from_text(name, max_len=100)


def _slugify_filename_part(s: str, max_len: int = 120) -> str:
    return _filename_stem_from_text(s, max_len=max_len)


def _source_slug_from_url(url: str) -> str:
    try:
        query_id = ""
        if "?" in url:
            query_part = url.split("?", 1)[1].split("#", 1)[0]
            for param in ["pinnedEntry", "inlagg", "id", "videoId", "assetId"]:
                m = re.search(fr"(?:^|&){param}=([a-zA-Z0-9_-]+)", query_part)
                if m:
                    query_id = f"-{m.group(1)[:12]}"
                    break

        clean = (url or "").split("?", 1)[0].split("#", 1)[0].rstrip("/")
        parts = [p for p in clean.split("/") if p]
        if not parts:
            return "media" + query_id

        last = parts[-1]
        if re.fullmatch(r"[A-Za-z0-9]{4,12}", last) and len(parts) >= 2:
            last = f"{parts[-2]}-{last}"

        return _slugify_filename_part(last, max_len=100) + query_id
    except Exception:
        return "media"


def _classify_ytdlp_error(stderr: str, stdout: str = "") -> str:
    text = ((stderr or "") + "\n" + (stdout or "")).lower()

    if "unsupported url" in text or "no suitable extractor" in text:
        return "Länken stöds inte av yt-dlp (eller kräver annan metod)."
    if "http error 403" in text or "forbidden" in text:
        return "Åtkomst nekad (403). Testa cookies eller kontrollera om klippet är låst."
    if "http error 404" in text or "not found" in text:
        return "Klippet hittades inte (404) eller länken är gammal."
    if "geo" in text and ("blocked" in text or "restriction" in text):
        return "Klippet verkar vara geografiskt blockerat."
    if "sign in" in text or "login" in text or "cookies" in text:
        return "Klippet kräver inloggning/cookies."
    if "fragment" in text and "failed" in text:
        return "HLS-fragmentfel under nedladdning (nätverk/streamproblem)."
    if "timed out" in text or "timeout" in text:
        return "Timeout under nedladdning."
    return ""

def _is_download_artifact(name: str) -> bool:
    lower = name.lower()
    if lower.endswith((".part", ".ytdl", ".tmp", ".temp", ".partial", ".download", ".aria2")):
        return False
    # Råa manifests/texts är inte en färdig mediafil även om yt-dlp råkar returnera 0.
    if lower.endswith((".mpd", ".m3u8", ".json", ".xml", ".txt", ".description", ".info.json")):
        return False
    return True


def _list_finished_files(work_dir: str) -> List[str]:
    finished = []
    if not os.path.isdir(work_dir):
        return finished
    for name in os.listdir(work_dir):
        path = os.path.join(work_dir, name)
        if not os.path.isfile(path):
            continue
        if not _is_download_artifact(name):
            continue
        finished.append(path)

    # Välj sannolik huvudfil först om yt-dlp har lämnat flera artefakter.
    return sorted(finished, key=lambda p: (os.path.getsize(p), os.path.getmtime(p)), reverse=True)


def _atomic_move(src: str, dst: str) -> None:
    try:
        os.replace(src, dst)
    except OSError:
        shutil.move(src, dst)


def _unique_output_path(base_name: str, ext: str) -> Tuple[str, str]:
    safe_base = _slugify_filename_part(base_name, max_len=120)
    safe_ext = (ext or "").lower()
    if not safe_ext.startswith("."):
        safe_ext = f".{safe_ext}" if safe_ext else ".bin"

    final_name = f"{safe_base}{safe_ext}"
    counter = 1
    while os.path.exists(os.path.join(OUTPUT_DIR, final_name)):
        counter += 1
        final_name = f"{safe_base}-{counter}{safe_ext}"

    return os.path.join(OUTPUT_DIR, final_name), final_name


def _publish_final_output(work_dir: str, preferred_base: str) -> str:
    files = _list_finished_files(work_dir)
    if not files:
        raise RuntimeError("Inga färdiga filer hittades i jobbmappen (work_dir).")

    src = files[0]
    detected_base, ext = os.path.splitext(os.path.basename(src))
    final_base = preferred_base or detected_base or "media"
    dst, final_name = _unique_output_path(final_base, ext)
    _atomic_move(src, dst)
    return final_name


def _is_aftonbladet_article(url: str) -> bool:
    u = (url or "").lower()
    return ("aftonbladet.se" in u) and ("/a/" in u or "/live/" in u)

def _extract_media_urls_from_text(blob: str) -> List[str]:
    if not blob:
        return []
    pats = [
        r"https?://[^\s\"']+\.m3u8[^\s\"']*",
        r"https?://[^\s\"']+\.mpd[^\s\"']*",
        r"https?://[^\s\"']+\.mp4[^\s\"']*",
        r"https?://[^\s\"']+\.m4a[^\s\"']*",
        r"https?://[^\s\"']+\.mp3[^\s\"']*",
    ]
    out: List[str] = []
    for pat in pats:
        out.extend(re.findall(pat, blob, flags=re.I))

    seen = set()
    uniq = []
    for u in out:
        if u in seen:
            continue
        seen.add(u)
        uniq.append(u)
    return uniq

def _extract_ab_vod_paths_from_text(blob: str) -> List[str]:
    if not blob:
        return []
    s = blob
    found: List[str] = []

    for pat in [
        r"(/ab/vod/\d{4}/\d{2}/[A-Za-z0-9_-]+/[^\s\"']+_pkg\.m3u8)",
        r"(/ab/vod/\d{4}/\d{2}/[A-Za-z0-9_-]+/master\.m3u8)",
        r"(/ab/vod/\d{4}/\d{2}/[A-Za-z0-9_-]+/[^\"\s']+\.mpd)",
        r"(/ab/vod/\d{4}/\d{2}/[A-Za-z0-9_-]+/[^\s\"']+_pkg\.mp4)",
        r"(/ab/vod/\d{4}/\d{2}/[A-Za-z0-9_-]+/[^\s\"']+\.mp4)",
    ]:
        for m in re.findall(pat, s, flags=re.I):
            found.append(_AB_AKAMAI_ORIGIN + m)

    for m in re.findall(r"(/ab/vod/\d{4}/\d{2}/[A-Za-z0-9_-]+/)", s, flags=re.I):
        found.append(_AB_AKAMAI_ORIGIN + m + "master.m3u8")

    seen = set()
    uniq = []
    for u in found:
        if u in seen:
            continue
        seen.add(u)
        uniq.append(u)
    return uniq

def _ab_is_bad_candidate_url(u: str) -> bool:
    ul = (u or "").lower()
    return any(h in ul for h in _AB_BAD_URL_HINTS)

def _score_stream_url(u: str) -> Tuple[int, int, int, int, int]:
    ul = (u or "").lower()

    bad = -1 if _ab_is_bad_candidate_url(ul) else 0

    # --- SVT Candidate Ranking Fix v1.7.10 ---
    if "svt-vod-" in ul or "switcher.cdn.svt.se" in ul or "video.svt.se" in ul:
        if "hls-cmaf-avc" in ul:
            return (bad, 10_000_000, 1080, 0, 5)
        if "hls-ts-full" in ul:
            return (bad, 9_000_000, 1080, 0, 5)
        if "hls-cmaf-full" in ul:
            return (bad, 8_000_000, 1080, 0, 4)
        if ".m3u8" in ul:
            return (bad, 7_000_000, 720, 0, 4)
        if "dash-avc" in ul:
            return (bad, 2_000_000, 720, 0, 2)
        if ".mpd" in ul:
            return (bad, 1_000_000, 720, 0, 1)

    direct_mp4_bonus = 0
    if ul.endswith(".mp4") and ("_pkg" in ul or "/ps_" in ul):
        direct_mp4_bonus = 5_000_000

    m = re.search(r"/(\d{3,4})_(\d{3,5})_pkg\.(?:m3u8|mp4)", ul)
    if m:
        h = int(m.group(1))
        br = int(m.group(2))
        return (bad, br + direct_mp4_bonus, h, 0, 3 if ul.endswith(".mp4") else 2)

    m = re.search(r"/(?:ps_)?(\d{3,4})_(\d{3,4})(?:_(\d{3,5}))?\.(?:m3u8|mp4)", ul)
    if m:
        h = int(m.group(1))
        w_or_other = int(m.group(2))
        br = int(m.group(3)) if m.group(3) else 0
        return (bad, br + direct_mp4_bonus, h, w_or_other, 3 if ul.endswith(".mp4") else 2)

    if ul.endswith("master.m3u8"):
        return (bad, -1, -1, -1, 1)
    if ul.endswith(".mpd"):
        return (bad, -2, -2, -2, 0)
    if ul.endswith(".mp4"):
        return (bad, 1_000_000, 0, 0, 3)
    if ul.endswith(".m4a") or ul.endswith(".mp3"):
        return (bad, 900_000, 0, 0, 3)
    if ".m3u8" in ul:
        return (bad, -3, -3, -3, 1)
    if "trailers.filmstaden.se" in ul:
        return (bad, 1_500_000, 0, 0, 4)
    if "youtube.com" in ul or "youtu.be" in ul or "vimeo.com" in ul:
        return (bad, 500_000, 0, 0, 2)

    return (bad, -9, -9, -9, -9)


def _pick_best_media_url(urls: List[str]) -> Optional[str]:
    if not urls:
        return None
    clean = [u for u in urls if not _ab_is_bad_candidate_url(u)]
    pool = clean or urls
    return sorted(pool, key=_score_stream_url, reverse=True)[0]

def _fetch_html(article_url: str) -> str:
    req = urllib.request.Request(article_url, headers={
        "User-Agent": _AB_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "sv-SE,sv;q=0.9,en-US;q=0.8,en;q=0.7"
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read(_AB_MAX_BODY)
    return raw.decode("utf-8", errors="ignore")

def _collect_ab_candidates_from_html(clean_html: str) -> List[str]:
    candidates: List[str] = []
    candidates.extend(_extract_media_urls_from_text(clean_html))
    candidates.extend(_extract_ab_vod_paths_from_text(clean_html))

    extra = re.findall(
        r'https?://[^"\'>\s]*akamaized\.net/ab/(?:vod|live)/[^"\'>\s]+\.(?:m3u8|mp4|mpd)(?:\?[^"\'>\s]*)?',
        clean_html,
        flags=re.I,
    )
    candidates.extend(extra)

    seen = set()
    out = []
    for c in candidates:
        c2 = c.replace("\\/", "/")
        if c2 in seen:
            continue
        seen.add(c2)
        out.append(c2)
    return out

def _get_aftonbladet_direct_url(job_id: str, article_url: str) -> Optional[str]:
    try:
        logger.info(f"[{job_id}] AB Resolver: Skannar artikelkoden efter direkta strömmar...")
        html = _fetch_html(article_url)
        clean_html = html.replace("\\/", "/")

        json_match = re.search(r'"(?:videoId|liveId|videoAssetId)"\s*:\s*"([a-zA-Z0-9_-]{5,})"', clean_html)
        if json_match:
            vid = json_match.group(1)
            logger.info(f"[{job_id}] BINGO! Hittade Schibsted-ID ({vid}), skapar tv-länk...")
            return f"https://tv.aftonbladet.se/video/{vid}"

        iframe_match = re.search(r'tv\.aftonbladet\.se/iframe/(?:video|live)/([a-zA-Z0-9_-]+)', clean_html)
        if iframe_match:
            logger.info(f"[{job_id}] BINGO! Hittade iFrame-länk...")
            return f"https://tv.aftonbladet.se/video/{iframe_match.group(1)}"

        candidates = _collect_ab_candidates_from_html(clean_html)
        best_url = _pick_best_media_url(candidates)
        if best_url:
            logger.info(f"[{job_id}] Hittade/rankade Akamai-kandidat: {best_url}")
            return best_url

    except Exception as e:
        logger.info(f"[{job_id}] API-skrapan misslyckades: {e}")

    return None

def _get_tv4_direct_url(job_id: str, article_url: str) -> Optional[str]:
    try:
        logger.info(f"[{job_id}] TV4 Resolver: Skannar artikelkoden efter Video-ID...")
        
        req = urllib.request.Request(article_url, headers={
            "User-Agent": _AB_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "sv-SE,sv;q=0.9,en-US;q=0.8,en;q=0.7",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        
        clean_html = html.replace("\\/", "/")

        vid = None
        patterns = [
            r'tv4\.se/klipp/[^/]+/(\d{7,8})',
            r'tv4play\.se/(?:iframe/)?video/(\d{7,8})',
            r'(?:"videoAssetId"|"videoId"|data-video-id|"assetId"|"b17gId")\s*[:=]\s*["\']?(\d{7,8})["\']?',
            r'VideoAsset:(\d{7,8})',
            r'"video"\s*:\s*\{\s*"id"\s*:\s*"(\d{7,8})"',
            r'video_id[=:]\s*["\']?(\d{7,8})["\']?',
            r'\"vid\"\s*:\s*\"(\d{7,8})\"',
            r'\"id\"[^\d]+(\d{7,8})'
        ]
        
        for pat in patterns:
            match = re.search(pat, clean_html, flags=re.IGNORECASE)
            if match:
                vid = match.group(1)
                break

        if not vid:
            logger.info(f"[{job_id}] TV4 Sniper missade: Hittade inget Video-ID i HTML-koden.")
            return None

        logger.info(f"[{job_id}] BINGO! Hittade TV4 Video-ID: {vid}")

        api_url = f"https://playback2.a2d.tv/play/{vid}?service=tv4play&device=browser&protocol=hls"
        
        req_api = urllib.request.Request(api_url, headers={
            "User-Agent": _AB_UA,
            "Accept": "application/json",
            "Origin": "https://www.tv4.se",
            "Referer": "https://www.tv4.se/"
        })
        
        with urllib.request.urlopen(req_api, timeout=10) as resp_api:
            data = json.loads(resp_api.read().decode("utf-8"))
        
        if "playbackItem" in data:
            manifest_url = data["playbackItem"].get("manifestUrl")
            if manifest_url:
                logger.info(f"[{job_id}] TV4 Sniper: Hittade rent manifest!")
                return manifest_url
            
            access_url = data["playbackItem"].get("accessUrl")
            if access_url:
                logger.info(f"[{job_id}] TV4 Sniper: Hittade accessUrl (Prism-session)...")
                return access_url

    except Exception as e:
        logger.info(f"[{job_id}] TV4 API-skrapan misslyckades: {e}")

    return None

def _get_sr_direct_url(job_id: str, article_url: str) -> Optional[str]:
    try:
        logger.info(f"[{job_id}] SR Resolver: Letar efter <audio preload> (topsy) tagg...")
        from curl_cffi import requests
        
        resp = requests.get(article_url, impersonate="chrome", timeout=15)
        clean_html = resp.text.replace("\\/", "/")
        
        topsy_pattern = r'(https?://(?:www\.)?sverigesradio\.se/topsy/ljudfil/[^\s"\'<>]+|/topsy/ljudfil/[^\s"\'<>]+)'
        match = re.search(topsy_pattern, clean_html, flags=re.I)
        
        if match:
            best_url = match.group(1)
            if best_url.startswith("/"):
                best_url = "https://www.sverigesradio.se" + best_url
                
            logger.info(f"[{job_id}] BINGO! Hittade dold topsy-länk direkt i HTML: {best_url}")
            return best_url
            
        logger.info(f"[{job_id}] SR Resolver: Hittade tyvärr ingen topsy-länk.")
    except Exception as e:
        logger.error(f"[{job_id}] SR Resolver kraschade: {e}")

    return None

def _pick_svt_video_reference(video_refs: List[Dict[str, Any]]) -> Optional[str]:
    if not video_refs:
        return None
    preferred_formats = [
        "hls-cmaf-avc",
        "hls-ts-full",
        "hls",
        "hls-cmaf-full",
        "dash-avc",
        "dash",
    ]
    for wanted in preferred_formats:
        for ref in video_refs:
            fmt = (ref.get("format") or "").lower()
            u = ref.get("url") or ref.get("resolve") or ref.get("redirect") or ""
            if fmt == wanted and (".m3u8" in u or ".mpd" in u):
                return u
    for ref in video_refs:
        u = ref.get("url") or ref.get("resolve") or ref.get("redirect") or ""
        if ".m3u8" in u or ".mpd" in u:
            return u
    return None


def _extract_svt_manifest_urls_from_text(blob: str) -> List[str]:
    """Plockar ut riktiga SVT-manifest ur HTML/URL/API-wrapper.

    SVT bäddar ofta in riktig HLS i:
    https://api.svt.se/ditto/api/v3/manifest?manifestUrl=<ENCODED_HLS>&platform=...
    """
    if not blob:
        return []

    s = html_parser.unescape(str(blob)).replace("\\/", "/")
    out: List[str] = []

    # 1) Full api.svt.se/ditto-wrapper.
    for api_url in re.findall(r'https?://api\.svt\.se/ditto/api/v3/manifest\?[^"\'<>\s]+', s, flags=re.I):
        try:
            qs = parse_qs(urlparse(api_url).query)
            for u in qs.get("manifestUrl", []):
                if u:
                    out.append(unquote(u))
        except Exception:
            pass

    # 2) Rå manifestUrl=... i HTML/JS.
    for m in re.finditer(r'manifestUrl=([^&"\'<>\s]+)', s, flags=re.I):
        try:
            out.append(unquote(m.group(1)))
        except Exception:
            pass

    # 3) JSON-liknande manifestUrl.
    for m in re.finditer(r'"manifestUrl"\s*:\s*"([^"]+)"', s, flags=re.I):
        try:
            out.append(unquote(m.group(1)))
        except Exception:
            pass

    # 4) Redan direkta SVT-media-URL:er.
    for u in _extract_media_urls_from_text(s):
        ul = u.lower()
        if "svt-vod-" in ul or "switcher.cdn.svt.se" in ul or "video.svt.se" in ul:
            out.append(u)

    clean: List[str] = []
    seen = set()
    for u in out:
        u2 = html_parser.unescape(str(u)).replace("\\/", "/").strip()
        if not u2:
            continue
        if not (".m3u8" in u2.lower() or ".mpd" in u2.lower()):
            continue
        if u2 in seen:
            continue
        seen.add(u2)
        clean.append(u2)

    return clean


def _extract_svt_livepost_manifest_from_html(page_html: str, inlagg_id: str) -> Optional[str]:
    """Hittar manifest inne i exakt rätt SVT-livepost-container."""
    if not page_html or not inlagg_id:
        return None

    s = html_parser.unescape(page_html).replace("\\/", "/")

    marker = re.search(
        rf'<div[^>]+id=["\']{re.escape(inlagg_id)}["\'][^>]*>',
        s,
        flags=re.I,
    )
    if not marker:
        return None

    # Ta ett lokalt fönster från rätt post. Stort nog för video-wrappern,
    # men litet nog för att inte börja ranka hela liveflödet.
    chunk = s[marker.start(): marker.start() + 180_000]

    candidates = _extract_svt_manifest_urls_from_text(chunk)
    if not candidates:
        return None

    return _pick_best_media_url(candidates)

def _get_svt_direct_url(job_id: str, article_url: str) -> Optional[str]:
    try:
        logger.info(f"[{job_id}] SVT Apollo Cracker: Startar isolering av inlägg...")
        svt_id = None
        
        parsed_url = urlparse(article_url)
        query_params = parse_qs(parsed_url.query)
        inlagg_id = query_params.get('inlagg', [None])[0]

        direct_video_match = re.search(r'video\.svt\.se/video/([a-zA-Z0-9_-]+)', article_url)
        if direct_video_match:
            svt_id = direct_video_match.group(1)

        if not svt_id:
            from curl_cffi import requests
            resp = requests.get(article_url, impersonate="chrome", timeout=15)
            html = resp.text

            if inlagg_id:
                direct_manifest = _extract_svt_livepost_manifest_from_html(html, inlagg_id)
                if direct_manifest:
                    logger.info(f"[{job_id}] SVT Livepost: hittade manifest direkt i rätt inlägg: {direct_manifest}")
                    return direct_manifest

            if inlagg_id:
                logger.info(f"[{job_id}] Söker djupt i databasen efter inlägg: {inlagg_id}")
                m = re.search(r'<script id="__NEXT_DATA__".*?>(.*?)</script>', html, flags=re.DOTALL)
                if m:
                    try:
                        data = json.loads(m.group(1))
                        def extract_vids(obj, is_target=False):
                            vids = []
                            if isinstance(obj, dict):
                                current_is_target = is_target
                                if not current_is_target:
                                    for k, v in obj.items():
                                        if isinstance(v, str) and inlagg_id in v:
                                            current_is_target = True
                                            break
                                if current_is_target:
                                    s = json.dumps(obj)
                                    for match in re.finditer(r'(?:Video|SvtVideo|VideoAsset):([a-zA-Z0-9_-]{5,30})', s):
                                        vids.append(match.group(1))
                                    for match in re.finditer(r'(?:"videoAssetId"|"svtId"|"videoId")\s*:\s*"([a-zA-Z0-9_-]{5,30})"', s):
                                        vids.append(match.group(1))
                                for k, v in obj.items():
                                    vids.extend(extract_vids(v, current_is_target))
                            elif isinstance(obj, list):
                                for item in obj:
                                    vids.extend(extract_vids(item, is_target))
                            return vids
                            
                        found_vids = extract_vids(data)
                        if found_vids:
                            svt_id = found_vids[0]
                            logger.info(f"[{job_id}] CRACKED! Hittade videoreferens för inlägget: {svt_id}")
                    except Exception as e:
                        logger.error(f"[{job_id}] JSON-parsning misslyckades: {e}")

            if not svt_id and not inlagg_id:
                html_match = re.search(r'(?:"videoAssetId"|"svtId"|"videoId"|"urn")\s*:\s*"([a-zA-Z0-9_-]{6,40})"', html)
                if html_match: svt_id = html_match.group(1)

        if svt_id:
            logger.info(f"[{job_id}] Hämtar säkert manifest för SVT-ID: {svt_id}")
            from curl_cffi import requests
            
            if len(svt_id) >= 36:
                switcher_url = f"https://switcher.cdn.svt.se/resolve/{svt_id}/hls-cmaf-avc.m3u8"
                resp_sw = requests.get(switcher_url, impersonate="chrome", timeout=5)
                if resp_sw.status_code in [200, 302]: return switcher_url
            
            api_url = f"https://video.svt.se/video/{svt_id}"
            api_resp = requests.get(api_url, impersonate="chrome", timeout=10)
            if api_resp.status_code == 200:
                data = api_resp.json()
                picked = _pick_svt_video_reference(data.get("videoReferences") or [])
                if picked: return picked

        if inlagg_id and not svt_id:
            logger.info(f"[{job_id}] Inlägget verkar inte innehålla någon egen video.")
            return "__ABORT_NO_VIDEO__"

    except Exception as e:
        logger.error(f"[{job_id}] SVT Apollo kraschade: {e}")
    return None

def _resolve_manifests_via_playwright(job_id: str, article_url: str) -> List[str]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        logger.info(f"[{job_id}] Resolver: playwright import fail: {e}")
        return []

    parsed_article = urlparse(article_url)
    query = parse_qs(parsed_article.query)
    inlagg_id = query.get("inlagg", [None])[0]
    is_svt_page = "svt.se" in (article_url or "").lower() or "svtplay.se" in (article_url or "").lower()

    found: List[str] = []

    def _dedupe(items: List[str]) -> List[str]:
        seen = set()
        out: List[str] = []
        for item in items:
            if not item or item in seen:
                continue
            seen.add(item)
            out.append(item)
        return out

    def _unwrap_svt_manifest_urls(blob: str) -> List[str]:
        if not blob:
            return []

        s = html_parser.unescape(str(blob)).replace("\\/", "/")
        out: List[str] = []

        def push(u: str) -> None:
            u = html_parser.unescape(str(u or "")).replace("\\/", "/").strip()
            if not u or u.startswith("blob:"):
                return
            if u not in out:
                out.append(u)

        for api_url in re.findall(r"https?://api\.svt\.se/ditto/api/v3/manifest\?[^\"'<>\\s]+", s, flags=re.I):
            try:
                qs = parse_qs(urlparse(api_url).query)
                for real_url in qs.get("manifestUrl", []):
                    push(unquote(real_url))
            except Exception:
                pass

        for m in re.finditer(r"manifestUrl=([^&\"'<>\\s]+)", s, flags=re.I):
            try:
                push(unquote(m.group(1)))
            except Exception:
                pass

        for m in re.finditer(r'"manifestUrl"\s*:\s*"([^"]+)"', s, flags=re.I):
            try:
                push(unquote(m.group(1)))
            except Exception:
                pass

        for u in re.findall(r"https?://(?:svt-vod-[^\"'<>\\s]+|switcher\.cdn\.svt\.se/resolve/[^\"'<>\\s]+)", s, flags=re.I):
            push(u)

        return _dedupe(out)

    def _is_candidate_url(u: str) -> bool:
        ul = (u or "").lower()
        if not ul or ul.startswith("blob:"):
            return False
        if "api.svt.se/ditto/api/v3/manifest" in ul:
            return False
        if any(bad in ul for bad in ["adserver", "freewheel", "videoplaza", "doubleclick", "scorecardresearch", "imrworldwide"]):
            return False
        if any(ext in ul for ext in [".m3u8", ".mpd", ".mp4", ".m4a", ".mp3"]):
            return True
        if any(domain in ul for domain in ["switcher.cdn.svt.se/resolve/", "trailers.filmstaden.se", "player.vimeo.com/video/", "youtube.com/embed/", "youtube-nocookie.com/embed/", "youtube.com/watch", "youtu.be/"]):
            return True
        return False

    def _add_to(pool: List[str], u: str) -> None:
        if not u:
            return

        raw = html_parser.unescape(str(u)).replace("\\/", "/").strip()
        if not raw or raw.startswith("blob:"):
            return

        if "api.svt.se/ditto/api/v3/manifest" in raw:
            for real_url in _unwrap_svt_manifest_urls(raw):
                _add_to(pool, real_url)
            return

        m = re.search(r"https://switcher\.cdn\.svt\.se/resolve/([0-9a-fA-F-]{36})/", raw)
        if m:
            uuid_val = m.group(1)
            hls_full = f"https://switcher.cdn.svt.se/resolve/{uuid_val}/hls-cmaf-full.m3u8"
            if hls_full not in pool:
                pool.append(hls_full)

        if _is_candidate_url(raw) and raw not in pool:
            pool.append(raw)

        for real_url in _unwrap_svt_manifest_urls(raw):
            if real_url not in pool:
                pool.append(real_url)

    def _add(u: str) -> None:
        _add_to(found, u)

    def _extract_scoped_svt_dom(page, post_id: str) -> Dict[str, Any]:
        return page.evaluate(
            """
            (postId) => {
                const out = {
                    readyState: document.readyState,
                    htmlHasId: document.documentElement.innerHTML.includes(postId),
                    container: false,
                    videoSrcs: [],
                    urls: [],
                    buttonLabels: [],
                    textSample: ""
                };

                const node = document.getElementById(postId);
                if (!node) return out;

                out.container = true;
                out.textSample = (node.innerText || "").slice(0, 500);

                for (const video of Array.from(node.querySelectorAll("video"))) {
                    out.videoSrcs.push(video.currentSrc || video.src || video.getAttribute("src") || "");
                }

                for (const btn of Array.from(node.querySelectorAll("button"))) {
                    out.buttonLabels.push(btn.getAttribute("aria-label") || btn.getAttribute("data-rt") || btn.innerText || "");
                }

                const html = node.innerHTML || "";
                out.urls = html.match(/https?:\\/\\/[^"'<>\\s)]+/g) || [];
                return out;
            }
            """,
            post_id,
        )

    def _extract_scoped_candidates(page, post_id: str, label: str) -> Tuple[List[str], Dict[str, Any]]:
        scoped: List[str] = []
        info: Dict[str, Any] = {}

        try:
            info = _extract_scoped_svt_dom(page, post_id)
        except Exception as e:
            logger.info(f"[{job_id}] SVT DOM-first: evaluate misslyckades ({label}): {e}")
            return [], {}

        logger.info(
            f"[{job_id}] SVT DOM-first ({label}): "
            f"container={info.get('container')} "
            f"htmlHasId={info.get('htmlHasId')} "
            f"videos={len(info.get('videoSrcs') or [])} "
            f"urls={len(info.get('urls') or [])} "
            f"buttons={len(info.get('buttonLabels') or [])}"
        )

        for src in info.get("videoSrcs") or []:
            _add_to(scoped, src)

        for u in info.get("urls") or []:
            _add_to(scoped, u)

        scoped = _dedupe(scoped)
        if scoped:
            logger.info(f"[{job_id}] SVT DOM-first ({label}): kandidater={len(scoped)} första={scoped[0]}")

        return scoped, info

    def _click_scoped_svt_play(page, post_id: str) -> str:
        try:
            return page.evaluate(
                """
                (postId) => {
                    const node = document.getElementById(postId);
                    if (!node) return "no-container";

                    const selectors = [
                        '[data-testid="play-pause-button"]',
                        'button[data-rt="video-player-splash-play"]',
                        'button[aria-label*="Spela"]',
                        'button[title*="Spela"]',
                        'video'
                    ];

                    for (const sel of selectors) {
                        const el = node.querySelector(sel);
                        if (!el) continue;

                        el.scrollIntoView({block: "center", inline: "center"});

                        if (el.tagName && el.tagName.toLowerCase() === "video") {
                            try {
                                el.play();
                                return "video-play-called";
                            } catch (e) {
                                return "video-play-failed";
                            }
                        }

                        try {
                            el.click();
                            return "clicked:" + sel;
                        } catch (e) {
                            return "click-failed:" + sel;
                        }
                    }

                    return "no-button";
                }
                """,
                post_id,
            )
        except Exception as e:
            return f"exception:{e}"

    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            ctx = b.new_context(
                user_agent=_AB_UA,
                viewport={"width": 1440, "height": 1600},
                extra_http_headers={"Accept-Language": "sv-SE,sv;q=0.9,en-US;q=0.8,en;q=0.7"},
            )
            page = ctx.new_page()

            def on_request(req):
                try:
                    req_url = req.url

                    if "api.svt.se/ditto/api/v3/manifest" in req_url:
                        for real_url in _unwrap_svt_manifest_urls(req_url):
                            _add(real_url)

                    if "switcher.cdn.svt.se/resolve/" in req_url:
                        _add(req_url)

                    if _is_candidate_url(req_url):
                        for media_u in _extract_media_urls_from_text(req_url):
                            _add(media_u)
                        _add(req_url)

                    if "svt-vod-" in req_url and "akamaized.net" in req_url:
                        if ".m3u8" in req_url or ".mpd" in req_url:
                            logger.info(f"[{job_id}] [FISKENÄTET] Hittade SVT manifest: {req_url}")
                            _add(req_url)
                        elif "cmaf" in req_url and "init.mp4" in req_url:
                            base_match = re.match(r"(https://svt-vod-[a-zA-Z0-9.-]+/.*?[0-9a-fA-F-]{36}/)", req_url)
                            if base_match:
                                guessed = base_match.group(1) + "hls-cmaf-full.m3u8"
                                logger.info(f"[{job_id}] [FISKENÄTET] Rekonstruerad SVT manifest-länk: {guessed}")
                                _add(guessed)
                except Exception:
                    pass

            def on_response(res):
                try:
                    res_url = res.url

                    if "playback2.a2d.tv/play/" in res_url:
                        data = res.json()
                        if "playbackItem" in data:
                            m = data["playbackItem"].get("manifestUrl") or data["playbackItem"].get("accessUrl")
                            if m:
                                _add(m)

                    if "video.svt.se/video/" in res_url:
                        logger.info(f"[{job_id}] Snipern fångade SVT API-anrop!")
                        data = res.json()
                        if "videoReferences" in data:
                            for ref in data["videoReferences"]:
                                u = ref.get("url") or ref.get("resolve") or ref.get("redirect")
                                if u:
                                    _add(u)

                    content_type = res.headers.get("content-type", "")
                    if "application/json" in content_type:
                        body = res.text()
                        for media_u in _extract_media_urls_from_text(body):
                            _add(media_u)
                        for real_url in _unwrap_svt_manifest_urls(body):
                            _add(real_url)
                except Exception:
                    pass

            page.on("request", on_request)
            page.on("response", on_response)

            logger.info(f"[{job_id}] Playwright-fiskenät aktiverat! Skrapar {article_url}...")

            try:
                page.goto(article_url, wait_until="domcontentloaded", timeout=25_000)
            except Exception as e:
                logger.info(f"[{job_id}] Playwright goto varnade: {e}")

            try:
                page.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:
                pass

            if is_svt_page and inlagg_id:
                logger.info(f"[{job_id}] SVT DOM-first: söker exakt inlägg [id=\"{inlagg_id}\"]")

                for attempt in range(1, 7):
                    scoped, info = _extract_scoped_candidates(page, inlagg_id, f"attempt-{attempt}")
                    if scoped:
                        ctx.close()
                        b.close()
                        return scoped

                    if info.get("container"):
                        break

                    try:
                        page.evaluate("window.scrollBy(0, Math.max(window.innerHeight, 900));")
                    except Exception:
                        pass

                    for sel in [
                        'button:has-text("Fler händelser")',
                        'button:has-text("Visa fler")',
                        'button:has-text("Visa inlägget")',
                        'button[aria-label*="Visa inlägget"]',
                    ]:
                        try:
                            page.click(sel, timeout=900)
                            page.wait_for_timeout(700)
                            break
                        except Exception:
                            pass

                    page.wait_for_timeout(900)

                found.clear()
                click_result = _click_scoped_svt_play(page, inlagg_id)
                logger.info(f"[{job_id}] SVT DOM-first: scoped klickresultat: {click_result}")

                for i in range(1, 12):
                    page.wait_for_timeout(700)
                    scoped, info = _extract_scoped_candidates(page, inlagg_id, f"after-click-{i}")
                    if scoped:
                        ctx.close()
                        b.close()
                        return scoped

                    if found and click_result.startswith(("clicked:", "video-play-called")):
                        logger.info(f"[{job_id}] SVT DOM-first: scoped klick gav nätverkskandidater={len(found)} första={found[0]}")
                        ctx.close()
                        b.close()
                        return _dedupe(found)

                logger.info(f"[{job_id}] SVT DOM-first: ingen scoped stream hittades. Avbryter utan global fallback för att undvika fel video.")
                ctx.close()
                b.close()
                return []

            try:
                page_html = page.content()
                for media_u in _extract_media_urls_from_text(page_html):
                    _add(media_u)
                for real_url in _unwrap_svt_manifest_urls(page_html):
                    _add(real_url)
            except Exception:
                pass

            for _ in range(16):
                if found:
                    logger.info(f"[{job_id}] Fiskenätet fångade direktlänk, avbryter sidladdning!")
                    ctx.close()
                    b.close()
                    return _dedupe(found)
                page.wait_for_timeout(500)

            for sel in [
                'button:has-text("Godkänn alla cookies")',
                'button:has-text("Godkänn")',
                'button:has-text("Okej")',
                'button:has-text("Acceptera")',
                'button:has-text("Tillåt alla")',
            ]:
                try:
                    page.click(sel, timeout=1000)
                    break
                except Exception:
                    pass

            for frame in [page] + page.frames:
                for sel in [
                    'button:has-text("Trailer")',
                    'button[aria-label*="Trailer"]',
                    'a:has-text("Trailer")',
                    'div:has-text("Spela trailer")',
                    '.play-icon',
                    ".jw-video",
                    'button:has-text("Spela")',
                    'button[aria-label*="Spela"]',
                    'button[title*="Spela"]',
                    ".vgtv-player",
                    "video",
                    ".vjs-big-play-button",
                    'button[class*="play-button"]',
                    'button[class*="PlayButton"]',
                    '[data-testid="play-pause-button"]',
                    'button[data-rt="video-player-splash-play"]',
                ]:
                    try:
                        frame.click(sel, timeout=1000)
                    except Exception:
                        pass

            for _ in range(15):
                if found:
                    logger.info(f"[{job_id}] Fiskenätet fångade ström efter klick!")
                    break
                page.wait_for_timeout(1000)

            ctx.close()
            b.close()

    except Exception as e:
        logger.info(f"[{job_id}] Fiskenätet (Playwright) stötte på ett problem: {e}")

    return _dedupe(found)


def _is_direct_media(url: str) -> bool:
    ul = (url or "").lower()
    return (".m3u8" in ul) or ul.endswith(".mpd") or ul.endswith(".mp4") or ul.endswith(".m4a") or ul.endswith(".mp3")

def _yt_dlp_headers_for(url: str) -> List[str]:
    ul = (url or "").lower()
    headers = ["--add-header", f"User-Agent:{_AB_UA}"]

    # Viktigt: SVT måste ligga före generell Akamai/Aftonbladet-logik.
    # Annars kan svt-vod-*.akamaized.net råka få Aftonbladet-referer.
    if any(domain in ul for domain in ["svt.se", "svtplay.se", "video.svt.se", "svt-vod-", "switcher.cdn.svt.se"]):
        headers.extend(["--add-header", "Referer:https://www.svt.se/"])
        headers.extend(["--add-header", "Origin:https://www.svt.se"])
    elif any(domain in ul for domain in ["amd-ab.akamaized.net", "dd-ab.akamaized.net", "aftonbladet.se", "schibsted"]):
        headers.extend(["--add-header", f"Referer:{_AB_REFERER}"])
    elif any(domain in ul for domain in ["a2d.tv", "b17g.net", "tv4play.se", "tv4.se", "cmore.se"]):
        headers.extend(["--add-header", "Referer:https://www.tv4.se/"])

    return headers

def _yt_dlp_cookie_args() -> List[str]:
    if not YTDLP_COOKIES:
        return []
    if os.path.isfile(YTDLP_COOKIES):
        return ["--cookies", YTDLP_COOKIES]
    logger.info(f"YTDLP_COOKIES satt men fil saknas: {YTDLP_COOKIES}")
    return []

def _run_yt_dlp(cmd: List[str], timeout_sec: int = 1800) -> subprocess.CompletedProcess:
    logger.info("Kör yt-dlp: %s", " ".join(cmd[:12]) + (" ..." if len(cmd) > 12 else ""))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)

def process_download(job_id: str, url: str, mode: str, actual_format: str, source_slug: str):
    work_dir = os.path.join(OUTPUT_DIR, f"{PROCESSING_PREFIX}{job_id}")
    acquired_slot = False
    try:
        logger.info(f"[{job_id}] Väntar på ledig download-slot ({MAX_CONCURRENT_DOWNLOADS} samtidigt)...")
        _set_job(job_id, {"status": "processing", "message": "Väntar i kö..."})
        DOWNLOAD_SEMAPHORE.acquire()
        acquired_slot = True
        _set_job(job_id, {"status": "processing", "message": "Hämtar media..."})

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        os.makedirs(work_dir, exist_ok=True)
        _prune_jobs()

        # --- HÄMTA RUBRIK I BAKGRUNDEN ---
        bg_title = _fetch_universal_title(url)
        _update_history_title(job_id, url, bg_title)
        # ---------------------------------------

        logger.info(f"[{job_id}] START url={url} mode={mode} format={actual_format}")

        is_ab = _is_aftonbladet_article(url)
        is_tv4 = "tv4.se" in (url or "").lower() or "tv4play.se" in (url or "").lower()
        is_sr = "sverigesradio.se" in (url or "").lower() or "sr.se" in (url or "").lower()
        is_svt = "svt.se" in (url or "").lower() or "svtplay.se" in (url or "").lower()
        
        is_direct_sniper = any(d in (url or "").lower() for d in ["filmstaden.se"])

        resolved_url = None
        current_url = url

        if is_ab:
            resolved_url = _get_aftonbladet_direct_url(job_id, url)
        elif is_tv4:
            resolved_url = _get_tv4_direct_url(job_id, url)
        elif is_sr:
            resolved_url = _get_sr_direct_url(job_id, url)
        elif is_svt:
            resolved_url = _get_svt_direct_url(job_id, url)
            if resolved_url == "__ABORT_NO_VIDEO__":
                logger.info(f"[{job_id}] Snabba spåret hittade inget direkt ID. Tvingar Fiskenätet att ta över!")
                resolved_url = None
                current_url = "https://127.0.0.1/force_fail_to_trigger_playwright" # Får yt-dlp att krascha direkt, vilket utlöser Playwright
        elif is_direct_sniper:
            logger.info(f"[{job_id}] Spärrlistad domän upptäckt! Bypassar yt-dlp helt och skickar Playwright direkt för att undvika 403-blockering...")
            candidates = _resolve_manifests_via_playwright(job_id, url)
            resolved_url = _pick_best_media_url(candidates)
            if not resolved_url:
                logger.info(f"[{job_id}] Playwright kunde inte hitta den dolda mediaströmmen.")

        if resolved_url:
            current_url = resolved_url
            logger.info(f"[{job_id}] API/Sniper hittade direktlänk -> {current_url}")

        def run_ytdlp_attempt(target_url: str, use_slug_filename: bool):
            is_svt = "svt.se" in (url or "").lower()
            SVT_SAFE_VIDEO = "bv*[vcodec^=avc1][ext=mp4]+ba/b[ext=mp4]/b"

            base_cmd = [
                "python", "-m", "yt_dlp",
                "--no-playlist",
                "--restrict-filenames",
                "--trim-filenames", "100",
                "--js-runtimes", "node",
                "--socket-timeout", "20",
                "--retries", "10",
                "--file-access-retries", "5",
                "--fragment-retries", "20",
                "--extractor-retries", "5",
                "--skip-unavailable-fragments",
                "--concurrent-fragments", "4",
                "--hls-split-discontinuity",
                
            ]

            cookie_args = _yt_dlp_cookie_args()
            
            if use_slug_filename:
                safe_title = _sanitize_filename(bg_title) if bg_title else _slugify_filename_part(source_slug, max_len=140)
                if mode == "video" and (target_url or "").lower().endswith(".mp4"):
                    output_template = os.path.join(work_dir, f"{safe_title}.mp4")
                else:
                    output_template = os.path.join(work_dir, f"{safe_title}.%(ext)s")
            else:
                output_template = os.path.join(work_dir, "%(title)s.%(ext)s")

            extra_headers = _yt_dlp_headers_for(target_url) if _is_direct_media(target_url) else _yt_dlp_headers_for(url)
            res = None

            if mode == "audio":
                audio_fast = "bestaudio/best"
                audio_fallback = "bestvideo+bestaudio/best"
                common_args = base_cmd + cookie_args + extra_headers + ["-o", output_template]

                if actual_format == "wav":
                    cmd_fast = common_args + ["-f", audio_fast, "--extract-audio", "--audio-format", "wav", "--postprocessor-args", "ffmpeg:-ar 48000", target_url]
                    cmd_fallback = common_args + ["-f", audio_fallback, "--extract-audio", "--audio-format", "wav", "--postprocessor-args", "ffmpeg:-ar 48000", target_url]
                elif actual_format == "m4a":
                    cmd_fast = common_args + ["-f", audio_fast, "--extract-audio", "--audio-format", "m4a", target_url]
                    cmd_fallback = common_args + ["-f", audio_fallback, "--extract-audio", "--audio-format", "m4a", target_url]
                else:
                    cmd_fast = common_args + ["-f", audio_fast, target_url]
                    cmd_fallback = common_args + ["-f", audio_fallback, target_url]

                try:
                    res = _run_yt_dlp(cmd_fast, timeout_sec=1800)
                    if res.returncode != 0:
                        logger.info(f"[{job_id}] Snabb ljudhämtning misslyckades, testar fallback (A/V + extract)...")
                        res = _run_yt_dlp(cmd_fallback, timeout_sec=1800)
                except subprocess.TimeoutExpired:
                    return None, "Timeout"

            elif mode == "video":
                cmd_video = base_cmd + cookie_args + extra_headers
                is_manifest = ".m3u8" in (target_url or "").lower() or (target_url or "").lower().endswith(".mpd")
                is_raw_mp4 = (target_url or "").lower().endswith(".mp4")

                if actual_format == "mp4":
                    target_format = SVT_SAFE_VIDEO if is_svt else "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/b[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
                    if not is_raw_mp4:
                        cmd_video.extend(["-f", target_format, "--merge-output-format", "mp4"])
                else:
                    if is_manifest or not _is_direct_media(target_url):
                        cmd_video.extend(["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"])

                cmd_video.extend(["-o", output_template, target_url])

                try:
                    res = _run_yt_dlp(cmd_video, timeout_sec=1800)
                except subprocess.TimeoutExpired:
                    return None, "Timeout"

            return res, ""

        result, err_msg = run_ytdlp_attempt(current_url, use_slug_filename=bool(resolved_url))

        if err_msg == "Timeout":
            _update_history_status(job_id, url, "error")
            _set_job(job_id, {"status": "error", "message": "Nätverkstimeout: Klippet tog över 30 minuter att ladda ner."})
            return

        if (result is None or result.returncode != 0 or not _list_finished_files(work_dir)) and not resolved_url:
            logger.info(f"[{job_id}] yt-dlp kraschade/hittade inget. Kastar ut fiskenätet (Playwright) som plan B...")
            
            candidates = _resolve_manifests_via_playwright(job_id, url)
            best_fallback = _pick_best_media_url(candidates)
            
            if best_fallback:
                logger.info(f"[{job_id}] Fiskenätet räddade dagen! Försöker yt-dlp igen med dold ström: {best_fallback}")
                result, err_msg = run_ytdlp_attempt(best_fallback, use_slug_filename=True)
                
                if err_msg == "Timeout":
                    _update_history_status(job_id, url, "error")
                    _set_job(job_id, {"status": "error", "message": "Nätverkstimeout under fiskenäts-hämtning."})
                    return
            else:
                logger.info(f"[{job_id}] Fiskenätet drogs upp tomt.")

        if result is None or result.returncode != 0:
            stderr = "" if result is None else (result.stderr or "")
            stdout = "" if result is None else (result.stdout or "")
            error_msg = _clean_last_stderr_line(stderr)
            classified = _classify_ytdlp_error(stderr, stdout)

            logger.info(f"\n--- YT-DLP FEL START ---\n{stderr}\n--- YT-DLP FEL SLUT ---\n")
            stdout_tail = "\n".join(stdout.splitlines()[-20:])
            if stdout_tail:
                logger.info(f"\n--- YT-DLP STDOUT TAIL ---\n{stdout_tail}\n--- SLUT STDOUT TAIL ---\n")

            _update_history_status(job_id, url, "error")
            if classified:
                _set_job(job_id, {"status": "error", "message": f"{classified} ({error_msg})"})
            else:
                _set_job(job_id, {"status": "error", "message": f"Fel: {error_msg}"})
            return

        
        # --- SMART FORMAT DETECTOR (v1.7.0) ---
        if actual_format == "auto":
            files = _list_finished_files(work_dir)
            if files:
                src = files[0]
                ext = os.path.splitext(src)[1].lower()
                
                if ext in {".mpd", ".m3u8", ".json", ".xml"}:
                    logger.error(f"[{job_id}] Fick bara en text/manifest-fil ({ext}). Avbryter.")
                    os.remove(src)
                    files = [] # Detta tvingar fram ett fel längre ner
                else:
                    pt_friendly = {".wav", ".m4a", ".mp3", ".aiff", ".flac", ".mp4", ".mov"}
                    if mode == "audio" and ext not in pt_friendly:
                        logger.info(f"[{job_id}] Smart Detector: Inkompatibelt format ({ext}). Konverterar till WAV 48kHz...")
                        dst_wav = os.path.splitext(src)[0] + ".wav"
                        cmd = ["ffmpeg", "-y", "-i", src, "-ar", "48000", "-sample_fmt", "s16", dst_wav]
                        try:
                            conv = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
                        except subprocess.TimeoutExpired:
                            raise RuntimeError("Timeout vid ffmpeg-konvertering till WAV.")
                        if conv.returncode != 0 or not os.path.exists(dst_wav):
                            err_tail = _clean_last_stderr_line(conv.stderr or conv.stdout or "")
                            raise RuntimeError(f"ffmpeg-konvertering misslyckades: {err_tail}")
                        os.remove(src)
        # --------------------------------------
        final_base = bg_title or source_slug or "media"
        final_filename = _publish_final_output(work_dir, final_base)
        logger.info(f"[{job_id}] HÄMTNING KLAR! Filen sparades som: {final_filename}")
        _update_history_status(job_id, url, "success", final_filename)
        _set_job(job_id, {"status": "success", "message": "Hämtning klar!", "filename": final_filename})

    except Exception as e:
        err_str = str(e)
        logger.info(f"\n--- SYSTEMFEL ---\n{err_str}\n-----------------\n")
        _update_history_status(job_id, url, "error")
        if "Inga färdiga filer" in err_str:
            _set_job(job_id, {"status": "error", "message": "Vi kunde inte hitta någon giltig mediaström. Spelaren är troligen låst eller okänd. Kontakta systemadministratören om källan är viktig."})
        else:
            _set_job(job_id, {"status": "error", "message": "Ett oväntat fel uppstod vid nedladdningen. Kontakta systemadministratören."})
            
    finally:
        if acquired_slot:
            try:
                DOWNLOAD_SEMAPHORE.release()
            except Exception:
                pass
        try:
            if os.path.isdir(work_dir):
                shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass

@app.post("/download")
async def start_download(
    background_tasks: BackgroundTasks,
    url: str = Form(...),
    mode: str = Form("audio"),
    out_format: str = Form("m4a"),
):
    url = (url or "").strip()

    if "google.com/search" in url and "vid:" in url:
        vid_match = re.search(r'vid:([a-zA-Z0-9_-]{11})', url)
        if vid_match:
            logger.info(f"Fångade en Google Search-länk! Konverterar automatiskt till YouTube-länk (ID: {vid_match.group(1)}).")
            url = f"https://www.youtube.com/watch?v={vid_match.group(1)}"

    if not (url.startswith("http://") or url.startswith("https://")):
        return {"status": "error", "message": "Ogiltig länk (måste börja med http/https)."}

    if mode not in ALLOWED_MODES or out_format not in ALLOWED_FORMATS:
        return {"status": "error", "message": "Ogiltigt läge eller format."}

    actual_format = out_format

    job_id = str(uuid.uuid4())
    source_slug = _source_slug_from_url(url)

    _set_job(job_id, {"status": "processing"})
    
    _add_to_history(job_id, url, mode, actual_format)
    
    background_tasks.add_task(process_download, job_id, url, mode, actual_format, source_slug)

    return {"status": "started", "job_id": job_id}

@app.get("/status/{job_id}")
async def get_status(job_id: str):
    _prune_jobs()
    with JOB_LOCK:
        return JOBS.get(job_id, {"status": "not_found", "message": "Jobbet hittades inte."})

@app.get("/debug/resolve")
async def debug_resolve(url: str):
    if not url:
        return {"ok": False, "error": "url saknas"}
    if "aftonbladet.se" not in url.lower():
        return {"ok": False, "error": "debug/resolve är främst tänkt för Aftonbladet-länkar för tillfället"}

    try:
        html = _fetch_html(url)
        clean_html = html.replace("\\/", "/")

        json_match = re.search(r'"(?:videoId|liveId|videoAssetId)"\s*:\s*"([a-zA-Z0-9_-]{5,})"', clean_html)
        iframe_match = re.search(r'tv\.aftonbladet\.se/iframe/(?:video|live)/([a-zA-Z0-9_-]+)', clean_html)

        candidates = _collect_ab_candidates_from_html(clean_html)
        scored = []
        for c in candidates:
            scored.append({
                "url": c,
                "bad_hint": _ab_is_bad_candidate_url(c),
                "score": list(_score_stream_url(c)),
            })

        picked = _pick_best_media_url(candidates)

        return {
            "ok": True,
            "app_version": APP_VERSION,
            "input_url": url,
            "video_id": json_match.group(1) if json_match else None,
            "iframe_id": iframe_match.group(1) if iframe_match else None,
            "picked": picked,
            "candidates_count": len(candidates),
            "candidates": scored[:100],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
