# ===================================================
# LJUDBUSTER - Redaktionsverktyg
# Version: 1.8.17-dev av Johan Hörnqvist
# Uppdaterad: 2026-04-28
#
# Changelog:
# - SVT Title NEAR Resolver (1.8.17-dev): Matchar exakt livepost-rubrik mot närmaste play-knapp och sniffar media efter klick.
# - SVT Final Playwright Override (1.8.16-dev): Sist-i-filen resolver som rankar rätt SVT-playknapp, läser scoped video[src], rensar consent-overlay och sniffar media efter klick.
# - SVT Consent Clickfix (1.8.15-dev): Rensar SVT consent-overlay före rankad play-klick och faller tillbaka till JS-click.
# - SVT Ranked Aria Sniper (1.8.14-dev): Fixar falskt HTML-window, rankar synliga play-knappar och sniffar media efter vald knapp.
# - SVT Aria Scoped Sniper (1.8.13-dev): Matchar ?inlagg mot HTML-window/rubrik, klickar rätt synlig play-knapp och sniffar bara media efter klick.
# - SVT Chromium Scoped Sniper (1.8.13-dev): Klickar exakt SVT-livepost i Chromium och sniffar bara scoped Ditto/Switcher/HLS från rätt inlägg.
# - SVT Scoped HTML-window Resolver (1.8.12-dev): Läser manifest/video endast ur textfönster runt exakt ?inlagg-id och resolverar SVT switcher JSON före yt-dlp.
# - SVT Switcher JSON Resolver (1.8.11-dev): Resolverar switcher.cdn.svt.se JSON till riktig svt-vod .m3u8 innan yt-dlp.
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

APP_VERSION = "1.8.17-dev"

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

def _resolve_manifests_via_playwright_legacy_1812(job_id: str, article_url: str) -> List[str]:
    """Playwright/sniper fallback.

    Viktigt för SVT liveposts:
    - Om URL har ?inlagg=<id> får vi aldrig globalt plocka första bästa video.
    - Vi accepterar bara media som kan kopplas till exakt post-id:
      1) video[src]/manifestUrl inne i exakt DOM-post
      2) response/body/html-window där samma post-id finns nära manifestet
      3) nätverkstrafik efter klick på play-knappen i exakt post
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    except Exception as e:
        logger.info(f"[{job_id}] Resolver: playwright import fail: {e}")
        return []

    from urllib.parse import urlparse as _urlparse, parse_qs as _parse_qs, unquote as _unquote
    import html as _html

    parsed_article = _urlparse(article_url)
    query = _parse_qs(parsed_article.query)
    inlagg_id = query.get("inlagg", [None])[0]
    is_svt_page = "svt.se" in (article_url or "").lower() or "svtplay.se" in (article_url or "").lower()

    found: List[str] = []
    found_scoped: List[str] = []
    click_armed = {"value": False}

    def _dedupe(items: List[str]) -> List[str]:
        seen = set()
        out: List[str] = []
        for item in items:
            item = str(item or "").strip()
            if not item or item in seen:
                continue
            seen.add(item)
            out.append(item)
        return out

    def _decode_blob(blob: str) -> str:
        s = str(blob or "")
        for _ in range(3):
            s = _html.unescape(s)
            s = (
                s.replace("\\/", "/")
                 .replace("\\u0026", "&")
                 .replace("\\u003d", "=")
                 .replace("\\u003D", "=")
                 .replace("\\u003c", "<")
                 .replace("\\u003C", "<")
                 .replace("\\u003e", ">")
                 .replace("\\u003E", ">")
                 .replace("\\u0022", '"')
                 .replace("\\u0027", "'")
                 .replace("&amp;", "&")
            )
        return s

    def _is_svt_media_candidate(url: str) -> bool:
        u = str(url or "").lower()
        if not u or u.startswith("blob:"):
            return False
        return (
            ".m3u8" in u
            or "api.svt.se/ditto/api/v3/manifest" in u
            or "switcher.cdn.svt.se/resolve/" in u
            or "svt-vod-" in u
            or ".cdn.svt.se/" in u
            or "ed16.cdn.svt.se/" in u
        )

    def _normalize_candidate(url: str) -> List[str]:
        url = _decode_blob(url).strip().strip('"').strip("'")
        if not url or url.startswith("blob:"):
            return []

        out: List[str] = []

        # SVT Ditto wrapper:
        # https://api.svt.se/ditto/api/v3/manifest?manifestUrl=<encoded real hls>&platform=...
        if "api.svt.se/ditto/api/v3/manifest" in url.lower():
            try:
                qs = _parse_qs(_urlparse(url).query)
                for wrapped in qs.get("manifestUrl", []):
                    out.extend(_normalize_candidate(_unquote(wrapped)))
            except Exception:
                pass
            return out

        # Rå manifestUrl=... som kan ligga i HTML/Firestore/body.
        if "manifestUrl=" in url:
            try:
                qs = _parse_qs(_urlparse(url).query)
                for wrapped in qs.get("manifestUrl", []):
                    out.extend(_normalize_candidate(_unquote(wrapped)))
            except Exception:
                pass

        # SVT switcher svarar ibland JSON trots .m3u8-suffix.
        if "switcher.cdn.svt.se/resolve/" in url.lower():
            try:
                if "_resolve_svt_switcher_if_needed" in globals():
                    resolved = _resolve_svt_switcher_if_needed(job_id, url)
                    if resolved and resolved != url:
                        out.extend(_normalize_candidate(resolved))
                        return _dedupe(out)
            except Exception as e:
                logger.info(f"[{job_id}] SVT Chromium sniper: switcher normalize fail: {e}")

        if _is_svt_media_candidate(url):
            out.append(url)

        return _dedupe(out)

    def _extract_candidates_from_text(blob: str) -> List[str]:
        s = _decode_blob(blob)
        out: List[str] = []

        patterns = [
            r'https?://api\.svt\.se/ditto/api/v3/manifest\?[^"\'<>\s\\]+',
            r'https?://switcher\.cdn\.svt\.se/resolve/[^"\'<>\s\\]+',
            r'https?://[^"\'<>\s\\]+\.m3u8[^"\'<>\s\\]*',
            r'manifestUrl=([^"\'<>\s\\]+)',
            r'"manifestUrl"\s*:\s*"([^"]+)"',
            r"'manifestUrl'\s*:\s*'([^']+)'",
            r'src\s*=\s*"([^"]+\.m3u8[^"]*)"',
            r"src\s*=\s*'([^']+\.m3u8[^']*)'",
        ]

        for pat in patterns:
            for m in re.finditer(pat, s, flags=re.I):
                raw = m.group(1) if m.lastindex else m.group(0)
                out.extend(_normalize_candidate(raw))

        return _dedupe(out)

    def _extract_scoped_window(blob: str, marker: str, before: int = 12000, after: int = 24000) -> str:
        s = _decode_blob(blob)
        if not marker or marker not in s:
            return ""
        idx = s.find(marker)
        start = max(0, idx - before)
        end = min(len(s), idx + after)
        return s[start:end]

    def _add_global_candidate(url: str, source: str) -> None:
        for u in _normalize_candidate(url):
            if u not in found:
                found.append(u)
                logger.info(f"[{job_id}] Snipern fångade SVT-kandidat ({source}): {u}")

    def _add_scoped_candidate(url: str, source: str) -> None:
        for u in _normalize_candidate(url):
            if u not in found_scoped:
                found_scoped.append(u)
                logger.info(f"[{job_id}] SVT scoped sniper fångade kandidat ({source}): {u}")

    def _inspect_scoped_dom(page, label: str) -> List[str]:
        if not inlagg_id:
            return []

        try:
            data = page.evaluate(
                """
                (id) => {
                  const post =
                    document.getElementById(id) ||
                    Array.from(document.querySelectorAll('[id]')).find(el => el.id === id);

                  const html = post ? post.outerHTML : "";
                  const docHtml = document.documentElement ? document.documentElement.innerHTML : "";

                  return {
                    hasPost: !!post,
                    docHasId: docHtml.includes(id),
                    postHtml: html,
                    postText: post ? (post.innerText || "") : "",
                    videoSrcs: post ? Array.from(post.querySelectorAll("video")).map(v => v.currentSrc || v.src || v.getAttribute("src") || "").filter(Boolean) : [],
                    buttons: post ? Array.from(post.querySelectorAll("button")).map(b => ({
                      text: b.innerText || "",
                      aria: b.getAttribute("aria-label") || "",
                      testid: b.getAttribute("data-testid") || "",
                      rt: b.getAttribute("data-rt") || ""
                    })).slice(0, 20) : []
                  };
                }
                """,
                inlagg_id,
            )
        except Exception as e:
            logger.info(f"[{job_id}] SVT scoped DOM ({label}) evaluate fail: {e}")
            return []

        has_post = bool(data.get("hasPost"))
        doc_has_id = bool(data.get("docHasId"))
        video_srcs = data.get("videoSrcs") or []
        buttons = data.get("buttons") or []
        post_html = data.get("postHtml") or ""

        logger.info(
            f"[{job_id}] SVT scoped DOM ({label}): "
            f"post={has_post} docHasId={doc_has_id} videos={len(video_srcs)} buttons={len(buttons)}"
        )

        out: List[str] = []
        for src in video_srcs:
            out.extend(_normalize_candidate(src))
        out.extend(_extract_candidates_from_text(post_html))

        for u in _dedupe(out):
            _add_scoped_candidate(u, f"dom:{label}")

        return _dedupe(out)

    def _click_exact_post(page) -> str:
        if not inlagg_id:
            return "no-inlagg-id"

        try:
            return page.evaluate(
                """
                async (id) => {
                  const sleep = (ms) => new Promise(r => setTimeout(r, ms));

                  const findPost = () =>
                    document.getElementById(id) ||
                    Array.from(document.querySelectorAll('[id]')).find(el => el.id === id);

                  // Försök få SVT att rendera/expandera liveflödet.
                  for (let round = 0; round < 8; round++) {
                    const post = findPost();
                    if (post) break;

                    window.scrollTo(0, Math.floor(document.body.scrollHeight * (round / 8)));

                    const expanders = Array.from(document.querySelectorAll('button')).filter(b => {
                      const t = (b.innerText || "").toLowerCase();
                      const a = (b.getAttribute("aria-label") || "").toLowerCase();
                      return (
                        t.includes("fler händelser") ||
                        t.includes("visa inlägg") ||
                        a.includes("visa inlägget") ||
                        a.includes("visa inlägg")
                      );
                    });

                    for (const b of expanders.slice(0, 8)) {
                      try {
                        b.scrollIntoView({block: "center", inline: "center"});
                        b.click();
                        await sleep(250);
                      } catch (e) {}
                    }

                    await sleep(500);
                  }

                  const post = findPost();
                  if (!post) return "no-post";

                  post.scrollIntoView({block: "center", inline: "center"});
                  await sleep(300);

                  const selectors = [
                    'button[data-testid="play-pause-button"]',
                    'button[data-rt="video-player-splash-play"]',
                    '[data-rt="video-player-splash-play"]',
                    'button[aria-label*="Spela"]',
                    'video'
                  ];

                  for (const sel of selectors) {
                    const el = post.querySelector(sel);
                    if (el) {
                      try {
                        el.scrollIntoView({block: "center", inline: "center"});
                        await sleep(200);
                        el.click();
                        return "clicked:" + sel;
                      } catch (e) {
                        return "click-error:" + sel + ":" + String(e);
                      }
                    }
                  }

                  return "no-play-control";
                }
                """,
                inlagg_id,
            )
        except Exception as e:
            return f"evaluate-error:{e}"

    def _try_scoped_html_body(page, label: str) -> List[str]:
        if not inlagg_id:
            return []

        out: List[str] = []

        try:
            content = page.content()
            window = _extract_scoped_window(content, inlagg_id)
            if window:
                html_hits = _extract_candidates_from_text(window)
                logger.info(f"[{job_id}] SVT scoped HTML-window ({label}): hits={len(html_hits)}")
                for u in html_hits:
                    _add_scoped_candidate(u, f"html-window:{label}")
                out.extend(html_hits)
            else:
                logger.info(f"[{job_id}] SVT scoped HTML-window ({label}): inget id-window")
        except Exception as e:
            logger.info(f"[{job_id}] SVT scoped HTML-window ({label}) fail: {e}")

        return _dedupe(out)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--autoplay-policy=no-user-gesture-required",
                ],
            )

            context = browser.new_context(
                viewport={"width": 1440, "height": 1400},
                locale="sv-SE",
                timezone_id="Europe/Stockholm",
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/142.0.0.0 Safari/537.36"
                ),
                extra_http_headers={
                    "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
                },
            )

            page = context.new_page()

            def on_request(req):
                u = req.url
                if not _is_svt_media_candidate(u):
                    return
                if is_svt_page and inlagg_id:
                    if click_armed["value"]:
                        _add_scoped_candidate(u, "request-after-click")
                    else:
                        _add_global_candidate(u, "request-before-scope")
                else:
                    _add_global_candidate(u, "request")

            def on_response(resp):
                u = resp.url

                if _is_svt_media_candidate(u):
                    if is_svt_page and inlagg_id:
                        if click_armed["value"]:
                            _add_scoped_candidate(u, "response-after-click")
                        else:
                            _add_global_candidate(u, "response-before-scope")
                    else:
                        _add_global_candidate(u, "response")

                # Viktigt: Firestore/live/API-responsen kan innehålla själva post-blocket
                # även när document.getElementById(id) ännu inte finns.
                if is_svt_page and inlagg_id:
                    lu = u.lower()
                    interesting_body = (
                        "firestore.googleapis.com" in lu
                        or "direktcenter" in lu
                        or "svt.se" in lu
                        or "api.svt.se" in lu
                    )

                    if interesting_body:
                        try:
                            body = resp.text(timeout=3000)
                        except TypeError:
                            try:
                                body = resp.text()
                            except Exception:
                                body = ""
                        except Exception:
                            body = ""

                        if body and inlagg_id in _decode_blob(body):
                            scoped = _extract_scoped_window(body, inlagg_id)
                            hits = _extract_candidates_from_text(scoped)
                            logger.info(
                                f"[{job_id}] SVT scoped response-body: "
                                f"url={u[:120]} hits={len(hits)}"
                            )
                            for hit in hits:
                                _add_scoped_candidate(hit, "response-body-near-id")

            page.on("request", on_request)
            page.on("response", on_response)

            logger.info(f"[{job_id}] Playwright-fiskenät aktiverat! Skrapar {article_url}...")

            page.goto(article_url, wait_until="domcontentloaded", timeout=30000)

            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

            if is_svt_page and inlagg_id:
                logger.info(f"[{job_id}] SVT Chromium scoped sniper: target inlägg={inlagg_id}")

                # Först: se om Firestore/API redan gav oss exakt post.
                for _ in range(10):
                    if found_scoped:
                        break
                    _try_scoped_html_body(page, "before-click")
                    _inspect_scoped_dom(page, "before-click")
                    if found_scoped:
                        break
                    page.wait_for_timeout(1000)

                if not found_scoped:
                    click_result = _click_exact_post(page)
                    logger.info(f"[{job_id}] SVT Chromium scoped sniper: klickresultat={click_result}")

                    click_armed["value"] = True

                    # Direkt efter klick: DOM och nätverk får några sekunder på sig.
                    for i in range(16):
                        if found_scoped:
                            break
                        _inspect_scoped_dom(page, f"after-click-{i+1}")
                        _try_scoped_html_body(page, f"after-click-{i+1}")
                        if found_scoped:
                            break
                        page.wait_for_timeout(750)

                browser.close()

                scoped_final = _dedupe(found_scoped)
                if scoped_final:
                    logger.info(f"[{job_id}] SVT Chromium scoped sniper: returnerar {len(scoped_final)} scoped kandidat(er), första={scoped_final[0]}")
                    return scoped_final

                logger.info(f"[{job_id}] SVT Chromium scoped sniper: ingen video kunde kopplas till exakt SVT-inlägg. Global fallback blockerad.")
                return []

            # Generic fallback för vanliga artiklar / icke-SVT / SVT utan ?inlagg=.
            page.wait_for_timeout(3000)

            try:
                # Klicka första synliga play-knapp som generisk fallback.
                page.evaluate(
                    """
                    async () => {
                      const sleep = (ms) => new Promise(r => setTimeout(r, ms));
                      const buttons = Array.from(document.querySelectorAll(
                        'button[data-testid="play-pause-button"], button[data-rt="video-player-splash-play"], button[aria-label*="Spela"]'
                      ));
                      for (const b of buttons.slice(0, 3)) {
                        try {
                          b.scrollIntoView({block: "center", inline: "center"});
                          await sleep(200);
                          b.click();
                          await sleep(1000);
                          return true;
                        } catch (e) {}
                      }
                      return false;
                    }
                    """
                )
            except Exception:
                pass

            page.wait_for_timeout(8000)
            browser.close()

            final = _dedupe(found)
            if final:
                logger.info(f"[{job_id}] Fiskenätet fångade ström efter klick! Första={final[0]}")
            else:
                logger.info(f"[{job_id}] Fiskenätet drogs upp tomt.")

            return final

    except Exception as e:
        logger.info(f"[{job_id}] Playwright resolver fel: {e}")
        return []

def _is_direct_media(url: str) -> bool:
    ul = (url or "").lower()
    return (".m3u8" in ul) or ul.endswith(".mpd") or ul.endswith(".mp4") or ul.endswith(".m4a") or ul.endswith(".mp3")


def _extract_svt_scoped_media_urls_from_blob(job_id: str, blob: str, inlagg_id: str) -> List[str]:
    """Extraherar SVT-media endast nära exakt livepost-id.

    Detta är avsiktligt smalare än globalt fiskenät:
    - URL måste ha ?inlagg=<id>
    - vi letar bara i HTML/text nära just det id:t
    - vi unwrap:ar api.svt.se/ditto manifestUrl
    - vi resolverar switcher.cdn.svt.se JSON om helpern finns
    """
    if not blob or not inlagg_id:
        return []

    raw = html_parser.unescape(str(blob)).replace("\\/", "/")
    low = raw.lower()
    needle = inlagg_id.lower()

    if needle not in low:
        return []

    chunks: List[str] = []
    pos = 0

    while True:
        idx = low.find(needle, pos)
        if idx < 0:
            break

        # Försök börja vid närmaste <div före id:t.
        div_start = low.rfind("<div", 0, idx)
        if div_start >= 0 and (idx - div_start) < 3000:
            start = div_start
        else:
            start = max(0, idx - 5000)

        max_end = min(len(raw), idx + 220000)

        # Sluta helst innan nästa post-id, för att inte råka ta grannvideo.
        next_post = re.search(
            r'<div\s+id=["\'][0-9a-f]{24,40}["\']',
            low[idx + len(needle):max_end],
            flags=re.I,
        )

        if next_post:
            end = idx + len(needle) + next_post.start()
        else:
            end = max_end
            for marker in ("</li>", "<li><button", "</article>"):
                marker_pos = low.find(marker, idx + len(needle), max_end)
                if marker_pos != -1:
                    end = min(end, marker_pos)

        chunk = raw[start:end]
        if chunk:
            chunks.append(chunk)

        pos = idx + len(needle)

    candidates: List[str] = []

    def push(value: str) -> None:
        u = html_parser.unescape(str(value or "")).replace("\\/", "/").strip()
        if not u or u.startswith("blob:"):
            return

        if u.startswith("//"):
            u = "https:" + u

        if "api.svt.se/ditto/api/v3/manifest" in u.lower():
            try:
                qs = parse_qs(urlparse(u).query)
                for wrapped in qs.get("manifestUrl", []):
                    push(unquote(wrapped))
            except Exception:
                pass
            return

        if not (u.startswith("http://") or u.startswith("https://")):
            return

        ul = u.lower()
        if (
            ".m3u8" in ul
            or ".mpd" in ul
            or "switcher.cdn.svt.se/resolve/" in ul
            or "svt-vod-" in ul
            or ".cdn.svt.se/" in ul
        ):
            candidates.append(u)

    for chunk in chunks:
        # 1) video[src] inne i rätt block.
        for tag in re.findall(r"<video\b[^>]*>", chunk, flags=re.I | re.S):
            for m in re.finditer(r'\bsrc=["\']([^"\']+)["\']', tag, flags=re.I):
                push(m.group(1))

        # 2) Full api.svt.se/ditto wrapper.
        for m in re.findall(
            r'https?://api\.svt\.se/ditto/api/v3/manifest\?[^"\'<>\s]+',
            chunk,
            flags=re.I,
        ):
            push(m)

        # 3) Rå manifestUrl=...
        for m in re.finditer(r'manifestUrl=([^&"\'<>\s]+)', chunk, flags=re.I):
            push(unquote(m.group(1)))

        # 4) JSON-liknande manifestUrl.
        for m in re.finditer(r'"manifestUrl"\s*:\s*"([^"]+)"', chunk, flags=re.I):
            push(m.group(1))

        # 5) Direkta SVT/Switcher HLS-länkar.
        for m in re.findall(r'https?://[^"\'<>\s]+\.m3u8(?:\?[^"\'<>\s]*)?', chunk, flags=re.I):
            push(m)

    # Dedupe + resolvera switcher-JSON.
    seen = set()
    resolved: List[str] = []

    for u in candidates:
        try:
            if "switcher.cdn.svt.se/resolve/" in u.lower() and "_resolve_svt_switcher_if_needed" in globals():
                u = _resolve_svt_switcher_if_needed(job_id, u)
        except Exception:
            pass

        if not u or u in seen:
            continue
        seen.add(u)
        resolved.append(u)

    if resolved:
        logger.info(
            f"[{job_id}] SVT scoped HTML-window: id={inlagg_id} chunks={len(chunks)} kandidater={len(resolved)} första={resolved[0]}"
        )
    else:
        logger.info(
            f"[{job_id}] SVT scoped HTML-window: id={inlagg_id} chunks={len(chunks)} men inga mediakandidater"
        )

    return resolved



# === LjudBuster 1.8.13-dev: SVT aria-correlated scoped sniper ===

def _lb_svt_norm_text(value: str) -> str:
    value = html_parser.unescape(str(value or ""))
    value = re.sub(r"<[^>]+>", " ", value)
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _lb_svt_looks_like_media_url(url: str) -> bool:
    lower = str(url or "").lower()

    if not lower.startswith("http"):
        return False

    if "__manifest" in lower:
        return False

    if "/assets/manifest-" in lower:
        return False

    if "firestore.googleapis.com" in lower:
        return False

    return (
        "api.svt.se/ditto/api/v3/manifest" in lower
        or "switcher.cdn.svt.se/resolve/" in lower
        or ".m3u8" in lower
        or ".mpd" in lower
        or "svt-vod" in lower
        or re.search(r"https://ed[0-9]+\.cdn\.svt\.se/", lower) is not None
    )


def _lb_svt_push_candidate(out: List[str], url: str) -> None:
    url = html_parser.unescape(str(url or "")).replace("\\/", "/").strip()
    url = url.rstrip(".,);")

    if not url:
        return

    # SVT Ditto wrapper: unwrap manifestUrl.
    if "api.svt.se/ditto/api/v3/manifest" in url.lower():
        try:
            qs = parse_qs(urlparse(url).query)
            for wrapped in qs.get("manifestUrl", []):
                if wrapped:
                    _lb_svt_push_candidate(out, unquote(wrapped))
            return
        except Exception:
            pass

    # Prefer HLS sibling for SVT switcher DASH URLs when possible.
    if "switcher.cdn.svt.se/resolve/" in url.lower():
        hls = (
            url.replace("/dash-full.mpd", "/hls-cmaf-full.m3u8")
               .replace("/dash-cmaf.mpd", "/hls-cmaf-avc.m3u8")
               .replace("/dash.mpd", "/hls-cmaf-full.m3u8")
        )
        if hls != url and hls not in out:
            out.append(hls)

    # Prefer HLS sibling for already-resolved SVT CDN DASH URLs when possible.
    if re.search(r"https://ed[0-9]+\.cdn\.svt\.se/", url.lower()) or "svt-vod" in url.lower():
        hls = (
            url.replace("/dash-full.mpd", "/hls-cmaf-full.m3u8")
               .replace("/dash-cmaf.mpd", "/hls-cmaf-avc.m3u8")
               .replace("/dash.mpd", "/hls-cmaf-full.m3u8")
        )
        if hls != url and hls not in out:
            out.append(hls)

    if _lb_svt_looks_like_media_url(url) and url not in out:
        out.append(url)


def _lb_svt_extract_media_candidates_from_blob(blob: str) -> List[str]:
    s = html_parser.unescape(str(blob or "")).replace("\\/", "/")
    out: List[str] = []

    # Full URLs.
    for m in re.finditer(r'https?://[^\s"\'<>]+', s, flags=re.I):
        _lb_svt_push_candidate(out, m.group(0))

    # Raw manifestUrl=... fragments.
    for m in re.finditer(r'manifestUrl=([^&"\'<>\s]+)', s, flags=re.I):
        try:
            _lb_svt_push_candidate(out, unquote(m.group(1)))
        except Exception:
            pass

    # Deduplicate while preserving order.
    deduped: List[str] = []
    for item in out:
        if item and item not in deduped:
            deduped.append(item)

    return deduped


def _lb_svt_extract_match_terms_from_window(html_window: str) -> List[str]:
    s = html_parser.unescape(str(html_window or ""))
    terms: List[str] = []

    # Exact aria labels near the selected inlägg are best.
    for m in re.finditer(r'aria-label=["\']([^"\']*Spela[^"\']+)["\']', s, flags=re.I):
        label = _lb_svt_norm_text(m.group(1))
        if label and label not in terms:
            terms.append(label)

        # Also add title part after dash.
        parts = re.split(r"\s+[—-]\s+", label, maxsplit=1)
        if len(parts) == 2:
            title_part = re.sub(r",\s*[0-9]+\s*(sek|min).*$", "", parts[1], flags=re.I).strip()
            if title_part and title_part not in terms:
                terms.append(title_part)

    # Heading text near the selected inlägg.
    for m in re.finditer(r'<h[1-6][^>]*>(.*?)</h[1-6]>', s, flags=re.I | re.S):
        heading = _lb_svt_norm_text(m.group(1))
        if heading and heading not in terms:
            terms.append(heading)

    # SVT often wraps post heading in span.
    for m in re.finditer(r'<span[^>]*>([^<]{8,160})</span>', s, flags=re.I | re.S):
        span_text = _lb_svt_norm_text(m.group(1))
        if span_text and any(word in span_text.lower() for word in ["trump", "intervju", "rapport", "rasar"]):
            if span_text not in terms:
                terms.append(span_text)

    return terms[:8]


def _resolve_svt_livepost_via_aria_sniper_1813(job_id: str, article_url: str, inlagg_id: str) -> List[str]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        logger.info(f"[{job_id}] SVT aria-sniper: playwright import fail: {e}")
        return []

    media_urls: List[str] = []
    clicked_media_urls: List[str] = []
    state = {"after_click": False}

    def _capture_url(url: str, source: str) -> None:
        if not state.get("after_click"):
            return
        if not _lb_svt_looks_like_media_url(url):
            return
        if url not in clicked_media_urls:
            clicked_media_urls.append(url)
            logger.info(f"[{job_id}] SVT aria-sniper: fångade media efter klick ({source}): {url}")

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--autoplay-policy=no-user-gesture-required",
                ],
            )

            context = browser.new_context(
                viewport={"width": 1365, "height": 2200},
                user_agent=_AB_UA,
                locale="sv-SE",
            )

            page = context.new_page()

            page.on("request", lambda req: _capture_url(req.url, "request"))
            page.on("response", lambda resp: _capture_url(resp.url, "response"))

            logger.info(f"[{job_id}] SVT aria-sniper: öppnar livepost-sida")
            page.goto(article_url, wait_until="domcontentloaded", timeout=30000)

            # Give React/Firestore time to populate.
            try:
                page.wait_for_timeout(7000)
            except Exception:
                pass

            html = page.content()
            idx = html.find(inlagg_id)

            if idx < 0:
                logger.info(f"[{job_id}] SVT aria-sniper: inläggs-id finns inte i page.content")
                context.close()
                browser.close()
                return []

            start = max(0, idx - 5000)
            end = min(len(html), idx + 25000)
            html_window = html[start:end]

            logger.info(
                f"[{job_id}] SVT aria-sniper: html-window runt inlägg id hittad "
                f"(start={start}, end={end}, bytes={len(html_window)})"
            )

            # First: direct media URL inside the inlägg window. This is the cleanest path.
            scoped_candidates = _lb_svt_extract_media_candidates_from_blob(html_window)
            scoped_candidates = [u for u in scoped_candidates if _lb_svt_looks_like_media_url(u)]

            if scoped_candidates:
                logger.info(
                    f"[{job_id}] SVT aria-sniper: hittade scoped media direkt i HTML-window: "
                    f"{scoped_candidates[0]}"
                )
                context.close()
                browser.close()
                return scoped_candidates

            # Second: extract heading/aria text from the inlägg window and click matching visible play button.
            match_terms = _lb_svt_extract_match_terms_from_window(html_window)
            logger.info(f"[{job_id}] SVT aria-sniper: match-termer={match_terms}")

            if not match_terms:
                logger.info(f"[{job_id}] SVT aria-sniper: inga match-termer hittades i HTML-window")
                context.close()
                browser.close()
                return []

            clicked_label = page.evaluate(
                """(terms) => {
                    const norm = (s) => String(s || '')
                        .replace(/\\s+/g, ' ')
                        .replace(/[“”]/g, '"')
                        .trim()
                        .toLowerCase();

                    const buttons = Array.from(document.querySelectorAll('button[aria-label], button[data-testid="play-pause-button"]'));

                    for (const rawNeedle of terms) {
                        const needle = norm(rawNeedle);
                        if (!needle) continue;

                        for (const btn of buttons) {
                            const label = btn.getAttribute('aria-label') || btn.innerText || '';
                            const hay = norm(label);

                            if (!hay) continue;

                            if (hay.includes(needle) || needle.includes(hay)) {
                                btn.scrollIntoView({block: 'center', inline: 'center'});
                                btn.click();
                                return label;
                            }
                        }
                    }

                    return '';
                }""",
                match_terms,
            )

            if not clicked_label:
                logger.info(f"[{job_id}] SVT aria-sniper: hittade ingen synlig knapp som matchade HTML-window")
                context.close()
                browser.close()
                return []

            logger.info(f"[{job_id}] SVT aria-sniper: klickade matchad play-knapp: {clicked_label}")

            state["after_click"] = True

            # Wait for video player to request manifest/media after click.
            for _ in range(24):
                try:
                    page.wait_for_timeout(500)
                except Exception:
                    break

            # Also inspect current video[src] after click.
            try:
                video_srcs = page.evaluate(
                    """() => Array.from(document.querySelectorAll('video[src]')).map(v => v.getAttribute('src')).filter(Boolean)"""
                )
                for src in video_srcs or []:
                    _lb_svt_push_candidate(clicked_media_urls, src)
            except Exception as e:
                logger.info(f"[{job_id}] SVT aria-sniper: kunde inte läsa video[src] efter klick: {e}")

            context.close()
            browser.close()

    except Exception as e:
        logger.info(f"[{job_id}] SVT aria-sniper: misslyckades: {e}")
        return []

    for item in clicked_media_urls:
        _lb_svt_push_candidate(media_urls, item)

    media_urls = [u for u in media_urls if _lb_svt_looks_like_media_url(u)]

    if media_urls:
        logger.info(f"[{job_id}] SVT aria-sniper: slutkandidat={media_urls[0]} totalt={len(media_urls)}")
    else:
        logger.info(f"[{job_id}] SVT aria-sniper: ingen media fångad efter klick")

    return media_urls


def _resolve_manifests_via_playwright(job_id: str, article_url: str) -> List[str]:
    parsed_article = urlparse(article_url)
    query = parse_qs(parsed_article.query)
    inlagg_id = query.get("inlagg", [None])[0]
    is_svt_livepost = bool(inlagg_id) and "svt.se" in (article_url or "").lower()

    if is_svt_livepost:
        logger.info(f"[{job_id}] SVT aria-sniper: använder scoped livepost-väg för inlägg {inlagg_id}")
        urls = _resolve_svt_livepost_via_aria_sniper_1813(job_id, article_url, inlagg_id)
        if urls:
            return urls

        logger.info(
            f"[{job_id}] SVT aria-sniper: ingen video kunde kopplas till exakt inlägg. "
            "Avbryter utan global fallback för att undvika fel video."
        )
        return []

    return _resolve_manifests_via_playwright_legacy_1812(job_id, article_url)


def _resolve_svt_switcher_if_needed(job_id: str, media_url: str) -> str:
    """Resolverar SVT switcher.cdn.svt.se/resolve-URL:er innan yt-dlp.

    SVT kan ge en URL som slutar på .m3u8 men ändå svarar med application/json.
    Då måste vi läsa JSON-svaret och skicka den riktiga svt-vod-*.m3u8 vidare till yt-dlp.
    """
    url = str(media_url or "").strip()
    lower = url.lower()

    if "switcher.cdn.svt.se/resolve/" not in lower:
        return url

    try:
        logger.info(f"[{job_id}] SVT Switcher: resolverar JSON-wrapper: {url}")

        try:
            from curl_cffi import requests
            resp = requests.get(
                url,
                impersonate="chrome",
                headers={
                    "Accept": "*/*",
                    "Origin": "https://www.svt.se",
                    "Referer": "https://www.svt.se/",
                    "User-Agent": _AB_UA,
                },
                timeout=10,
            )
            status = getattr(resp, "status_code", 0)
            ctype = resp.headers.get("content-type", "") if hasattr(resp, "headers") else ""
            body = resp.text or ""
        except Exception:
            req = urllib.request.Request(url, headers={
                "Accept": "*/*",
                "Origin": "https://www.svt.se",
                "Referer": "https://www.svt.se/",
                "User-Agent": _AB_UA,
            })
            with urllib.request.urlopen(req, timeout=10) as r:
                status = getattr(r, "status", 0)
                ctype = r.headers.get("content-type", "")
                body = r.read(2_000_000).decode("utf-8", errors="ignore")

        logger.info(f"[{job_id}] SVT Switcher: status={status} content-type={ctype} bytes={len(body)}")

        candidates: List[str] = []

        def push(candidate: str) -> None:
            candidate = html_parser.unescape(str(candidate or "")).replace("\\/", "/").strip()
            if not candidate or candidate.startswith("blob:"):
                return

            if "api.svt.se/ditto/api/v3/manifest" in candidate:
                try:
                    qs = parse_qs(urlparse(candidate).query)
                    for real_u in qs.get("manifestUrl", []):
                        push(unquote(real_u))
                except Exception:
                    pass
                return

            if any(x in candidate.lower() for x in [".m3u8", ".mpd", ".mp4", ".m4a", ".mp3"]):
                if candidate not in candidates:
                    candidates.append(candidate)

        def walk(obj):
            if isinstance(obj, dict):
                for value in obj.values():
                    walk(value)
            elif isinstance(obj, list):
                for value in obj:
                    walk(value)
            elif isinstance(obj, str):
                push(obj)

        try:
            parsed = json.loads(body)
            walk(parsed)
        except Exception:
            pass

        clean_body = html_parser.unescape(body).replace("\\/", "/")

        for raw in re.findall(r'https%3A%2F%2F[^"\'<>\s]+', clean_body, flags=re.I):
            push(unquote(raw))

        for pat in [
            r'https?://svt-vod-[^"\'<>\s]+',
            r'https?://[^"\'<>\s]+\.m3u8[^"\'<>\s]*',
            r'https?://[^"\'<>\s]+\.mpd[^"\'<>\s]*',
            r'https?://[^"\'<>\s]+\.mp4[^"\'<>\s]*',
        ]:
            for raw in re.findall(pat, clean_body, flags=re.I):
                push(raw)

        picked = _pick_best_media_url(candidates)
        if picked and picked != url:
            logger.info(f"[{job_id}] SVT Switcher: resolved -> {picked}")
            return picked

        logger.info(f"[{job_id}] SVT Switcher: hittade ingen underliggande media-URL, behåller original.")
        return url

    except Exception as e:
        logger.info(f"[{job_id}] SVT Switcher: resolver misslyckades: {e}")
        return url

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
            target_url = _resolve_svt_switcher_if_needed(job_id, target_url)
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
                if current_url.startswith("https://127.0.0.1/force_fail_to_trigger_playwright"):
                    _update_history_status(job_id, url, "error")
                    _set_job(job_id, {
                        "status": "error",
                        "message": "Ingen video kunde kopplas till exakt SVT-inlägg. Avbröt för att inte hämta fel video."
                    })
                    return

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


def _lb1815_clear_svt_consent_overlay(page, job_id: str) -> None:
    """Remove/accept SVT consent overlay that can intercept Playwright clicks."""
    try:
        clicked = False

        candidates = [
            'button:has-text("Godkänn")',
            'button:has-text("Acceptera")',
            'button:has-text("Acceptera alla")',
            'button:has-text("Tillåt alla")',
            'button:has-text("OK")',
            'button:has-text("Stäng")',
            '[role="dialog"] button',
            '[class*="ConsentDialog"] button',
        ]

        for sel in candidates:
            try:
                loc = page.locator(sel)
                if loc.count() > 0:
                    loc.first.click(timeout=1200, force=True)
                    logger.info(f"[{job_id}] SVT consent-clickfix: klickade/kvitterade overlay via {sel}")
                    clicked = True
                    page.wait_for_timeout(400)
                    break
            except Exception:
                pass

        removed = page.evaluate("""() => {
            let count = 0;
            const selectors = [
                '[class*="ConsentDialog"]',
                '[class*="Cookie"]',
                '[class*="cookie"]',
                '[data-testid*="cookie"]',
                '[data-testid*="consent"]',
                '[role="dialog"]'
            ];

            for (const el of Array.from(document.querySelectorAll(selectors.join(',')))) {
                const txt = (el.innerText || el.textContent || '').toLowerCase();
                const cls = String(el.className || '').toLowerCase();
                const looksLikeConsent =
                    cls.includes('consent') ||
                    cls.includes('cookie') ||
                    txt.includes('cookie') ||
                    txt.includes('kakor') ||
                    txt.includes('samtycke') ||
                    txt.includes('personuppgifter') ||
                    txt.includes('integritet');

                if (looksLikeConsent) {
                    el.remove();
                    count++;
                }
            }

            document.documentElement.style.overflow = 'auto';
            document.body.style.overflow = 'auto';
            return count;
        }""")

        if removed:
            logger.info(f"[{job_id}] SVT consent-clickfix: tog bort overlay-element={removed}")
        elif not clicked:
            logger.info(f"[{job_id}] SVT consent-clickfix: ingen tydlig overlay hittad")
    except Exception as e:
        logger.info(f"[{job_id}] SVT consent-clickfix: overlay cleanup misslyckades: {e}")


def _lb1815_click_play_button_safely(page, locator, job_id: str, label: str) -> None:
    """Click ranked SVT play button even when overlays/actionability block normal click."""
    _lb1815_clear_svt_consent_overlay(page, job_id)

    try:
        locator.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass

    try:
        locator.click(timeout=5000)
        logger.info(f"[{job_id}] SVT consent-clickfix: normal klick OK: {label}")
        return
    except Exception as e:
        logger.info(f"[{job_id}] SVT consent-clickfix: normal klick blockerades: {e}")

    _lb1815_clear_svt_consent_overlay(page, job_id)

    try:
        locator.click(timeout=3000, force=True)
        logger.info(f"[{job_id}] SVT consent-clickfix: force-klick OK: {label}")
        return
    except Exception as e:
        logger.info(f"[{job_id}] SVT consent-clickfix: force-klick blockerades: {e}")

    try:
        handle = locator.element_handle(timeout=3000)
        if not handle:
            raise RuntimeError("no element_handle for ranked play button")
        page.evaluate("(el) => el.click()", handle)
        logger.info(f"[{job_id}] SVT consent-clickfix: JS-click OK: {label}")
        page.wait_for_timeout(500)
        return
    except Exception as e:
        logger.info(f"[{job_id}] SVT consent-clickfix: JS-click misslyckades: {e}")
        raise

# === LjudBuster 1.8.14-dev: SVT ranked aria sniper override ===

def _lb1814_norm_text(value: str) -> str:
    value = html_parser.unescape(str(value or ""))
    value = value.replace("\\/", "/").replace("\xa0", " ")
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _lb1814_tokens(value: str) -> set:
    value = _lb1814_norm_text(value).lower()
    value = value.replace("—", " ").replace("–", " ").replace("-", " ")
    words = re.findall(r"[a-zåäö0-9]{3,}", value, flags=re.I)
    stop = {
        "spela", "rapport", "video", "sek", "min", "och", "att", "det", "som",
        "för", "från", "till", "med", "den", "detta", "där", "här", "har",
        "hur", "vad", "när", "senaste", "nytt", "usa", "politik"
    }
    return {w for w in words if w not in stop}


def _lb1814_is_media_url(url: str) -> bool:
    lower = str(url or "").lower()

    if not lower.startswith("http"):
        return False

    reject = [
        "__manifest",
        "/assets/manifest-",
        "news-render/assets",
        "firestore.googleapis.com",
        "google.firestore",
        ".js",
        ".css",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".svg",
        ".woff",
        ".woff2",
    ]
    if any(x in lower for x in reject):
        return False

    accept = [
        ".m3u8",
        ".mpd",
        "switcher.cdn.svt.se/resolve/",
        "api.svt.se/ditto/api/v3/manifest",
        "svt-vod",
        "cdn.svt.se/d0/",
        "akamaized.net/",
    ]
    return any(x in lower for x in accept)


def _lb1814_unwrap_media_urls(blob: str) -> list:
    from urllib.parse import parse_qs, unquote, urlparse

    raw = html_parser.unescape(str(blob or "")).replace("\\/", "/")
    out = []

    def push(u: str) -> None:
        u = html_parser.unescape(str(u or "")).replace("\\/", "/").strip()
        u = u.rstrip(".,;)'\"<>]")
        if not u:
            return

        try:
            parsed = urlparse(u)
            qs = parse_qs(parsed.query)
            for key in ("manifestUrl", "manifesturl", "url"):
                for nested in qs.get(key, []):
                    nested = unquote(nested)
                    if _lb1814_is_media_url(nested):
                        out.append(nested)
        except Exception:
            pass

        if _lb1814_is_media_url(u):
            out.append(u)

        # Chromium/SVT ger ofta DASH via switcher. Lägg HLS-syskon som förstaval också.
        if "switcher.cdn.svt.se/resolve/" in u and u.endswith("/dash-full.mpd"):
            out.append(u.replace("/dash-full.mpd", "/hls-cmaf-full.m3u8"))
            out.append(u.replace("/dash-full.mpd", "/hls-cmaf-avc.m3u8"))

        if "/dash-full.mpd" in u:
            out.append(u.replace("/dash-full.mpd", "/hls-cmaf-full.m3u8"))
            out.append(u.replace("/dash-full.mpd", "/hls-cmaf-avc.m3u8"))

    for m in re.finditer(r"https?://[^\s\"'<>\\)]+", raw):
        push(m.group(0))

    for m in re.finditer(r"manifestUrl=([^&\"'<>\\)]+)", raw, flags=re.I):
        push(unquote(m.group(1)))

    seen = set()
    clean = []
    for u in out:
        if not u or u in seen:
            continue
        if not _lb1814_is_media_url(u):
            continue
        seen.add(u)
        clean.append(u)

    return clean


def _lb1814_extract_strict_post_block(html: str, inlagg_id: str) -> str:
    """Returnerar bara block om vi hittar riktig id-attribut-träff, inte bara id i manifest/query."""
    if not html or not inlagg_id:
        return ""

    patterns = [
        f'id="{inlagg_id}"',
        f"id='{inlagg_id}'",
        f'id=&quot;{inlagg_id}&quot;',
    ]

    pos = -1
    for pat in patterns:
        pos = html.find(pat)
        if pos >= 0:
            break

    if pos < 0:
        return ""

    # Backa till närmaste <div före id-attributet.
    start = html.rfind("<div", 0, pos)
    if start < 0:
        start = max(0, pos - 2000)

    # Enkel men robust nog: ta rimlig post-slice fram till några kommande post-root/id.
    end = len(html)
    next_markers = [
        html.find('class="_Post__root', pos + len(inlagg_id)),
        html.find('data-created-at=', pos + len(inlagg_id)),
        html.find('<div id="', pos + len(inlagg_id)),
        html.find("<div id='", pos + len(inlagg_id)),
    ]
    next_markers = [x for x in next_markers if x > pos]
    if next_markers:
        end = min(next_markers)

    # Säkerhetscap så vi inte råkar mata in halva sidan.
    end = min(end, start + 45000)
    return html[start:end]


def _lb1814_extract_match_terms(scope_html: str) -> list:
    terms = []

    raw = html_parser.unescape(str(scope_html or "")).replace("\\/", "/")

    # Aria labels är bäst eftersom de ofta exakt speglar play-knappen.
    for m in re.finditer(r'aria-label=["\']([^"\']{8,180})["\']', raw, flags=re.I):
        terms.append(_lb1814_norm_text(m.group(1)))

    # Rubriker / text i postblock.
    for tag in ("h1", "h2", "h3", "h4", "span", "p"):
        for m in re.finditer(rf"<{tag}[^>]*>(.*?)</{tag}>", raw, flags=re.I | re.S):
            val = _lb1814_norm_text(m.group(1))
            if 10 <= len(val) <= 160:
                terms.append(val)

    bad_exact = {"video", "senaste nytt", "visa inlägg"}
    cleaned = []
    seen = set()

    for t in terms:
        t = _lb1814_norm_text(t)
        low = t.lower()

        if len(t) < 10:
            continue
        if low in bad_exact:
            continue
        if low.startswith("senaste nytt om ") and len(t) < 45:
            continue
        if t in seen:
            continue

        seen.add(t)
        cleaned.append(t)

    # Längre / mer specifika termer först.
    cleaned.sort(key=lambda x: (len(_lb1814_tokens(x)), len(x)), reverse=True)
    return cleaned[:20]


def _lb1814_score_button(label: str, terms: list) -> int:
    label_n = _lb1814_norm_text(label).lower()
    label_tokens = _lb1814_tokens(label)
    score = 0

    if not label_tokens:
        return 0

    for term in terms:
        term_n = _lb1814_norm_text(term).lower()
        term_tokens = _lb1814_tokens(term)

        if not term_tokens:
            continue

        if term_n and term_n in label_n:
            score += 1000 + (len(term_tokens) * 80) + len(term_n)

        overlap = label_tokens & term_tokens
        if overlap:
            score += len(overlap) * 120
            if len(overlap) >= 3:
                score += len(overlap) * 160

        # Extra bonus när en kort rubrik och aria-label säger samma sak men inte exakt.
        if len(overlap) >= max(2, min(4, len(term_tokens))):
            score += 300

    return score


def _lb1814_resolve_svt_livepost_ranked(job_id: str, article_url: str, inlagg_id: str) -> list:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        logger.info(f"[{job_id}] SVT ranked-sniper: playwright import fail: {e}")
        return []

    media_after_click = []
    media_all = []
    clicked = {"active": False}

    def remember(url: str, source: str) -> None:
        for candidate in _lb1814_unwrap_media_urls(url):
            if candidate not in media_all:
                media_all.append(candidate)

            if clicked["active"] and candidate not in media_after_click:
                media_after_click.append(candidate)
                logger.info(f"[{job_id}] SVT ranked-sniper: fångade media efter klick ({source}): {candidate}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--autoplay-policy=no-user-gesture-required",
            ],
        )

        context = browser.new_context(
            viewport={"width": 1280, "height": 9000},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/142.0.0.0 Safari/537.36"
            ),
            locale="sv-SE",
        )

        page = context.new_page()

        page.on("request", lambda req: remember(req.url, "request"))

        def on_response(resp):
            try:
                remember(resp.url, "response-url")
                ctype = (resp.headers or {}).get("content-type", "")
                if any(x in ctype.lower() for x in ("json", "text", "mpegurl", "dash", "xml")):
                    try:
                        body = resp.text()
                        for candidate in _lb1814_unwrap_media_urls(body):
                            remember(candidate, "response-body")
                    except Exception:
                        pass
            except Exception:
                pass

        page.on("response", on_response)

        try:
            logger.info(f"[{job_id}] SVT ranked-sniper: öppnar livepost-sida")
            page.goto(article_url, wait_until="domcontentloaded", timeout=35000)
            page.wait_for_timeout(7000)

            html = page.content()
            strict_block = _lb1814_extract_strict_post_block(html, inlagg_id)

            if strict_block:
                logger.info(f"[{job_id}] SVT ranked-sniper: hittade strikt postblock för id={inlagg_id} bytes={len(strict_block)}")
                terms = _lb1814_extract_match_terms(strict_block)

                direct = _lb1814_unwrap_media_urls(strict_block)
                if direct:
                    logger.info(f"[{job_id}] SVT ranked-sniper: hittade media direkt i strikt postblock: {direct[0]}")
                    return direct
            else:
                logger.info(
                    f"[{job_id}] SVT ranked-sniper: inget strikt postblock för id={inlagg_id}. "
                    "Använder rankad aria-fallback utan global mediafallback."
                )

                # Fallback: använd liten, försiktig window runt id-förekomst ENDAST för texttermer,
                # aldrig för direkt media, eftersom id kan ligga i __manifest-brus.
                pos = html.find(inlagg_id)
                if pos >= 0:
                    loose = html[max(0, pos - 6000):min(len(html), pos + 30000)]
                    terms = _lb1814_extract_match_terms(loose)
                else:
                    terms = []

            logger.info(f"[{job_id}] SVT ranked-sniper: match-termer={terms}")

            if not terms:
                logger.info(f"[{job_id}] SVT ranked-sniper: inga match-termer, avbryter livepost för att undvika fel video.")
                return []

            buttons = page.locator('button[data-testid="play-pause-button"], button[data-rt="video-player-splash-play"], button[aria-label*="Spela"]')
            count = buttons.count()

            ranked = []
            for i in range(count):
                btn = buttons.nth(i)
                try:
                    if not btn.is_visible(timeout=800):
                        continue

                    label = btn.get_attribute("aria-label") or btn.inner_text(timeout=800) or ""
                    label = _lb1814_norm_text(label)
                    box = btn.bounding_box() or {}
                    score = _lb1814_score_button(label, terms)

                    ranked.append({
                        "index": i,
                        "score": score,
                        "label": label,
                        "y": box.get("y", 999999),
                    })
                except Exception:
                    continue

            ranked.sort(key=lambda x: (x["score"], -int(x["y"])), reverse=True)

            logger.info(f"[{job_id}] SVT ranked-sniper: knapp-ranking topp={ranked[:5]}")

            if not ranked or ranked[0]["score"] <= 0:
                logger.info(f"[{job_id}] SVT ranked-sniper: ingen knapp fick positiv score.")
                return []

            chosen = ranked[0]
            btn = buttons.nth(chosen["index"])
            btn.scroll_into_view_if_needed(timeout=3000)

            clicked["active"] = True
            _lb1815_click_play_button_safely(page, btn, job_id, best.get("label", "") if isinstance(best, dict) else "")
            logger.info(
                f"[{job_id}] SVT ranked-sniper: klickade rankad play-knapp "
                f"score={chosen['score']} label={chosen['label']}"
            )

            page.wait_for_timeout(12000)

            # Läs video[src] efter klick som sista scoped källa.
            try:
                video_srcs = page.evaluate("""
                    () => Array.from(document.querySelectorAll('video'))
                        .map(v => v.currentSrc || v.src || '')
                        .filter(Boolean)
                """)
                for src in video_srcs or []:
                    remember(src, "video-src-after-click")
            except Exception as e:
                logger.info(f"[{job_id}] SVT ranked-sniper: kunde inte läsa video[src] efter klick: {e}")

        except Exception as e:
            logger.info(f"[{job_id}] SVT ranked-sniper: misslyckades: {e}")
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass

    # Bara efter-klick-kandidater får användas för livepost.
    out = []
    seen = set()
    for u in media_after_click:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)

    if out:
        logger.info(f"[{job_id}] SVT ranked-sniper: slutkandidat={out[0]} totalt={len(out)}")
    else:
        logger.info(f"[{job_id}] SVT ranked-sniper: ingen media fångad efter rankad klick.")

    return out


def _resolve_manifests_via_playwright(job_id: str, article_url: str) -> List[str]:
    """1.8.14 override.

    SVT ?inlagg= kör strikt/rankad livepost-sniper.
    Övriga sidor går till legacy-resolvern.
    """
    try:
        parsed = urlparse(article_url)
        query = parse_qs(parsed.query)
        inlagg_id = query.get("inlagg", [None])[0]
        is_svt = "svt.se" in (article_url or "").lower() or "svtplay.se" in (article_url or "").lower()

        if is_svt and inlagg_id:
            logger.info(f"[{job_id}] SVT ranked-sniper: använder 1.8.14 livepost-väg för inlägg {inlagg_id}")
            urls = _lb1814_resolve_svt_livepost_ranked(job_id, article_url, inlagg_id)
            if urls:
                return urls

            logger.info(
                f"[{job_id}] SVT ranked-sniper: ingen video kunde kopplas till exakt inlägg. "
                "Avbryter utan global fallback."
            )
            return []

        if "_resolve_manifests_via_playwright_legacy_1812" in globals():
            return _resolve_manifests_via_playwright_legacy_1812(job_id, article_url)

        return []

    except Exception as e:
        logger.info(f"[{job_id}] SVT ranked-sniper: wrapper misslyckades: {e}")
        return []

# === End LjudBuster 1.8.14-dev override ===


# === LjudBuster 1.8.16-dev: final SVT Playwright override ===

def _lb1816_norm(value):
    value = html_parser.unescape(str(value or ""))
    value = value.replace("\\/", "/").replace("\xa0", " ")
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _lb1816_tokens(value):
    value = _lb1816_norm(value).lower()
    value = value.replace("—", " ").replace("–", " ").replace("-", " ")
    words = re.findall(r"[a-zåäö0-9]{3,}", value, flags=re.I)
    stop = {
        "spela", "rapport", "video", "sek", "min", "och", "att", "det", "som",
        "för", "från", "till", "med", "den", "detta", "där", "här", "har",
        "hur", "vad", "när", "senaste", "nytt", "usa", "politik", "visa",
        "inlägg", "inlagg", "öppna", "meny", "fler", "händelser"
    }
    return {w for w in words if w not in stop}


def _lb1816_unwrap_media_urls(blob):
    from urllib.parse import parse_qs, unquote, urlparse

    raw = html_parser.unescape(str(blob or "")).replace("\\/", "/")
    out = []

    def push(url):
        url = html_parser.unescape(str(url or "")).replace("\\/", "/").strip()
        url = url.strip('\'"<>),;')
        if not url.startswith("http"):
            return

        lower = url.lower()

        if "__manifest" in lower:
            return
        if "/assets/manifest-" in lower:
            return
        if "firestore.googleapis.com" in lower:
            return
        if "sentry" in lower:
            return

        if "api.svt.se/ditto/api" in lower and "manifesturl=" in lower:
            try:
                qs = parse_qs(urlparse(url).query)
                nested = qs.get("manifestUrl") or qs.get("manifesturl")
                if nested:
                    push(unquote(nested[0]))
                    return
            except Exception:
                pass

        if (
            ".m3u8" in lower
            or ".mpd" in lower
            or "switcher.cdn.svt.se/resolve/" in lower
            or "svt-vod" in lower
            or "cdn.svt.se" in lower
            or "akamaized.net" in lower
        ):
            out.append(url)

    for m in re.finditer(r'manifestUrl=([^&"\'<> ]+)', raw, flags=re.I):
        push(unquote(m.group(1)))

    for m in re.finditer(r'https?://[^"\'<>\s]+', raw):
        push(m.group(0))

    seen = set()
    clean = []
    for u in out:
        if u not in seen:
            seen.add(u)
            clean.append(u)
    return clean


def _lb1816_resolve_final_media(job_id, url):
    url = str(url or "").strip()
    if not url:
        return ""

    if "api.svt.se/ditto/api" in url.lower() and "manifesturl=" in url.lower():
        nested = _lb1816_unwrap_media_urls(url)
        if nested:
            url = nested[0]

    if "switcher.cdn.svt.se/resolve/" in url.lower():
        try:
            return _resolve_svt_switcher_if_needed(job_id, url)
        except Exception as e:
            logger.info(f"[{job_id}] SVT 1.8.16: switcher resolve misslyckades: {e}")
            return url

    return url


def _lb1816_clear_consent(page, job_id):
    try:
        clicked = False
        selectors = [
            'button:has-text("Godkänn")',
            'button:has-text("Acceptera")',
            'button:has-text("Acceptera alla")',
            'button:has-text("Tillåt alla")',
            'button:has-text("OK")',
            '[class*="ConsentDialog"] button',
            '[role="dialog"] button',
        ]

        for sel in selectors:
            try:
                loc = page.locator(sel)
                if loc.count() > 0:
                    loc.first.click(timeout=800, force=True)
                    logger.info(f"[{job_id}] SVT 1.8.16: consent-overlay kvitterad via {sel}")
                    page.wait_for_timeout(300)
                    clicked = True
                    break
            except Exception:
                pass

        removed = page.evaluate("""() => {
            let count = 0;
            const selectors = [
                '[class*="ConsentDialog"]',
                '[class*="Cookie"]',
                '[class*="cookie"]',
                '[data-testid*="cookie"]',
                '[data-testid*="consent"]',
                '[role="dialog"]'
            ];
            for (const el of Array.from(document.querySelectorAll(selectors.join(',')))) {
                const txt = (el.innerText || el.textContent || '').toLowerCase();
                const cls = String(el.className || '').toLowerCase();
                const hit =
                    cls.includes('consent') ||
                    cls.includes('cookie') ||
                    txt.includes('cookie') ||
                    txt.includes('kakor') ||
                    txt.includes('samtycke') ||
                    txt.includes('personuppgifter') ||
                    txt.includes('integritet');

                if (hit) {
                    el.remove();
                    count++;
                }
            }
            document.documentElement.style.overflow = 'auto';
            document.body.style.overflow = 'auto';
            return count;
        }""")

        if removed:
            logger.info(f"[{job_id}] SVT 1.8.16: consent-overlay borttagen element={removed}")
        elif not clicked:
            logger.info(f"[{job_id}] SVT 1.8.16: ingen consent-overlay hittad")
    except Exception as e:
        logger.info(f"[{job_id}] SVT 1.8.16: consent-cleanup fel: {e}")


def _lb1816_button_scoped_video_src(button):
    try:
        return button.evaluate("""(btn) => {
            function clean(v) {
                if (!v) return "";
                return String(v).replaceAll("&amp;", "&");
            }

            let node = btn;
            for (let depth = 0; node && depth < 12; depth++, node = node.parentElement) {
                const video = node.querySelector && node.querySelector('video[src]');
                if (video && video.src) return clean(video.src);

                const source = node.querySelector && node.querySelector('source[src]');
                if (source && source.src) return clean(source.src);

                const html = node.innerHTML || "";
                const m = html.match(/https?:\\/\\/[^"'<>\\s]+(?:m3u8|mpd|manifestUrl=[^"'<>\\s]+)/i);
                if (m) return clean(m[0]);
            }

            return "";
        }""")
    except Exception:
        return ""


def _lb1816_terms_from_page_content(page, inlagg_id):
    html = page.content()
    terms = []

    # Try window around exact id. SVT sometimes has id in rendered payload rather than queryable DOM.
    idx = html.find(inlagg_id)
    if idx >= 0:
        window = html[max(0, idx - 9000): idx + 22000]
    else:
        window = html

    # aria-labels from play buttons
    for m in re.finditer(r'aria-label=["\']([^"\']{8,220})["\']', window, flags=re.I):
        terms.append(_lb1816_norm(m.group(1)))

    # headings/spans/paragraph-ish text
    for m in re.finditer(r'<(?:h1|h2|h3|span|p|em)[^>]*>(.*?)</(?:h1|h2|h3|span|p|em)>', window, flags=re.I | re.S):
        t = _lb1816_norm(m.group(1))
        if 8 <= len(t) <= 220:
            terms.append(t)

    # visible button-ish text
    for m in re.finditer(r'<button[^>]*>(.*?)</button>', window, flags=re.I | re.S):
        t = _lb1816_norm(m.group(1))
        if 3 <= len(t) <= 160:
            terms.append(t)

    seen = set()
    out = []
    for t in terms:
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)

    return out[:40], window


def _lb1816_rank_play_buttons(page, terms):
    term_tokens = set()
    term_text = " ".join(terms).lower()

    for t in terms:
        term_tokens |= _lb1816_tokens(t)

    buttons = page.locator('button[data-testid="play-pause-button"], button[data-rt="video-player-splash-play"], button[aria-label*="Spela"]')

    ranked = []
    count = buttons.count()

    for i in range(count):
        btn = buttons.nth(i)
        try:
            label = btn.get_attribute("aria-label") or ""
            text = btn.inner_text(timeout=1000) or ""
            box = btn.bounding_box() or {}
            y = float(box.get("y") or 0)
        except Exception:
            continue

        hay = f"{label} {text}"
        btoks = _lb1816_tokens(hay)

        score = 0
        overlap = btoks & term_tokens
        score += len(overlap) * 1000

        low_label = label.lower()

        # Strong phrase scoring: actual titles from the target post should dominate.
        for term in terms:
            tl = term.lower()
            if len(tl) >= 12 and tl in low_label:
                score += min(len(tl), 120) * 50

        # De-prioritize generic page/player labels unless they also have strong token overlap.
        if "spela rapport" in low_label:
            score += 50
        if "trump" in low_label and "hemska" in low_label:
            score += 5000
        if "bilder från hotellet" in low_label:
            score -= 1000

        if score > 0:
            ranked.append({
                "index": i,
                "score": score,
                "label": label,
                "text": text,
                "y": y,
                "overlap": sorted(overlap),
            })

    ranked.sort(key=lambda x: (-x["score"], x["y"]))
    return buttons, ranked


def _lb1816_resolve_svt_livepost(job_id, article_url, inlagg_id):
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        logger.info(f"[{job_id}] SVT 1.8.16: playwright import fail: {e}")
        return []

    captured = []
    collecting = {"active": False}

    def push_candidate(url, source):
        for u in _lb1816_unwrap_media_urls(url):
            final = _lb1816_resolve_final_media(job_id, u)
            if final and final not in captured:
                captured.append(final)
                logger.info(f"[{job_id}] SVT 1.8.16: fångade media ({source}): {final}")

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--autoplay-policy=no-user-gesture-required",
                ],
            )

            context = browser.new_context(
                viewport={"width": 1440, "height": 1200},
                locale="sv-SE",
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/142.0.0.0 Safari/537.36"
                ),
            )

            page = context.new_page()

            def on_request(req):
                if collecting["active"]:
                    push_candidate(req.url, "request-after-click")

            def on_response(resp):
                if collecting["active"]:
                    push_candidate(resp.url, "response-after-click")

            page.on("request", on_request)
            page.on("response", on_response)

            logger.info(f"[{job_id}] SVT 1.8.16: öppnar livepost-sida")
            page.goto(article_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(6500)

            terms, html_window = _lb1816_terms_from_page_content(page, inlagg_id)
            direct = _lb1816_unwrap_media_urls(html_window)

            if direct:
                final_direct = [_lb1816_resolve_final_media(job_id, u) for u in direct]
                final_direct = [u for u in final_direct if u]
                if final_direct:
                    logger.info(f"[{job_id}] SVT 1.8.16: hittade media direkt i HTML-window: {final_direct[0]}")
                    browser.close()
                    return final_direct[:3]

            logger.info(f"[{job_id}] SVT 1.8.16: match-termer={terms[:20]}")

            if not terms:
                logger.info(f"[{job_id}] SVT 1.8.16: inga match-termer, avbryter för att undvika fel video")
                browser.close()
                return []

            buttons, ranked = _lb1816_rank_play_buttons(page, terms)
            logger.info(f"[{job_id}] SVT 1.8.16: knapp-ranking topp={ranked[:5]}")

            if not ranked:
                logger.info(f"[{job_id}] SVT 1.8.16: ingen play-knapp fick positiv score")
                browser.close()
                return []

            best = ranked[0]
            button = buttons.nth(best["index"])

            # First: read the video src from the same player container. This is the least flaky path.
            pre_src = _lb1816_button_scoped_video_src(button)
            pre_urls = _lb1816_unwrap_media_urls(pre_src)
            if pre_urls:
                final = [_lb1816_resolve_final_media(job_id, u) for u in pre_urls]
                final = [u for u in final if u]
                if final:
                    logger.info(
                        f"[{job_id}] SVT 1.8.16: hittade scoped video[src] före klick "
                        f"label={best['label']} url={final[0]}"
                    )
                    browser.close()
                    return final[:3]

            _lb1816_clear_consent(page, job_id)

            try:
                button.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass

            collecting["active"] = True

            clicked = False
            try:
                button.click(timeout=5000)
                clicked = True
                logger.info(f"[{job_id}] SVT 1.8.16: normal klick OK label={best['label']}")
            except Exception as e:
                logger.info(f"[{job_id}] SVT 1.8.16: normal klick fail: {e}")

            if not clicked:
                _lb1816_clear_consent(page, job_id)
                try:
                    button.click(timeout=3000, force=True)
                    clicked = True
                    logger.info(f"[{job_id}] SVT 1.8.16: force-klick OK label={best['label']}")
                except Exception as e:
                    logger.info(f"[{job_id}] SVT 1.8.16: force-klick fail: {e}")

            if not clicked:
                try:
                    handle = button.element_handle(timeout=3000)
                    page.evaluate("(el) => el.click()", handle)
                    clicked = True
                    logger.info(f"[{job_id}] SVT 1.8.16: JS-click OK label={best['label']}")
                except Exception as e:
                    logger.info(f"[{job_id}] SVT 1.8.16: JS-click fail: {e}")

            page.wait_for_timeout(7000)
            collecting["active"] = False

            post_src = _lb1816_button_scoped_video_src(button)
            for u in _lb1816_unwrap_media_urls(post_src):
                final = _lb1816_resolve_final_media(job_id, u)
                if final and final not in captured:
                    captured.append(final)
                    logger.info(f"[{job_id}] SVT 1.8.16: hittade scoped video[src] efter klick: {final}")

            browser.close()

    except Exception as e:
        logger.info(f"[{job_id}] SVT 1.8.16: resolver misslyckades: {e}")

    if captured:
        logger.info(f"[{job_id}] SVT 1.8.16: slutkandidat={captured[0]} totalt={len(captured)}")
    else:
        logger.info(f"[{job_id}] SVT 1.8.16: ingen media kunde kopplas till exakt inlägg")

    return captured[:3]


def _resolve_manifests_via_playwright(job_id: str, article_url: str):
    from urllib.parse import parse_qs, urlparse

    try:
        parsed = urlparse(article_url)
        inlagg_id = parse_qs(parsed.query).get("inlagg", [None])[0]
        is_svt = "svt.se" in (article_url or "").lower() or "svtplay.se" in (article_url or "").lower()

        if is_svt and inlagg_id:
            logger.info(f"[{job_id}] SVT 1.8.16: använder final livepost-resolver för inlägg {inlagg_id}")
            urls = _lb1816_resolve_svt_livepost(job_id, article_url, inlagg_id)

            if urls:
                return urls

            logger.info(
                f"[{job_id}] SVT 1.8.16: ingen video kunde kopplas till exakt inlägg. "
                "Avbryter utan global fallback."
            )
            return []

    except Exception as e:
        logger.info(f"[{job_id}] SVT 1.8.16: wrapper fel: {e}")
        return []

    try:
        return _resolve_manifests_via_playwright_legacy_1812(job_id, article_url)
    except NameError:
        logger.info(f"[{job_id}] SVT 1.8.16: legacy resolver saknas")
        return []

# === End LjudBuster 1.8.16-dev override ===


# === LjudBuster 1.8.17-dev: SVT title NEAR resolver override ===

_lb1817_previous_resolve_manifests_via_playwright = _resolve_manifests_via_playwright


def _lb1817_norm_text(value: str) -> str:
    value = html_parser.unescape(str(value or ""))
    value = value.replace("\\/", "/").replace("\xa0", " ")
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _lb1817_tokens(value: str) -> set:
    value = _lb1817_norm_text(value).lower()
    value = value.replace("—", " ").replace("–", " ").replace("-", " ")
    words = re.findall(r"[a-zåäö0-9]{3,}", value, flags=re.I)
    stop = {
        "spela", "rapport", "video", "sek", "min", "och", "att", "det",
        "som", "för", "från", "till", "med", "den", "detta", "där", "här",
        "har", "hur", "vad", "när", "senaste", "nytt", "usa", "politik",
        "visa", "länkar", "under", "meny", "öppna", "huvudmeny", "snabbmeny",
    }
    return {w for w in words if w and w not in stop}


def _lb1817_looks_like_media_url(url: str) -> bool:
    lower = str(url or "").lower()

    if not lower.startswith("http"):
        return False

    bad = [
        "doubleclick", "freewheel", "videoplaza", "adform", "googlesyndication",
        "firestore.googleapis.com", "__manifest", "/assets/manifest-",
        "sentry", "analytics", "privacy-mgmt",
    ]
    if any(x in lower for x in bad):
        return False

    good = [
        ".m3u8", ".mpd", ".mp4", ".m4a", ".mp3",
        "api.svt.se/ditto/api/v3/manifest",
        "switcher.cdn.svt.se/resolve/",
        "svt-vod", ".cdn.svt.se/d0/",
        "video.svt.se/video/",
    ]
    return any(x in lower for x in good)


def _lb1817_unwrap_media_url(job_id: str, url: str) -> list:
    from urllib.parse import parse_qs, urlparse, unquote

    raw = str(url or "").strip()
    if not raw:
        return []

    out = []

    try:
        parsed = urlparse(raw)
        qs = parse_qs(parsed.query)

        if "api.svt.se/ditto/api/v3/manifest" in raw and qs.get("manifestUrl"):
            out.append(unquote(qs["manifestUrl"][0]))

        elif "switcher.cdn.svt.se/resolve/" in raw:
            try:
                out.append(_resolve_svt_switcher_if_needed(job_id, raw))
            except Exception:
                out.append(raw)

        else:
            out.append(raw)

    except Exception:
        out.append(raw)

    clean = []
    for item in out:
        item = str(item or "").strip()
        if item and item not in clean and _lb1817_looks_like_media_url(item):
            clean.append(item)

    return clean


def _lb1817_extract_title_terms_from_html(html: str, inlagg_id: str) -> list:
    """Tar bara text EFTER exakt inlägg-id.

    Det är kärnfixen jämfört med 1.8.16:
    tidigare kunde text från föregående post hamna i scoring-fönstret.
    """
    if not html or not inlagg_id:
        return []

    pos = html.find(inlagg_id)
    if pos < 0:
        return []

    # Bara framåt från träffen. Inte bakåt till sidtopp/föregående inlägg.
    window = html[pos:pos + 45000]

    terms = []

    # Starkast: rubriker efter id:t, t.ex. <h3><span>Trump rasar...</span></h3>
    for m in re.finditer(r"<h[1-4][^>]*>(.*?)</h[1-4]>", window, flags=re.I | re.S):
        t = _lb1817_norm_text(m.group(1))
        if 12 <= len(t) <= 180:
            terms.append(t)

    # Näst starkast: videoknappens aria-label efter id:t.
    for m in re.finditer(r'aria-label=["\']([^"\']*Spela[^"\']+)["\']', window, flags=re.I | re.S):
        t = _lb1817_norm_text(m.group(1))
        if 12 <= len(t) <= 220:
            terms.append(t)

    # Extra stöd: kortare textfragment i postens brödtext, men bara sådant med vettiga tokens.
    for m in re.finditer(r"<p[^>]*>(.*?)</p>", window, flags=re.I | re.S):
        t = _lb1817_norm_text(m.group(1))
        toks = _lb1817_tokens(t)
        if 18 <= len(t) <= 220 and len(toks) >= 3:
            terms.append(t)

    # Rensa generiskt skräp.
    bad_exact = {
        "Video", "Fler händelser", "Visa inlägget", "Skriv inlägg",
        "Öppna meny", "Visa länkar under Nyheter", "Visa länkar under Lokalt",
        "Visa länkar under Sport",
    }

    unique = []
    for term in terms:
        term = _lb1817_norm_text(term)
        if not term or term in bad_exact:
            continue
        if len(_lb1817_tokens(term)) < 2:
            continue
        if term not in unique:
            unique.append(term)

    return unique[:12]


def _lb1817_accept_or_remove_consent(page, job_id: str) -> None:
    selectors = [
        'button:has-text("Tillåt alla")',
        'button:has-text("Godkänn alla cookies")',
        'button:has-text("Godkänn alla")',
        'button:has-text("Acceptera")',
        'button:has-text("Jag godkänner")',
    ]

    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible(timeout=700):
                btn.click(timeout=1200)
                logger.info(f"[{job_id}] SVT 1.8.17: consent kvitterad via {sel}")
                page.wait_for_timeout(300)
                break
        except Exception:
            pass

    # Fallback: om SVT-overlay fortfarande fångar klick, ta bort den lokalt i headless-DOM.
    try:
        removed = page.evaluate("""() => {
            const nodes = Array.from(document.querySelectorAll(
                '[class*="ConsentDialog"], [data-testid*="consent"], div[role="main"]'
            ));
            let n = 0;
            for (const el of nodes) {
                const txt = (el.innerText || '').toLowerCase();
                const cls = (el.className || '').toString().toLowerCase();
                if (txt.includes('cookies') || txt.includes('samtycke') || cls.includes('consentdialog')) {
                    el.remove();
                    n++;
                }
            }
            return n;
        }""")
        if removed:
            logger.info(f"[{job_id}] SVT 1.8.17: consent-overlay borttagen element={removed}")
    except Exception:
        pass


def _lb1817_resolve_svt_livepost_by_title_near(job_id: str, article_url: str, inlagg_id: str) -> list:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        logger.info(f"[{job_id}] SVT 1.8.17: playwright import fail: {e}")
        return []

    found = []
    armed = {"value": False}

    def add_url(raw_url: str, source: str) -> None:
        for candidate in _lb1817_unwrap_media_url(job_id, raw_url):
            if candidate not in found:
                found.append(candidate)
                logger.info(f"[{job_id}] SVT 1.8.17: fångade media ({source}): {candidate}")

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 1800},
            )
            page = ctx.new_page()

            def on_request(req):
                if armed["value"] and _lb1817_looks_like_media_url(req.url):
                    add_url(req.url, "request-after-click")

            def on_response(res):
                if not armed["value"]:
                    return
                try:
                    url = res.url
                    if _lb1817_looks_like_media_url(url):
                        add_url(url, "response-after-click")
                    if "switcher.cdn.svt.se/resolve/" in url or "api.svt.se/ditto/api/v3/manifest" in url:
                        try:
                            body = res.text()
                            for m in re.finditer(r'https?:\\?/\\?/[^"\'<>\\\\ ]+', body):
                                add_url(m.group(0).replace("\\/", "/"), "response-body-after-click")
                        except Exception:
                            pass
                except Exception:
                    pass

            page.on("request", on_request)
            page.on("response", on_response)

            logger.info(f"[{job_id}] SVT 1.8.17: öppnar livepost-sida")
            try:
                page.goto(article_url, wait_until="domcontentloaded", timeout=20000)
            except Exception as e:
                logger.info(f"[{job_id}] SVT 1.8.17: goto warning: {e}")

            page.wait_for_timeout(2500)
            _lb1817_accept_or_remove_consent(page, job_id)

            html = page.content()
            terms = _lb1817_extract_title_terms_from_html(html, inlagg_id)
            logger.info(f"[{job_id}] SVT 1.8.17: title/near-termer={terms}")

            if not terms:
                logger.info(f"[{job_id}] SVT 1.8.17: hittade ingen rubrik efter exakt inlägg-id")
                ctx.close()
                browser.close()
                return []

            ranking = page.evaluate(
                """(terms) => {
                    const norm = (s) => String(s || '')
                        .replace(/\\s+/g, ' ')
                        .replace(/[–—-]/g, ' ')
                        .trim();

                    const tokens = (s) => {
                        const stop = new Set([
                            'spela','rapport','video','sek','min','och','att','det','som',
                            'för','från','till','med','den','detta','där','här','har',
                            'hur','vad','när','senaste','nytt','usa','politik','visa',
                            'länkar','under','meny','öppna','huvudmeny','snabbmeny'
                        ]);
                        return norm(s).toLowerCase()
                            .match(/[a-zåäö0-9]{3,}/g)
                            ?.filter(w => !stop.has(w)) || [];
                    };

                    const termTokens = new Set();
                    for (const t of terms) {
                        for (const tok of tokens(t)) termTokens.add(tok);
                    }

                    const textEls = Array.from(document.querySelectorAll(
                        'h1,h2,h3,h4,p,span,em,strong,[aria-label]'
                    ));

                    const anchors = [];
                    for (const el of textEls) {
                        const raw = el.getAttribute('aria-label') || el.innerText || el.textContent || '';
                        const txt = norm(raw);
                        if (!txt) continue;

                        const toks = tokens(txt);
                        const overlap = toks.filter(t => termTokens.has(t));
                        const exactHit = terms.some(term => txt.includes(norm(term)) || norm(term).includes(txt));

                        if (overlap.length >= 2 || exactHit) {
                            const r = el.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) {
                                anchors.push({
                                    text: txt.slice(0, 180),
                                    x: r.x,
                                    y: r.y,
                                    top: r.top,
                                    bottom: r.bottom,
                                    cx: r.x + r.width / 2,
                                    cy: r.y + r.height / 2,
                                    overlap,
                                });
                            }
                        }
                    }

                    const buttons = Array.from(document.querySelectorAll(
                        'button[data-testid="play-pause-button"], button[data-rt="video-player-splash-play"], button[aria-label*="Spela"]'
                    ));

                    const rows = buttons.map((btn, index) => {
                        const label = norm(btn.getAttribute('aria-label') || btn.innerText || btn.textContent || '');
                        const br = btn.getBoundingClientRect();
                        const btoks = tokens(label);
                        const labelOverlap = btoks.filter(t => termTokens.has(t));

                        let bestDistance = 999999;
                        let bestAnchor = null;

                        for (const a of anchors) {
                            const dx = Math.abs((br.x + br.width / 2) - a.cx);
                            let dy = 0;

                            if (br.bottom < a.top) {
                                dy = a.top - br.bottom;       // button above text, normal SVT case
                            } else if (a.bottom < br.top) {
                                dy = br.top - a.bottom;       // button below text
                            } else {
                                dy = 0;                       // overlapping/same block
                            }

                            const dist = dy + dx * 0.35;
                            if (dist < bestDistance) {
                                bestDistance = dist;
                                bestAnchor = a;
                            }
                        }

                        let score = 0;
                        score += labelOverlap.length * 5000;
                        if (bestDistance < 900) score += Math.max(0, 3000 - bestDistance * 3);
                        if (labelOverlap.length >= 2) score += 4000;
                        if (label.toLowerCase().includes('trump')) score += 1000;

                        return {
                            index,
                            score: Math.round(score),
                            label,
                            y: Math.round(br.y),
                            bestDistance: Math.round(bestDistance),
                            labelOverlap,
                            nearestText: bestAnchor ? bestAnchor.text : '',
                        };
                    }).filter(r => r.score > 0)
                      .sort((a, b) => b.score - a.score);

                    return rows;
                }""",
                terms,
            )

            logger.info(f"[{job_id}] SVT 1.8.17: NEAR-knapp-ranking topp={ranking[:5] if ranking else []}")

            if not ranking:
                logger.info(f"[{job_id}] SVT 1.8.17: ingen play-knapp matchade rubrik/NEAR")
                ctx.close()
                browser.close()
                return []

            best = ranking[0]
            best_index = int(best["index"])
            selector = 'button[data-testid="play-pause-button"], button[data-rt="video-player-splash-play"], button[aria-label*="Spela"]'
            target = page.locator(selector).nth(best_index)

            try:
                target.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass

            _lb1817_accept_or_remove_consent(page, job_id)

            armed["value"] = True

            clicked = False
            try:
                target.click(timeout=3500)
                clicked = True
                logger.info(f"[{job_id}] SVT 1.8.17: normal klick OK label={best.get('label')}")
            except Exception as e:
                logger.info(f"[{job_id}] SVT 1.8.17: normal klick misslyckades, provar force: {e}")
                try:
                    target.click(timeout=3500, force=True)
                    clicked = True
                    logger.info(f"[{job_id}] SVT 1.8.17: force-klick OK label={best.get('label')}")
                except Exception as e2:
                    logger.info(f"[{job_id}] SVT 1.8.17: force-klick misslyckades, provar JS click: {e2}")
                    try:
                        page.evaluate(
                            """(idx) => {
                                const buttons = Array.from(document.querySelectorAll(
                                    'button[data-testid="play-pause-button"], button[data-rt="video-player-splash-play"], button[aria-label*="Spela"]'
                                ));
                                if (buttons[idx]) buttons[idx].click();
                            }""",
                            best_index,
                        )
                        clicked = True
                        logger.info(f"[{job_id}] SVT 1.8.17: JS-klick OK label={best.get('label')}")
                    except Exception as e3:
                        logger.info(f"[{job_id}] SVT 1.8.17: JS-klick misslyckades: {e3}")

            if clicked:
                page.wait_for_timeout(8000)

            # Sista säkra fallback: läs video[src] nära vald knapp, inte globalt.
            try:
                near_videos = page.evaluate(
                    """(idx) => {
                        const buttons = Array.from(document.querySelectorAll(
                            'button[data-testid="play-pause-button"], button[data-rt="video-player-splash-play"], button[aria-label*="Spela"]'
                        ));
                        const btn = buttons[idx];
                        if (!btn) return [];

                        const br = btn.getBoundingClientRect();
                        const bcx = br.x + br.width / 2;
                        const bcy = br.y + br.height / 2;

                        return Array.from(document.querySelectorAll('video')).map(v => {
                            const r = v.getBoundingClientRect();
                            const src = v.currentSrc || v.src || v.getAttribute('src') || '';
                            const cx = r.x + r.width / 2;
                            const cy = r.y + r.height / 2;
                            const dist = Math.abs(cx - bcx) * 0.35 + Math.abs(cy - bcy);
                            return {src, dist: Math.round(dist)};
                        }).filter(v => v.src)
                          .sort((a, b) => a.dist - b.dist)
                          .slice(0, 3);
                    }""",
                    best_index,
                )
                logger.info(f"[{job_id}] SVT 1.8.17: video[src] nära vald knapp={near_videos}")
                for item in near_videos or []:
                    if int(item.get("dist", 999999)) < 1200:
                        add_url(item.get("src", ""), "near-video-src")
            except Exception as e:
                logger.info(f"[{job_id}] SVT 1.8.17: kunde inte läsa near video[src]: {e}")

            ctx.close()
            browser.close()

    except Exception as e:
        logger.info(f"[{job_id}] SVT 1.8.17: resolver misslyckades: {e}")

    if found:
        logger.info(f"[{job_id}] SVT 1.8.17: slutkandidat={found[0]} totalt={len(found)}")
    else:
        logger.info(f"[{job_id}] SVT 1.8.17: ingen media fångad via titel/NEAR")

    return found


def _resolve_manifests_via_playwright(job_id: str, article_url: str) -> List[str]:
    from urllib.parse import parse_qs, urlparse

    try:
        parsed = urlparse(article_url)
        inlagg_id = parse_qs(parsed.query).get("inlagg", [""])[0]

        if inlagg_id and "svt.se" in parsed.netloc.lower():
            logger.info(f"[{job_id}] SVT 1.8.17: använder title/NEAR livepost-resolver för inlägg {inlagg_id}")
            urls = _lb1817_resolve_svt_livepost_by_title_near(job_id, article_url, inlagg_id)
            if urls:
                return urls

            logger.info(
                f"[{job_id}] SVT 1.8.17: ingen video kunde kopplas till exakt inlägg. "
                "Avbryter utan global fallback."
            )
            return []

    except Exception as e:
        logger.info(f"[{job_id}] SVT 1.8.17: wrapper misslyckades: {e}")

    return _lb1817_previous_resolve_manifests_via_playwright(job_id, article_url)

# === End LjudBuster 1.8.17-dev override ===

