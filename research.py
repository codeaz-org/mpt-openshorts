"""
research.py -- find long-form YouTube videos we're actually allowed to reclip.

Two-step verification, not one:
  1. search.list(videoLicense="creativeCommon") to narrow the candidate pool.
  2. videos.list(...).status.license re-checked per video, because the search
     filter reflects what the uploader flagged and uploaders get this wrong in
     both directions. Only videos that pass BOTH steps are used.

Two arms, and they are ranked differently on purpose:

  1. An allowlist of channels whose CC licensing a human has checked
     (sources.json), newest first, rotated per run.
  2. Keyword search across all of YouTube, same licence filter and the same
     re-verification, ordered by view count.

The keyword arm did rank by relevance, on the reasoning that a legitimate CC
pool should not chase what is already viral. Watching the output changed that:
the clipper amplifies pacing the source already has and cannot invent it, so
an unwatched CC upload is rarely worth clipping. The licence constraint is
what keeps this legitimate -- every candidate is still verified per video --
not the refusal to notice which videos people watch.

Requires: YOUTUBE_API_KEY (a plain API key is enough -- no OAuth needed,
this only reads public search/videos endpoints).
"""
import json
import os
import random
import sys
from googleapiclient.discovery import build

SOURCES_PATH = os.environ.get("SOURCES_PATH", "sources.json")
POSTED_PATH = os.environ.get("POSTED_PATH", "posted.json")


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r") as f:
        return json.load(f)


def already_used(video_id, posted):
    return any(u.get("source_video_id") == video_id for u in posted.get("uploads", []))


def verify_license(youtube, video_ids):
    """Authoritative check: videos.list status.license, not the search filter."""
    if not video_ids:
        return {}
    out = {}
    # videos.list caps the id filter at 50 -- more than that comes back as
    # HTTP 400 invalidFilters, so page through in chunks.
    for i in range(0, len(video_ids), 50):
        resp = youtube.videos().list(
            part="status,snippet,contentDetails",
            id=",".join(video_ids[i:i + 50]),
        ).execute()
        for item in resp.get("items", []):
            out[item["id"]] = item
    return out


def iso8601_duration_to_seconds(duration):
    import re
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration or "")
    if not m:
        return 0
    h, mnt, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mnt * 60 + s


def find_candidates(niche, posted, max_results=25):
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        print("ERROR: YOUTUBE_API_KEY is not set.", file=sys.stderr)
        sys.exit(1)

    youtube = build("youtube", "v3", developerKey=api_key)
    min_seconds = niche.get("min_source_seconds", 600)
    duration = niche.get("video_duration", "medium")

    candidate_ids = []

    # Rotate which channel is asked first. autopilot always takes candidates[0],
    # so a fixed list order means the first channel is mined until it runs dry
    # and the rest never get a turn -- with FOSDEM listed first, every single
    # run picked a FOSDEM talk. Rotating spreads it across the pool.
    channels = list(niche.get("cc_channels", []))
    if channels:
        k = random.randrange(len(channels))
        channels = channels[k:] + channels[:k]

    # 1) Channel-scoped search -- the reliable source, since these channels'
    #    licensing policy has been checked by a human (see sources.json note).
    for ch in channels:
        resp = youtube.search().list(
            part="snippet",
            channelId=ch["channel_id"],
            type="video",
            videoLicense="creativeCommon",
            videoDuration=duration,
            order="date",
            maxResults=max_results,
        ).execute()
        for item in resp.get("items", []):
            candidate_ids.append(item["id"]["videoId"])

    # 2) Keyword search as a secondary source -- wider net, same license
    #    filter, same re-verification step below. Lower trust than #1.
    for q in niche.get("search_queries", []):
        resp = youtube.search().list(
            part="snippet",
            q=q,
            type="video",
            videoLicense="creativeCommon",
            videoDuration=duration,
            # viewCount, not relevance: this arm exists to widen the pool
            # beyond the vetted channels, and a CC video nobody watched is
            # rarely a video worth clipping.
            order="viewCount",
            maxResults=10,
        ).execute()
        for item in resp.get("items", []):
            candidate_ids.append(item["id"]["videoId"])

    candidate_ids = list(dict.fromkeys(candidate_ids))  # dedupe, keep order
    candidate_ids = [v for v in candidate_ids if not already_used(v, posted)]
    if not candidate_ids:
        return []

    verified = verify_license(youtube, candidate_ids)

    results = []
    for vid in candidate_ids:
        item = verified.get(vid)
        if not item:
            continue
        if item.get("status", {}).get("license") != "creativeCommon":
            # The authoritative flag disagrees with the search filter -- skip.
            continue
        seconds = iso8601_duration_to_seconds(item["contentDetails"]["duration"])
        if seconds < min_seconds:
            continue
        snippet = item["snippet"]
        # OpenShorts names the downloaded file after the video title and hands
        # that name to the Gemini Files API, which puts it in an HTTP header --
        # headers are ASCII-only, so a title carrying an en-dash or an accent
        # fails clip detection and kills the whole job.
        #
        # This is a backstop, not the fix: skipping these throws away most of a
        # conference catalogue (measured: 49 of 50 recent CC-licensed Godot
        # uploads are "Talk - Speaker - GodotCon 2026" with en-dashes). The
        # real fix is in the backend, which uploads through an ASCII-named
        # hardlink. Set OPENSHORTS_ASCII_SAFE=1 when you're pointed at a
        # backend carrying that fix and this guard stands down.
        title = snippet.get("title") or ""
        ascii_safe_backend = (os.environ.get("OPENSHORTS_ASCII_SAFE") or "").strip().lower() in ("1", "true", "yes")
        if not title.isascii() and not ascii_safe_backend:
            offending = sorted({c for c in title if not c.isascii()})
            print(f"Skipping {vid}: non-ASCII title breaks Gemini upload "
                  f"({''.join(offending)!r} in {title!r}). Set OPENSHORTS_ASCII_SAFE=1 "
                  f"if your backend has the hardlink fix.", file=sys.stderr)
            continue
        results.append({
            "video_id": vid,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "title": snippet.get("title"),
            "channel_title": snippet.get("channelTitle"),
            "channel_id": snippet.get("channelId"),
            "duration_seconds": seconds,
            "license": "CC BY 3.0",
        })

    return results


def main():
    sources = load_json(SOURCES_PATH, {})
    niche = sources.get("niche", {})
    posted = load_json(POSTED_PATH, {"uploads": []})

    max_candidates = niche.get("max_candidates_per_run", 5)
    candidates = find_candidates(niche, posted)[:max_candidates]

    print(json.dumps(candidates, indent=2))
    return candidates


if __name__ == "__main__":
    main()
