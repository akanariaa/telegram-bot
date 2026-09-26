"""
YouTube archiving module for the Telegram bot.

Downloads videos/thumbnails from YouTube (single videos, playlists, channels),
merges audio+video with ffmpeg, uploads to Backblaze B2, and tracks completions
in SQLite to avoid re-downloads.
"""

import asyncio
import logging
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
import http.client
import urllib.error
import urllib.request
from botocore.client import Config
from botocore.exceptions import ClientError, NoCredentialsError
from pytubefix import Channel, Playlist, YouTube
from pytubefix.exceptions import BotDetection, PytubeFixError

from config.settings import (
    B2_APPLICATION_KEY,
    B2_BUCKET_NAME,
    B2_ENDPOINT_URL,
    B2_KEY_ID,
    DOCKER_PROXY_CONTAINER,
    DOWNLOAD_BASE_DIR,
    MAX_WORKERS,
    PREFERRED_RESOLUTIONS,
    PROXY_HOST_PORT,
    USE_PROXY,
    get_proxy_dict,
)
from bot.services.database import get_yt_completed_ids, log_yt_completion

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retry / timing constants
# ---------------------------------------------------------------------------
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 5
MAX_DOWNLOAD_RETRIES = 4


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize_filename(name: str) -> str:
    """Remove characters that are illegal in file names."""
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = name.replace("..", "")
    return name


def _delete_local_file(path: str, max_retries: int = 3) -> bool:
    """Delete a local file with retries."""
    if not path or not os.path.exists(path):
        return True
    for attempt in range(max_retries):
        try:
            os.remove(path)
            return True
        except OSError as exc:
            logger.warning("File delete error (attempt %d/%d): %s – %s",
                           attempt + 1, max_retries, path, exc)
            if attempt < max_retries - 1:
                time.sleep(1)
    return False


# ---------------------------------------------------------------------------
# B2 upload
# ---------------------------------------------------------------------------

def _upload_to_b2(local_path: str, b2_key: str, max_retries: int = 3) -> bool:
    """Upload a local file to Backblaze B2 (S3-compatible)."""
    if not os.path.exists(local_path):
        logger.error("B2 upload skipped – file missing: %s", local_path)
        return False

    file_size = os.path.getsize(local_path)
    cfg = Config(signature_version="s3v4")
    client = boto3.client(
        "s3",
        endpoint_url=B2_ENDPOINT_URL,
        aws_access_key_id=B2_KEY_ID,
        aws_secret_access_key=B2_APPLICATION_KEY,
        config=cfg,
    )

    for attempt in range(max_retries):
        try:
            logger.info("B2 upload start (attempt %d/%d): %s (%.1f MB)",
                        attempt + 1, max_retries, b2_key, file_size / 1024 / 1024)
            client.upload_file(local_path, B2_BUCKET_NAME, b2_key)
            logger.info("B2 upload success: %s", b2_key)
            return True
        except (NoCredentialsError, ClientError) as exc:
            logger.error("B2 upload auth/client error (%s): %s", b2_key, exc)
            if isinstance(exc, NoCredentialsError):
                return False
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("InvalidAccessKeyId", "SignatureDoesNotMatch"):
                return False
        except (ConnectionError, http.client.HTTPException,
                urllib.error.URLError, OSError) as exc:
            logger.warning("B2 upload network error (attempt %d/%d): %s",
                           attempt + 1, max_retries, exc)
        except Exception as exc:
            logger.error("B2 upload unexpected error (%s): %s – %s",
                         b2_key, type(exc).__name__, exc)

        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)

    logger.error("B2 upload final failure: %s", b2_key)
    return False


# ---------------------------------------------------------------------------
# Thumbnail download
# ---------------------------------------------------------------------------

def _download_thumbnail(yt: YouTube, dest_path: str, proxies: dict | None,
                        max_retries: int = 3) -> bool:
    """Download the video thumbnail to *dest_path*."""
    for attempt in range(max_retries):
        try:
            thumb_url = yt.thumbnail_url
            if not thumb_url:
                logger.warning("No thumbnail URL for %s", yt.title)
                return False

            if proxies:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler(proxies)
                )
                with opener.open(thumb_url) as resp, open(dest_path, "wb") as f:
                    f.write(resp.read())
            else:
                urllib.request.urlretrieve(thumb_url, dest_path)

            if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
                return True
            raise RuntimeError("Thumbnail file empty or missing after download")

        except Exception as exc:
            logger.warning("Thumbnail download error (attempt %d/%d): %s",
                           attempt + 1, max_retries, exc)
            if attempt < max_retries - 1:
                time.sleep(RETRY_DELAY_SECONDS)

    logger.error("Thumbnail download final failure: %s", yt.title)
    return False


# ---------------------------------------------------------------------------
# Resolve URL → list of YouTube objects
# ---------------------------------------------------------------------------

def _resolve_videos(url: str, proxies: dict | None) -> list:
    """Return a list of YouTube objects for *url* (single / playlist / channel)."""
    for attempt in range(MAX_RETRIES):
        try:
            if "playlist" in url:
                return list(Playlist(url, proxies=proxies, client="ANDROID_VR").videos)

            if any(s in url for s in ("/c/", "/channel/", "/user/", "/@")):
                c = Channel(url, proxies=proxies, client="ANDROID_VR")
                if not c.channel_id:
                    logger.warning("Cannot resolve channel ID for %s", url)
                    return []
                uploads_id = "UU" + c.channel_id[2:]
                uploads_url = f"https://www.youtube.com/playlist?list={uploads_id}"
                logger.info("Channel detected – fetching uploads playlist: %s", uploads_url)
                return list(Playlist(uploads_url, proxies=proxies, client="ANDROID_VR").videos)

            return [YouTube(url, proxies=proxies, client="ANDROID_VR")]

        except BotDetection:
            logger.warning("Bot detection on resolve (attempt %d/%d): %s",
                           attempt + 1, MAX_RETRIES, url)
        except PytubeFixError as exc:
            msg = str(exc).lower()
            if any(kw in msg for kw in ("unavailable", "private video", "removed")):
                logger.warning("Video unavailable/private/removed: %s", url)
                return []
            logger.warning("PytubeFix error on resolve (attempt %d/%d): %s",
                           attempt + 1, MAX_RETRIES, exc)
        except Exception as exc:
            logger.warning("Resolve error (attempt %d/%d): %s – %s",
                           attempt + 1, MAX_RETRIES, type(exc).__name__, exc)

        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))

    return []


# ---------------------------------------------------------------------------
# Fetch metadata for a single video object
# ---------------------------------------------------------------------------

def _get_metadata(video_obj, proxies: dict | None) -> dict | None:
    """Return ``{id, title, author, url}`` or *None* on failure."""
    for attempt in range(MAX_RETRIES):
        try:
            _ = video_obj.title  # force metadata load
            return {
                "id": video_obj.video_id,
                "title": _sanitize_filename(video_obj.title),
                "author": video_obj.author,
                "url": video_obj.watch_url,
            }
        except BotDetection:
            logger.warning("Bot detection on metadata (attempt %d/%d)",
                           attempt + 1, MAX_RETRIES)
        except PytubeFixError as exc:
            msg = str(exc).lower()
            if any(kw in msg for kw in ("unavailable", "private video", "removed")):
                logger.warning("Video unavailable – skipping metadata: %s",
                               video_obj.watch_url)
                return None
            logger.warning("PytubeFix metadata error (attempt %d/%d): %s",
                           attempt + 1, MAX_RETRIES, exc)
        except Exception as exc:
            logger.warning("Metadata error (attempt %d/%d): %s – %s",
                           attempt + 1, MAX_RETRIES, type(exc).__name__, exc)

        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))

    logger.error("Metadata final failure: %s", video_obj.watch_url)
    return None


# ---------------------------------------------------------------------------
# Process a single video item (download, merge, upload)
# ---------------------------------------------------------------------------

def _process_video(
    meta: dict,
    proxies: dict | None,
    mode: str,
    completed_videos: set,
    completed_thumbs: set,
) -> str | None:
    """Process one video entry. Returns an error string or *None* on success."""
    vid = meta["id"]
    title = meta["title"]
    author = meta["author"]
    url = meta["url"]

    do_video = mode in ("video", "both") and vid not in completed_videos
    do_thumb = mode in ("thumbnail", "both") and vid not in completed_thumbs

    if not do_video and not do_thumb:
        logger.info("Skipping already completed: %s (%s)", title, vid)
        return None

    logger.info("Processing: %s (video=%s, thumb=%s)", title, do_video, do_thumb)

    video_tmp = audio_tmp = merged_path = thumb_path = None

    try:
        # --- Create YouTube object with retries ---
        yt = None
        for attempt in range(MAX_RETRIES):
            try:
                yt = YouTube(url, proxies=proxies, client="ANDROID_VR")
                yt.check_availability()
                break
            except BotDetection:
                logger.warning("Bot detection creating YT object (attempt %d/%d): %s",
                               attempt + 1, MAX_RETRIES, title)
            except PytubeFixError as exc:
                msg = str(exc).lower()
                if any(kw in msg for kw in ("unavailable", "private video", "removed")):
                    return f"Skipped (unavailable): {title}"
                logger.warning("PytubeFix YT object error (attempt %d/%d): %s",
                               attempt + 1, MAX_RETRIES, exc)
            except Exception as exc:
                logger.warning("YT object error (attempt %d/%d): %s – %s",
                               attempt + 1, MAX_RETRIES, type(exc).__name__, exc)

            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))

        if yt is None:
            return f"Failed to create YouTube object: {title}"

        # --- Prepare paths ---
        safe_author = _sanitize_filename(author)
        output_dir = os.path.join(DOWNLOAD_BASE_DIR, f"[Channel] {safe_author}")
        os.makedirs(output_dir, exist_ok=True)
        base = f"[{safe_author}] {title} ({vid})"

        # --- Thumbnail ---
        if do_thumb:
            thumb_path = os.path.join(output_dir, f"{base}.jpg")
            if _download_thumbnail(yt, thumb_path, proxies):
                b2_key = os.path.relpath(thumb_path, DOWNLOAD_BASE_DIR).replace(os.sep, "/")
                if _upload_to_b2(thumb_path, b2_key):
                    log_yt_completion(vid, "thumbnail", title, author)
                else:
                    logger.error("Thumbnail B2 upload failed: %s", title)
            else:
                logger.error("Thumbnail download failed: %s", title)
            _delete_local_file(thumb_path)
            thumb_path = None

        # --- Video ---
        if do_video:
            video_tmp = os.path.join(output_dir, f"{vid}_video.mp4")
            audio_tmp = os.path.join(output_dir, f"{vid}_audio.mp4")
            merged_path = os.path.join(output_dir, f"{base}.mp4")

            try:
                # Select video stream by preferred resolution
                video_stream = None
                for res in PREFERRED_RESOLUTIONS:
                    stream = yt.streams.filter(
                        adaptive=True, file_extension="mp4", resolution=res
                    ).first()
                    if stream:
                        video_stream = stream
                        logger.info("Selected %s stream for %s", res, title)
                        break

                if not video_stream:
                    video_stream = (
                        yt.streams.filter(adaptive=True, file_extension="mp4")
                        .order_by("resolution")
                        .desc()
                        .first()
                    )
                    if video_stream:
                        logger.info("Fallback to highest resolution %s for %s",
                                    video_stream.resolution, title)

                if not video_stream:
                    return f"No video stream available: {title}"

                audio_stream = yt.streams.get_audio_only()
                if not audio_stream:
                    return f"No audio stream available: {title}"

                # Download
                logger.info("Downloading video: %s", title)
                video_stream.download(
                    output_path=output_dir,
                    filename=os.path.basename(video_tmp),
                    max_retries=MAX_DOWNLOAD_RETRIES,
                )
                logger.info("Downloading audio: %s", title)
                audio_stream.download(
                    output_path=output_dir,
                    filename=os.path.basename(audio_tmp),
                    max_retries=MAX_DOWNLOAD_RETRIES,
                )

                # Merge with ffmpeg
                logger.info("Merging with ffmpeg: %s", title)
                result = subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-i", video_tmp,
                        "-i", audio_tmp,
                        "-c", "copy",
                        "-loglevel", "error",
                        merged_path,
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                )
                if result.returncode != 0:
                    return f"ffmpeg merge failed for {title}: {result.stderr}"

                # Clean up temp streams
                _delete_local_file(video_tmp); video_tmp = None
                _delete_local_file(audio_tmp); audio_tmp = None

                # Upload merged video
                b2_key = os.path.relpath(merged_path, DOWNLOAD_BASE_DIR).replace(os.sep, "/")
                if _upload_to_b2(merged_path, b2_key):
                    log_yt_completion(vid, "video", title, author)
                else:
                    return f"B2 upload failed for video: {title}"

            finally:
                _delete_local_file(video_tmp)
                _delete_local_file(audio_tmp)
                _delete_local_file(merged_path)

        return None  # success

    except Exception as exc:
        logger.error("Unexpected error processing %s: %s – %s",
                     title, type(exc).__name__, exc)
        return f"Error processing {title}: {exc}"
    finally:
        # Safety net cleanup
        for p in (video_tmp, audio_tmp, merged_path, thumb_path):
            _delete_local_file(p)


# ---------------------------------------------------------------------------
# Main async entry point
# ---------------------------------------------------------------------------

async def archive_youtube(url: str, mode: str = "both") -> str:
    """
    Archive a YouTube URL (single video, playlist, or channel).

    Parameters
    ----------
    url : str
        A YouTube video, playlist, or channel URL.
    mode : str
        ``"video"`` – download video only,
        ``"thumbnail"`` – download thumbnail only,
        ``"both"`` – download both (default).

    Returns
    -------
    str
        A human-readable status message summarising the results.
    """
    return await asyncio.to_thread(_archive_youtube_sync, url, mode)


def _archive_youtube_sync(url: str, mode: str) -> str:
    """Synchronous implementation of the archive workflow."""

    proxies = get_proxy_dict()

    # ------------------------------------------------------------------
    # 1. Resolve URL → video objects
    # ------------------------------------------------------------------
    logger.info("Resolving videos from URL: %s", url)
    video_objects = _resolve_videos(url, proxies)
    if not video_objects:
        return f"❌ No videos found at: {url}"

    logger.info("Found %d video(s) from URL", len(video_objects))

    # ------------------------------------------------------------------
    # 2. Fetch metadata in parallel
    # ------------------------------------------------------------------
    metadata_list: list[dict] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        future_map = {
            pool.submit(_get_metadata, vobj, proxies): vobj
            for vobj in video_objects
        }
        for future in as_completed(future_map):
            try:
                meta = future.result()
                if meta:
                    metadata_list.append(meta)
            except Exception as exc:
                logger.error("Metadata fetch error: %s", exc)

    if not metadata_list:
        return f"❌ Failed to load metadata for any video at: {url}"

    logger.info("Loaded metadata for %d / %d videos",
                len(metadata_list), len(video_objects))

    # ------------------------------------------------------------------
    # 3. Filter out already-completed items
    # ------------------------------------------------------------------
    completed_videos, completed_thumbs = get_yt_completed_ids()

    pending: list[dict] = []
    for m in metadata_list:
        vid = m["id"]
        v_done = vid in completed_videos
        t_done = vid in completed_thumbs
        if mode == "video" and v_done:
            continue
        if mode == "thumbnail" and t_done:
            continue
        if mode == "both" and v_done and t_done:
            continue
        pending.append(m)

    skipped = len(metadata_list) - len(pending)
    if not pending:
        return (
            f"✅ All {len(metadata_list)} item(s) already archived "
            f"(mode={mode}). Nothing to do."
        )

    logger.info("Processing %d new item(s), %d already completed",
                len(pending), skipped)

    # ------------------------------------------------------------------
    # 4. Process videos in parallel
    # ------------------------------------------------------------------
    errors: list[str] = []
    succeeded = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        future_map = {
            pool.submit(
                _process_video, meta, proxies, mode,
                completed_videos, completed_thumbs,
            ): meta
            for meta in pending
        }
        for future in as_completed(future_map):
            meta = future_map[future]
            try:
                err = future.result()
                if err:
                    errors.append(err)
                else:
                    succeeded += 1
            except Exception as exc:
                errors.append(f"Unhandled error for {meta.get('title', '?')}: {exc}")

    # ------------------------------------------------------------------
    # 5. Build summary
    # ------------------------------------------------------------------
    total = len(metadata_list)
    lines = [
        f"📦 Archive complete (mode={mode})",
        f"   Total videos: {total}",
        f"   Already done: {skipped}",
        f"   Succeeded:    {succeeded}",
        f"   Failed:       {len(errors)}",
    ]
    if errors:
        lines.append("")
        lines.append("Errors:")
        for e in errors[:20]:  # cap to avoid huge messages
            lines.append(f"  • {e}")
        if len(errors) > 20:
            lines.append(f"  … and {len(errors) - 20} more")

    return "\n".join(lines)