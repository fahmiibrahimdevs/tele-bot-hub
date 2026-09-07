import os
import re
import shutil

STORAGE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "storage")
os.makedirs(STORAGE_DIR, exist_ok=True)


def sanitize_filename(name: str, max_length: int = 60, strip_ext: str = "") -> str:
    # Remove invalid characters for files
    clean = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    # Replace multiple spaces with single space
    clean = re.sub(r'\s+', ' ', clean)
    # Strip common extension if typed by user (e.g. .pdf, .mp3, .mp4)
    if strip_ext:
        ext_pattern = rf'\.{strip_ext}$'
        clean = re.sub(ext_pattern, '', clean, flags=re.IGNORECASE).strip()
    if not clean:
        clean = "output"
    return clean[:max_length]


def format_duration(seconds: int | float | None) -> str:
    if not seconds:
        return "N/A"
    seconds = int(seconds)
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def format_bytes(size: int | float | None) -> str:
    if not size:
        return "N/A"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def cleanup_user_dir(user_dir: str):
    if os.path.exists(user_dir):
        try:
            shutil.rmtree(user_dir)
        except Exception:
            pass


def clean_and_validate_media_url(raw_url: str) -> tuple[str, bool, str]:
    """
    Membersihkan URL media (YouTube, TikTok, IG, FB, Twitter/X, SoundCloud, dll)
    dari parameter playlist, radio mix, tracking query, dan parameter sampah lainnya.
    Returns:
        (clean_url: str, is_pure_playlist: bool, error_msg: str)
    """
    from urllib.parse import urlparse, parse_qs, urlunparse
    url = raw_url.strip()
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        path = parsed.path
        qs = parse_qs(parsed.query)

        # 1. Kasus YouTube
        if any(d in domain for d in ("youtube.com", "youtu.be", "m.youtube.com")):
            # Deteksi playlist murni tanpa video
            if "/playlist" in path and "v" not in qs:
                return (
                    url,
                    True,
                    "⚠️ <b>Tautan Berupa Playlist!</b>\n\n"
                    "Bot ini hanya memproses <b>1 video/lagu</b> per pesan.\n"
                    "Silakan buka salah satu video di playlist tersebut, lalu salin dan kirimkan tautan videonya ke bot."
                )

            # Jika format /watch?v=VIDEO_ID
            if "v" in qs and qs["v"]:
                video_id = qs["v"][0]
                clean_url = f"https://www.youtube.com/watch?v={video_id}"
                return clean_url, False, ""

            # Jika format youtu.be/VIDEO_ID
            if "youtu.be" in domain and path.strip("/"):
                video_id = path.strip("/").split("/")[0]
                if video_id:
                    clean_url = f"https://www.youtube.com/watch?v={video_id}"
                    return clean_url, False, ""

            # Jika format /shorts/VIDEO_ID
            if "/shorts/" in path:
                parts = path.strip("/").split("/")
                if len(parts) >= 2 and parts[0] == "shorts":
                    video_id = parts[1]
                    clean_url = f"https://www.youtube.com/watch?v={video_id}"
                    return clean_url, False, ""

        # 2. Platform lain (TikTok, Instagram, Twitter/X, Facebook, SoundCloud)
        # Buang parameter pelacak (tracking params)
        tracking_params = {"utm_source", "utm_medium", "utm_campaign", "igsh", "si", "feature", "app", "share_id"}
        cleaned_qs = {k: v for k, v in qs.items() if k.lower() not in tracking_params}

        # Hapus 'list' jika bukan SoundCloud
        if "list" in cleaned_qs and not any(d in domain for d in ("soundcloud.com",)):
            del cleaned_qs["list"]

        new_query = "&".join(f"{k}={v[0]}" for k, v in cleaned_qs.items())
        clean_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, ""))
        return clean_url, False, ""

    except Exception:
        return url, False, ""


def fetch_content_length(url: str, timeout: float = 3.0) -> int | None:
    """
    Melakukan HTTP HEAD request cepat untuk membaca Content-Length dari direct stream URL (Facebook, IG, TikTok, dll).
    """
    if not url or not url.startswith("http"):
        return None
    import urllib.request
    try:
        req = urllib.request.Request(url, method="HEAD")
        req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            cl = resp.headers.get("Content-Length")
            if cl and cl.isdigit():
                return int(cl)
    except Exception:
        pass
    return None


def infer_effective_height(fmt: dict) -> int | None:
    """
    Menentukan resolusi vertikal / kategori resolusi (360p, 720p, 1080p) dari format video.
    Mendukung video vertikal (Reels, TikTok, Shorts), horizontal, dan format Facebook (sd, hd).
    """
    w = fmt.get("width")
    h = fmt.get("height")
    if w and h and isinstance(w, (int, float)) and isinstance(h, (int, float)) and w > 0 and h > 0:
        return int(min(w, h))
    if h and isinstance(h, (int, float)) and h > 0:
        return int(h)
    if w and isinstance(w, (int, float)) and w > 0:
        return int(w)

    res_str = fmt.get("resolution") or ""
    m = re.search(r'(\d+)x(\d+)', res_str)
    if m:
        return min(int(m.group(1)), int(m.group(2)))
    m = re.search(r'(\d+)p', res_str, re.IGNORECASE)
    if m:
        return int(m.group(1))

    text = f"{fmt.get('format_id', '')} {fmt.get('format_note', '')} {fmt.get('format', '')}".lower()
    if "1080" in text or "fhd" in text:
        return 1080
    if "720" in text or "hd" in text:
        return 720
    if "480" in text:
        return 480
    if "360" in text or "sd" in text:
        return 360
    if "240" in text:
        return 240
    return None


def parse_duration_seconds(val) -> float:
    """
    Mengonversi berbagai format durasi (float, int, string '01:23', '01:10:20') menjadi detik.
    """
    if not val:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        val = val.strip()
        parts = val.split(":")
        try:
            if len(parts) == 3:
                return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
            elif len(parts) == 2:
                return float(parts[0]) * 60 + float(parts[1])
            else:
                return float(val)
        except Exception:
            return 0.0
    return 0.0

