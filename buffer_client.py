"""
buffer_client.py -- publish to TikTok through Buffer. Adapted from mpt's
buffer.py (same approach, trimmed to one niche/channel).

Buffer posts to TikTok with its own TikTok-approved app, so captions and
hashtags go out with the video and the post is public -- neither of which an
unaudited direct-to-TikTok app can do. Buffer's GraphQL API has no upload
endpoint: VideoAssetInput takes a URL and Buffer fetches it. The rendered
clip only exists on the runner, so it's published as a GitHub release asset
first -- public repo, stable URL, stays out of git history.

Env:
  BUFFER_ACCESS_TOKEN   personal API key from publish.buffer.com/settings/api
  BUFFER_CHANNEL_ID     the TikTok channel id to post to (skip to auto-detect
                         the only connected TikTok channel). A per-niche
                         BUFFER_CHANNEL_ID_<SUFFIX> takes precedence, where
                         SUFFIX is sources.json's niche.env_suffix.
  BUFFER_MODE           shareNow (default), addToQueue, customScheduled
  BUFFER_DRAFT          set to 1 to stage as a Buffer draft instead of publishing
  GITHUB_TOKEN          for the release upload; Actions provides it automatically
  GITHUB_REPOSITORY     owner/repo; Actions provides it automatically
"""
import mimetypes
import os
import re
import time
from pathlib import Path

import requests

GRAPHQL_URL = "https://api.buffer.com/graphql"
MEDIA_TAG = "autopilot-media"  # one rolling release holds every video asset
KEEP_ASSETS = 20


def log(msg):
    print(f"[buffer] {msg}", flush=True)


def enabled():
    return bool((os.environ.get("BUFFER_ACCESS_TOKEN") or "").strip())


def gql(query, variables=None):
    token = (os.environ.get("BUFFER_ACCESS_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("BUFFER_ACCESS_TOKEN not set")
    r = requests.post(
        GRAPHQL_URL,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"query": query, "variables": variables or {}},
        timeout=90,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("errors"):
        raise RuntimeError("buffer: " + "; ".join(
            e.get("message", "?") for e in payload["errors"])[:300])
    return payload["data"]


def tiktok_channel(env_suffix=None):
    """The TikTok channel id to post to. BUFFER_CHANNEL_ID_<SUFFIX> wins,
    then the unsuffixed BUFFER_CHANNEL_ID; otherwise auto-detects, but only
    if exactly one TikTok channel is connected -- guessing among several
    would post to the wrong audience."""
    suffix = re.sub(r"[^A-Za-z0-9]", "", env_suffix or "").upper()
    forced = ((os.environ.get(f"BUFFER_CHANNEL_ID_{suffix}") if suffix else "")
              or os.environ.get("BUFFER_CHANNEL_ID") or "").strip()
    if forced:
        return forced

    account = gql("{ account { organizations { id } } }")["account"]
    orgs = account.get("organizations") or []
    if not orgs:
        raise RuntimeError("buffer account has no organisation")
    org = orgs[0]["id"]
    channels = gql(
        "query($i: ChannelsInput!){ channels(input: $i){ id name service } }",
        {"i": {"organizationId": org}},
    )["channels"]
    tiktok = [c for c in channels if (c.get("service") or "").lower() == "tiktok"]
    if not tiktok:
        raise RuntimeError("no TikTok channel connected in Buffer "
                            f"(found: {[c.get('service') for c in channels]})")
    if len(tiktok) > 1:
        raise RuntimeError(
            f"{len(tiktok)} TikTok channels connected; set BUFFER_CHANNEL_ID to pick one: "
            + ", ".join(f"{c.get('name')}={c['id']}" for c in tiktok))
    log(f"channel {tiktok[0].get('name', 'tiktok')} ({tiktok[0]['id']})")
    return tiktok[0]["id"]


# ---------- media hosting (GitHub release asset) ----------
def _gh_api(method, url, token, **kw):
    r = requests.request(method, url, timeout=180, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        **kw.pop("headers", {}),
    }, **kw)
    if not r.ok:
        raise RuntimeError(f"github {method} {url.split('/')[-1]}: {r.status_code} {r.text[:200]}")
    return r


def _release(repo, token):
    base = f"https://api.github.com/repos/{repo}"
    r = requests.get(f"{base}/releases/tags/{MEDIA_TAG}", timeout=60, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    if r.status_code == 200:
        return r.json()
    return _gh_api("POST", f"{base}/releases", token, json={
        "tag_name": MEDIA_TAG,
        "name": "Autopilot media",
        "body": "Rendered clips hosted for Buffer to fetch. Managed automatically.",
        "prerelease": True,
    }).json()


def _prune(repo, token, release, keep=KEEP_ASSETS):
    assets = sorted(release.get("assets", []), key=lambda a: a.get("created_at", ""))
    for asset in assets[:-keep] if len(assets) > keep else []:
        try:
            _gh_api("DELETE", f"https://api.github.com/repos/{repo}/releases/assets/{asset['id']}", token)
        except Exception as e:
            log(f"could not delete old asset {asset.get('name')}: {str(e)[:80]}")


def host_file(path, token=None, repo=None):
    """Upload a file as a GitHub release asset and return its public URL."""
    token = token or (os.environ.get("GITHUB_TOKEN") or "").strip()
    repo = repo or (os.environ.get("GITHUB_REPOSITORY") or "").strip()
    if not token or not repo:
        raise RuntimeError("GITHUB_TOKEN and GITHUB_REPOSITORY are needed to host the clip for Buffer")
    path = Path(path)
    release = _release(repo, token)
    name = f"{int(time.time())}-{path.name}".replace(" ", "_")
    upload = release["upload_url"].split("{")[0]
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with open(path, "rb") as f:
        asset = _gh_api("POST", f"{upload}?name={name}", token,
                         headers={"Content-Type": ctype}, data=f).json()
    _prune(repo, token, release)
    log(f"hosted {name} ({path.stat().st_size // 1024}KB)")
    return asset["browser_download_url"]


# ---------- publishing ----------
CREATE_POST = """
mutation($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess { post { id dueAt schedulingType channelService } }
    ... on NotFoundError { message }
    ... on UnauthorizedError { message }
    ... on UnexpectedError { message }
    ... on RestProxyError { message }
    ... on LimitReachedError { message }
    ... on InvalidInputError { message }
  }
}
"""


def _create_post(channel_id, caption, assets, title):
    mode = (os.environ.get("BUFFER_MODE") or "shareNow").strip()
    draft = (os.environ.get("BUFFER_DRAFT") or "").strip().lower() in ("1", "true", "yes")
    data = gql(CREATE_POST, {"input": {
        "channelId": channel_id,
        "text": caption,
        "assets": assets,
        "mode": mode,
        "schedulingType": "automatic",
        "needsApproval": False,
        "saveToDraft": draft,
        "source": "openshorts-autopilot",
        "metadata": {"tiktok": {"title": (title or caption)[:150]}},
    }})
    result = data["createPost"]
    if result.get("__typename") != "PostActionSuccess":
        raise RuntimeError(f"buffer rejected the post ({result.get('__typename')}): "
                            f"{result.get('message', 'no message')}")
    post = result.get("post") or {}
    log(f"queued post {post.get('id')} ({mode}{', draft' if draft else ''}"
        + (f", due {post['dueAt']}" if post.get("dueAt") else "") + ")")
    return post.get("id")


def publish(video_path, caption, title=None, video_url=None, env_suffix=None):
    """Queue the clip on the connected TikTok channel with its caption.
    No thumbnail is sent -- Buffer rejects custom video thumbnails for
    social networks that don't accept them; TikTok picks the cover itself."""
    channel_id = tiktok_channel(env_suffix)
    if not video_url:
        video_url = host_file(video_path)
    return _create_post(channel_id, caption, [{"video": {"url": video_url}}], title)
