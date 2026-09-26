"""S3/B2 bucket storage module — list, inspect, and transfer files."""

import asyncio
import html as html_mod
import logging
import os
import tempfile

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError, NoCredentialsError

from config.settings import (
    B2_APPLICATION_KEY,
    B2_BUCKET_NAME,
    B2_ENDPOINT_URL,
    B2_KEY_ID,
)

logger = logging.getLogger(__name__)

MAX_TELEGRAM_FILE_SIZE = 50 * 1024 * 1024  # 50 MB Telegram bot upload limit


def _get_s3_client():
    cfg = Config(signature_version="s3v4")
    return boto3.client(
        "s3",
        endpoint_url=B2_ENDPOINT_URL,
        aws_access_key_id=B2_KEY_ID,
        aws_secret_access_key=B2_APPLICATION_KEY,
        config=cfg,
    )


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.1f} MB"
    return f"{size_bytes / 1024 / 1024 / 1024:.2f} GB"


def _list_bucket_sync(
    prefix: str = "",
    max_keys: int = 100,
    bucket: str | None = None,
) -> dict:
    """List objects in the B2 bucket. Returns a dict with keys: objects, truncated, count."""
    client = _get_s3_client()
    target = bucket or B2_BUCKET_NAME

    try:
        kwargs = {"Bucket": target, "MaxKeys": max_keys}
        if prefix:
            kwargs["Prefix"] = prefix

        resp = client.list_objects_v2(**kwargs)
        objects = []
        for obj in resp.get("Contents", []):
            objects.append({
                "key": obj["Key"],
                "size": obj["Size"],
                "size_human": _format_size(obj["Size"]),
                "last_modified": obj["LastModified"].isoformat(),
            })

        return {
            "bucket": target,
            "prefix": prefix,
            "count": len(objects),
            "truncated": resp.get("IsTruncated", False),
            "objects": objects,
        }

    except (NoCredentialsError, ClientError) as exc:
        logger.error("S3 list error: %s", exc)
        return {"error": str(exc), "bucket": target, "objects": [], "count": 0}
    except Exception as exc:
        logger.error("S3 list unexpected error: %s – %s", type(exc).__name__, exc)
        return {"error": str(exc), "bucket": target, "objects": [], "count": 0}


def _get_object_info_sync(key: str, bucket: str | None = None) -> dict:
    """Get metadata for a single object."""
    client = _get_s3_client()
    target = bucket or B2_BUCKET_NAME

    try:
        resp = client.head_object(Bucket=target, Key=key)
        return {
            "bucket": target,
            "key": key,
            "size": resp["ContentLength"],
            "size_human": _format_size(resp["ContentLength"]),
            "content_type": resp.get("ContentType", "unknown"),
            "last_modified": resp["LastModified"].isoformat(),
            "etag": resp.get("ETag", ""),
        }
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "404":
            return {"error": f"File not found: {key}"}
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _download_object_sync(
    key: str,
    dest_path: str,
    bucket: str | None = None,
) -> str | None:
    """Download an object to dest_path. Returns dest_path on success, None on failure."""
    client = _get_s3_client()
    target = bucket or B2_BUCKET_NAME

    try:
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        client.download_file(target, key, dest_path)
        if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
            return dest_path
        return None
    except Exception as exc:
        logger.error("S3 download error (%s): %s – %s", key, type(exc).__name__, exc)
        return None


def _generate_presigned_url_sync(
    key: str,
    expires_in: int = 3600,
    bucket: str | None = None,
) -> str | None:
    """Generate a presigned download URL."""
    client = _get_s3_client()
    target = bucket or B2_BUCKET_NAME

    try:
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": target, "Key": key},
            ExpiresIn=expires_in,
        )
    except Exception as exc:
        logger.error("Presigned URL error (%s): %s", key, exc)
        return None


# ---------------------------------------------------------------------------
# Async wrappers
# ---------------------------------------------------------------------------

async def list_bucket(
    prefix: str = "",
    max_keys: int = 100,
    bucket: str | None = None,
) -> str:
    """List objects in the bucket. Returns a formatted string for Telegram."""
    result = await asyncio.to_thread(_list_bucket_sync, prefix, max_keys, bucket)

    if "error" in result:
        return f"버킷 조회 실패했다냥: {result['error']} nya."

    objects = result["objects"]
    if not objects:
        return f"버킷 <code>{html_mod.escape(result['bucket'])}</code>에 파일이 없다냥." + (
            f" (prefix: <code>{html_mod.escape(prefix)}</code>)" if prefix else ""
        ) + " nya"

    lines = [f"<b>버킷: <code>{html_mod.escape(result['bucket'])}</code></b> 다냥"]
    if prefix:
        lines.append(f"prefix: <code>{html_mod.escape(prefix)}</code>")
    lines.append(f"{result['count']}개 파일" +
                 (" (더 있음)" if result["truncated"] else "") + " nya")
    lines.append("")

    for obj in objects:
        lines.append(f"<code>{html_mod.escape(obj['key'])}</code> — {obj['size_human']}")

    return "\n".join(lines)


async def get_object_info(key: str, bucket: str | None = None) -> str:
    """Get file info. Returns a formatted string."""
    result = await asyncio.to_thread(_get_object_info_sync, key, bucket)

    if "error" in result:
        return f"{result['error']} nya."

    return (
        f"<b>파일 정보</b> 다냥\n\n"
        f"버킷: <code>{html_mod.escape(result['bucket'])}</code>\n"
        f"경로: <code>{html_mod.escape(result['key'])}</code>\n"
        f"크기: {result['size_human']}\n"
        f"타입: {result['content_type']}\n"
        f"수정: {result['last_modified']} nya"
    )


async def download_and_send(
    key: str,
    application,
    chat_id: int,
    bucket: str | None = None,
) -> str:
    """Download a file from B2 and send it to the user via Telegram.

    For files <= 50MB: sends directly as a Telegram document.
    For larger files: generates a presigned download URL.
    """
    info = await asyncio.to_thread(_get_object_info_sync, key, bucket)

    if "error" in info:
        return f"{info['error']} nya."

    size = info["size"]

    if size <= MAX_TELEGRAM_FILE_SIZE:
        # Download to temp and send as document
        suffix = os.path.splitext(key)[1] or ".bin"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name

        try:
            result = await asyncio.to_thread(
                _download_object_sync, key, tmp_path, bucket
            )
            if not result:
                return f"다운로드 실패했다냥: {key} nya."

            filename = os.path.basename(key)
            safe_key = html_mod.escape(key)
            with open(tmp_path, "rb") as f:
                await application.bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    filename=filename,
                    caption=f"<code>{safe_key}</code> ({info['size_human']}) 다냥",
                    parse_mode="HTML",
                )
            return f"파일 전송 완료했다냥: <code>{safe_key}</code> ({info['size_human']}) nya."
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
    else:
        # File too large for Telegram — send presigned URL
        url = await asyncio.to_thread(
            _generate_presigned_url_sync, key, 3600, bucket
        )
        if not url:
            return f"파일이 너무 크고({info['size_human']}), 다운로드 URL 생성도 실패했다냥 nya."

        safe_key = html_mod.escape(key)
        await application.bot.send_message(
            chat_id=chat_id,
            text=(
                f"<code>{safe_key}</code> ({info['size_human']}) 다냥\n\n"
                f"파일이 50MB를 초과해서 직접 전송이 불가하다냥.\n"
                f"다운로드 링크 (1시간 유효) nya:\n{url}"
            ),
            parse_mode="HTML",
        )
        return f"다운로드 링크 전송 완료했다냥: <code>{safe_key}</code> nya."