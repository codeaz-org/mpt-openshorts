"""
autopilot.py -- the live run. Same shape as mpt's autopilot.py: read config,
find one new thing to make, make it, post it directly (YouTube API + Buffer),
record it in posted.json, commit that file back (the workflow does the git
commit step).

Order per run:
  1. research.py -> a handful of CC-licensed, not-yet-used source videos
  2. take the first candidate, submit it to OpenShorts for clipping
  3. wait for the job, take up to `target_clips_per_video` clips
  4. for each clip: download it locally, build attribution copy, upload
     straight to YouTube via OAuth, publish to TikTok via Buffer
  5. append everything to posted.json (both the source video AND each clip)
"""
import json
import os
import sys
import time

from research import find_candidates, load_json
from openshorts_client import OpenShortsClient, OpenShortsError
from attribution import build_caption, build_youtube_description, build_youtube_title, credit_line
import youtube_uploader
import buffer_client

SOURCES_PATH = os.environ.get("SOURCES_PATH", "sources.json")
POSTED_PATH = os.environ.get("POSTED_PATH", "posted.json")
DOWNLOAD_DIR = "downloaded_clips"


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def post_clip(local_path, title, description, tiktok_caption, niche):
    """Uploads to YouTube directly, publishes to TikTok via Buffer if
    configured. Each platform's failure is independent -- one going down
    shouldn't block the other, and both get logged either way."""
    result = {"youtube": None, "tiktok": None}

    try:
        video_id, url = youtube_uploader.upload(
            local_path, title, description,
            tags=niche.get("youtube_tags", []),
            niche_id=niche.get("env_suffix") or niche["id"],
            category_id=niche.get("youtube_category_id", "28"),
        )
        result["youtube"] = {"video_id": video_id, "url": url}
    except Exception as e:  # noqa: BLE001
        # Deliberately broad: the docstring above promises the platforms are
        # independent, but only YouTubeUploadError was caught, so an expired
        # OAuth token raised RefreshError straight through and aborted the run
        # before TikTok was attempted. Anything YouTube throws is YouTube's
        # problem alone.
        print(f"WARNING: YouTube upload failed: {type(e).__name__}: {e}", file=sys.stderr)
        result["youtube"] = {"error": f"{type(e).__name__}: {e}"}

    if buffer_client.enabled():
        try:
            hosted_url = buffer_client.host_file(local_path)
            post_id = buffer_client.publish(
                local_path, tiktok_caption, title=title, video_url=hosted_url,
                env_suffix=niche.get("env_suffix") or niche["id"])
            result["tiktok"] = {"post_id": post_id, "via": "buffer"}
        except Exception as e:  # noqa: BLE001
            print(f"WARNING: TikTok/Buffer publish failed: {e}", file=sys.stderr)
            result["tiktok"] = {"error": str(e)}
    else:
        print("BUFFER_ACCESS_TOKEN not set; skipping TikTok.")

    return result


def main():
    sources = load_json(SOURCES_PATH, {})
    niche = sources.get("niche", {})
    posted = load_json(POSTED_PATH, {"uploads": []})

    client = OpenShortsClient()
    client.wait_ready()

    candidates = find_candidates(niche, posted)
    if not candidates:
        print("No new CC-licensed candidates found this run. Nothing to do.")
        return

    source = candidates[0]
    print(f"Selected source: {source['title']} ({source['url']}) by {source['channel_title']}")
    print(credit_line(source))

    try:
        job_id = client.submit_job(source["url"], niche)
        result = client.wait_for_result(job_id)
    except OpenShortsError as e:
        print(f"ERROR: clipping failed: {e}", file=sys.stderr)
        posted.setdefault("uploads", []).append({
            "niche": niche.get("id"),
            "source_video_id": source["video_id"],
            "source_title": source["title"],
            "error": str(e),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        save_json(POSTED_PATH, posted)
        sys.exit(1)

    clips = result.get("clips", [])
    n = min(len(clips), niche.get("target_clips_per_video", 3))
    print(f"Job produced {len(clips)} clips, posting {n} of them.")

    for i in range(n):
        clip = clips[i]
        local_path = os.path.join(DOWNLOAD_DIR, f"{job_id}_clip{i}.mp4")
        client.download_clip(job_id, clip, local_path)

        title = build_youtube_title(clip, source)
        yt_description = build_youtube_description(clip, source, " ".join(niche.get("youtube_tags", [])))
        tiktok_caption = build_caption(clip, source, niche.get("hashtags", ""))

        post_result = post_clip(local_path, title, yt_description, tiktok_caption, niche)
        print(f"Clip {i}: {post_result}")

        posted.setdefault("uploads", []).append({
            "niche": niche.get("id"),
            "source_video_id": source["video_id"],
            "source_title": source["title"],
            "source_channel": source["channel_title"],
            "source_url": source["url"],
            "clip_index": i,
            "clip_title": title,
            "job_id": job_id,
            "youtube": post_result.get("youtube"),
            "tiktok": post_result.get("tiktok"),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })

    save_json(POSTED_PATH, posted)
    print("Done. posted.json updated.")


if __name__ == "__main__":
    main()
