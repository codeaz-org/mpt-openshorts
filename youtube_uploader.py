"""
youtube_uploader.py -- direct YouTube Data API v3 upload via OAuth refresh
token. Same pattern as mpt: a one-time browser sign-in (get_youtube_token.py)
mints a refresh token, stored as a repo secret, used non-interactively by
every scheduled run after that. No third party sits between this job and
YouTube.

Env (per niche, falls back to the unsuffixed name if the suffixed one is unset):
  YT_CLIENT_ID / YT_CLIENT_ID_<NICHE>
  YT_CLIENT_SECRET / YT_CLIENT_SECRET_<NICHE>
  YT_REFRESH_TOKEN_<NICHE>   (no unsuffixed fallback -- always per-channel)
"""
import os
import re

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


class YouTubeUploadError(RuntimeError):
    pass


def env_suffix(niche_id):
    return re.sub(r"[^A-Za-z0-9]", "", niche_id or "").upper()


def credentials_for(niche_id):
    suffix = env_suffix(niche_id)

    def pick(name):
        return (os.environ.get(f"{name}_{suffix}") or os.environ.get(name) or "").strip()

    client_id = pick("YT_CLIENT_ID")
    client_secret = pick("YT_CLIENT_SECRET")
    refresh_token = (os.environ.get(f"YT_REFRESH_TOKEN_{suffix}") or "").strip()
    return client_id, client_secret, refresh_token


def upload(video_path, title, description, tags, niche_id, category_id="28", privacy="public"):
    """category_id 28 = Science & Technology. Returns (video_id, watch_url)."""
    client_id, client_secret, refresh_token = credentials_for(niche_id)
    if not refresh_token:
        raise YouTubeUploadError(
            f"No YT_REFRESH_TOKEN_{env_suffix(niche_id)} set. "
            f"Run: NICHE={niche_id} python get_youtube_token.py"
        )
    if not client_id or not client_secret:
        raise YouTubeUploadError("YT_CLIENT_ID / YT_CLIENT_SECRET not set.")

    creds = Credentials(
        None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    yt = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags,
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    video_id = resp["id"]
    watch_url = f"https://youtu.be/{video_id}"
    print(f"[youtube] uploaded: {watch_url}")
    return video_id, watch_url
