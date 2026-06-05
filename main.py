import asyncio
import json
import os
import shutil
import subprocess
import tempfile

import httpx
import yt_dlp
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse


def find_ffmpeg():
    path = shutil.which("ffmpeg")
    if path:
        print(f"[ffmpeg] found via which: {path}")
        return os.path.dirname(path)
    for p in ["/usr/local/bin", "/usr/bin", "/bin",
              "/nix/var/nix/profiles/default/bin",
              "/root/.nix-profile/bin"]:
        if os.path.exists(os.path.join(p, "ffmpeg")):
            print(f"[ffmpeg] found at: {p}")
            return p
    for search_dir in ["/nix", "/usr"]:
        try:
            result = subprocess.run(
                ["find", search_dir, "-name", "ffmpeg", "-type", "f"],
                capture_output=True, text=True, timeout=10,
            )
            lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
            if lines:
                print(f"[ffmpeg] found via find: {lines[0]}")
                return os.path.dirname(lines[0])
        except Exception as e:
            print(f"[ffmpeg] find error in {search_dir}: {e}")
    print("[ffmpeg] NOT FOUND anywhere!")
    return None


FFMPEG_PATH = find_ffmpeg()
print(f"[startup] FFMPEG_PATH = {FFMPEG_PATH}")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SC_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://soundcloud.com/",
}


def base_ydl_opts():
    return {
        "quiet": True,
        "no_warnings": True,
        "http_headers": SC_HEADERS,
    }


def pick_best_http_url(info: dict):
    formats = info.get("formats") or []
    candidates = []
    for f in formats:
        url = f.get("url", "")
        proto = f.get("protocol", "")
        if "m3u8" in url or "mpd" in url:
            continue
        if proto in ("m3u8", "m3u8_native", "dash", "http_dash_segments"):
            continue
        if f.get("acodec") in (None, "none"):
            continue
        candidates.append(f)
    if candidates:
        candidates.sort(key=lambda f: f.get("abr") or f.get("tbr") or 0, reverse=True)
        best = candidates[0]
        return best.get("url"), best.get("ext", "mp3"), best.get("abr", 0)
    return None, None, None


def pick_stream_url(info: dict) -> str:
    formats = info.get("formats") or []
    audio_only = [
        f for f in formats
        if f.get("acodec") not in (None, "none")
        and f.get("vcodec") in (None, "none")
        and f.get("url")
    ]
    if audio_only:
        audio_only.sort(key=lambda f: f.get("abr") or f.get("tbr") or 0)
        return audio_only[-1]["url"]
    with_audio = [
        f for f in formats
        if f.get("acodec") not in (None, "none") and f.get("url")
    ]
    if with_audio:
        with_audio.sort(key=lambda f: f.get("abr") or f.get("tbr") or 0)
        return with_audio[-1]["url"]
    return info.get("url")


def is_hls_url(url: str) -> bool:
    return "m3u8" in url or "mpd" in url


# ─────────────────────────────────────────────
#  HEALTH CHECK
# ─────────────────────────────────────────────
@app.get("/")
async def health():
    return {"status": "ok", "ffmpeg": FFMPEG_PATH is not None}


# ─────────────────────────────────────────────
#  SEARCH
# ─────────────────────────────────────────────
@app.get("/search")
async def search(q: str = Query(...), limit: int = 10):
    try:
        opts = base_ydl_opts()
        opts["skip_download"] = True
        opts["extract_flat"] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            results = ydl.extract_info(f"scsearch{limit}:{q}", download=False)
            items = []
            for entry in results.get("entries") or []:
                if not entry:
                    continue
                items.append({
                    "id": entry.get("id"),
                    "title": entry.get("title"),
                    "channel": entry.get("uploader") or entry.get("channel"),
                    "duration": entry.get("duration"),
                    "view_count": entry.get("view_count"),
                    "thumbnail": entry.get("thumbnail") or "",
                    "url": entry.get("url") or entry.get("webpage_url") or "",
                })
            return JSONResponse({"results": items, "count": len(items)})
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Pencarian gagal: {str(e)}")


# ─────────────────────────────────────────────
#  STREAM URL
# ─────────────────────────────────────────────
@app.get("/stream-url")
async def get_stream_url(url: str = Query(...)):
    if "soundcloud.com" not in url:
        raise HTTPException(status_code=400, detail="URL SoundCloud tidak valid")
    try:
        opts = base_ydl_opts()
        opts["skip_download"] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        http_url, ext, abr = pick_best_http_url(info)
        if http_url:
            return JSONResponse({
                "stream_url": http_url,
                "is_hls": False,
                "ext": ext,
                "abr": abr,
                "title": info.get("title"),
                "channel": info.get("uploader"),
                "duration": info.get("duration"),
                "thumbnail": info.get("thumbnail"),
            })

        stream_url = pick_stream_url(info)
        if not stream_url:
            raise HTTPException(status_code=500, detail="Stream URL tidak ditemukan")

        return JSONResponse({
            "stream_url": stream_url,
            "is_hls": is_hls_url(stream_url),
            "title": info.get("title"),
            "channel": info.get("uploader"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
        })
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal mengambil stream URL: {str(e)}")


# ─────────────────────────────────────────────
#  PROXY AUDIO
# ─────────────────────────────────────────────
@app.get("/proxy-audio")
async def proxy_audio(url: str = Query(...)):
    if "soundcloud.com" not in url:
        raise HTTPException(status_code=400, detail="URL tidak valid")
    try:
        opts = base_ydl_opts()
        opts["skip_download"] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        http_url, ext, abr = pick_best_http_url(info)
        if http_url:
            async def stream_direct():
                async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
                    async with client.stream("GET", http_url, headers=SC_HEADERS) as r:
                        if r.status_code >= 400:
                            raise HTTPException(status_code=502, detail=f"Upstream error {r.status_code}")
                        async for chunk in r.aiter_bytes(chunk_size=65536):
                            yield chunk
            return StreamingResponse(stream_direct(), media_type="audio/mpeg",
                                     headers={"Cache-Control": "no-cache"})

        stream_url = pick_stream_url(info)
        if not stream_url:
            raise HTTPException(status_code=500, detail="Stream URL tidak ditemukan")

        hls = is_hls_url(stream_url)

        if not hls:
            async def stream_fallback():
                async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
                    async with client.stream("GET", stream_url, headers=SC_HEADERS) as r:
                        async for chunk in r.aiter_bytes(chunk_size=65536):
                            yield chunk
            return StreamingResponse(stream_fallback(), media_type="audio/mpeg")

        # HLS → download via yt-dlp ke tmpdir
        with tempfile.TemporaryDirectory() as tmpdir:
            dl_opts = base_ydl_opts()
            dl_opts["format"] = "bestaudio/best"
            dl_opts["outtmpl"] = os.path.join(tmpdir, "%(id)s.%(ext)s")
            if FFMPEG_PATH:
                dl_opts["ffmpeg_location"] = FFMPEG_PATH
                dl_opts["postprocessors"] = [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "128",
                }]
            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                dl_info = ydl.extract_info(url, download=True)

            vid_id = dl_info.get("id", "audio")
            mp3_path = None
            for ext_try in ["mp3", "m4a", "opus", "ogg", "webm"]:
                p = os.path.join(tmpdir, f"{vid_id}.{ext_try}")
                if os.path.exists(p):
                    mp3_path = p
                    break
            if not mp3_path:
                files = os.listdir(tmpdir)
                if files:
                    mp3_path = os.path.join(tmpdir, files[0])

            if not mp3_path or not os.path.exists(mp3_path):
                raise HTTPException(status_code=500, detail="File audio tidak ditemukan setelah download")

            audio_bytes = open(mp3_path, "rb").read()

        return Response(content=audio_bytes, media_type="audio/mpeg",
                        headers={"Cache-Control": "no-cache"})

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Proxy gagal: {str(e)}")


# ─────────────────────────────────────────────
#  AUDIO DOWNLOAD
# ─────────────────────────────────────────────
@app.get("/audio")
async def get_audio(url: str = Query(...)):
    if "soundcloud.com" not in url:
        raise HTTPException(status_code=400, detail="URL SoundCloud tidak valid")
    path = None
    try:
        opts = base_ydl_opts()
        opts["format"] = "bestaudio/best"
        opts["outtmpl"] = "/tmp/%(id)s.%(ext)s"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}]
        if FFMPEG_PATH:
            opts["ffmpeg_location"] = FFMPEG_PATH

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            vid_id = info.get("id", "audio")
            path = f"/tmp/{vid_id}.mp3"
            if not os.path.exists(path):
                for ext in ["mp3", "m4a", "opus", "ogg"]:
                    p = f"/tmp/{vid_id}.{ext}"
                    if os.path.exists(p):
                        path = p
                        break

        if not path or not os.path.exists(path):
            raise HTTPException(status_code=500, detail="File tidak ditemukan")

        def iterfile():
            try:
                with open(path, "rb") as f:
                    yield from f
            finally:
                if path and os.path.exists(path):
                    os.remove(path)

        title = info.get("title", "audio").replace("/", "_")
        return StreamingResponse(
            iterfile(),
            media_type="audio/mpeg",
            headers={"Content-Disposition": f'attachment; filename="{title}.mp3"'},
        )
    except HTTPException:
        raise
    except Exception as e:
        if path and os.path.exists(path):
            os.remove(path)
        raise HTTPException(status_code=500, detail=f"Gagal: {str(e)}")


# ─────────────────────────────────────────────
#  ROBLOX UPLOAD  ← lewat backend, bebas CORS
# ─────────────────────────────────────────────
@app.post("/roblox-upload")
async def roblox_upload(
    file: UploadFile = File(...),
    api_key: str = Form(...),
    creator_id: str = Form(...),
    creator_type: str = Form(...),
    asset_name: str = Form(...),
):
    request_body = {
        "assetType": "Audio",
        "displayName": asset_name[:50],
        "description": "Uploaded via Ever Dream Studio",
        "creationContext": {
            "creator": (
                {"groupId": int(creator_id)}
                if creator_type == "Group"
                else {"userId": int(creator_id)}
            )
        }
    }

    file_bytes = await file.read()
    content_type = file.content_type or "audio/mpeg"

    try:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                "https://apis.roblox.com/assets/v1/assets",
                headers={"x-api-key": api_key},
                files={
                    "request": (None, json.dumps(request_body), "application/json"),
                    "fileContent": (file.filename, file_bytes, content_type),
                }
            )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Timeout saat menghubungi Roblox API")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Gagal menghubungi Roblox API: {str(e)}")

    if not response.is_success:
        raise HTTPException(
            status_code=502,
            detail=f"Roblox error {response.status_code}: {response.text}"
        )

    return JSONResponse(response.json())


# ─────────────────────────────────────────────
#  ROBLOX POLL OPERATION
# ─────────────────────────────────────────────
@app.get("/roblox-poll")
async def roblox_poll(
    op_path: str = Query(...),
    api_key: str = Query(...),
):
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"https://apis.roblox.com/assets/v1/{op_path}",
                headers={"x-api-key": api_key}
            )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Timeout saat polling Roblox")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Poll error: {str(e)}")

    if not response.is_success:
        raise HTTPException(
            status_code=502,
            detail=f"Poll error {response.status_code}: {response.text}"
        )

    return JSONResponse(response.json())


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
