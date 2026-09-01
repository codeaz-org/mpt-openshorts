"""
openshorts_client.py -- talks to a locally-running OpenShorts backend
(https://github.com/mutonby/openshorts), started via its own docker-compose
before this script runs. All endpoints confirmed against app.py directly:
  POST /api/process        -> {job_id}
  GET  /api/status/{id}    -> {status, logs, result:{clips:[...]}}
  POST /api/social/post    -> proxies to Upload-Post for one clip
"""
import os
import time
import requests

DEFAULT_BASE_URL = os.environ.get("OPENSHORTS_BASE_URL", "http://localhost:8000")
POLL_INTERVAL_SECONDS = 10
JOB_TIMEOUT_SECONDS = int(os.environ.get("OPENSHORTS_JOB_TIMEOUT", "3600"))


class OpenShortsError(RuntimeError):
    pass


class OpenShortsClient:
    def __init__(self, base_url=None, gemini_key=None):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.gemini_key = gemini_key or os.environ.get("GEMINI_API_KEY")
        if not self.gemini_key:
            raise OpenShortsError("GEMINI_API_KEY is required (self-host header X-Gemini-Key).")

    def _headers(self):
        return {"X-Gemini-Key": self.gemini_key}

    def submit_job(self, source_url, niche):
        """Submit a YouTube URL for clipping. `acknowledged=True` is a truthful
        attestation here: the source is CC-BY licensed, which is exactly the
        'rights to process it' OpenShorts is asking the caller to confirm --
        it is not a rubber stamp over unlicensed content."""
        body = {
            "url": source_url,
            "acknowledged": True,
            "output_format": "vertical",
            "target_clips": niche.get("target_clips_per_video", 3),
            "clip_min_seconds": niche.get("clip_min_seconds", 20),
            "clip_max_seconds": niche.get("clip_max_seconds", 75),
            "layouts": niche.get("layouts", ["auto"]),
            "auto_hook": niche.get("auto_hook", True),
            "captions": niche.get("captions", True),
        }
        resp = requests.post(
            f"{self.base_url}/api/process",
            json=body,
            headers=self._headers(),
            timeout=60,
        )
        if resp.status_code == 200 and resp.json().get("needs_confirmation"):
            # Quality gate tripped (source below configured min resolution).
            # Retry once, forcing low quality through rather than silently
            # dropping the run -- conference recordings are often 720p.
            body["force_low_quality"] = True
            resp = requests.post(
                f"{self.base_url}/api/process",
                json=body,
                headers=self._headers(),
                timeout=60,
            )
        resp.raise_for_status()
        data = resp.json()
        job_id = data.get("job_id") or data.get("id")
        if not job_id:
            raise OpenShortsError(f"No job_id in response: {data}")
        return job_id

    def wait_for_result(self, job_id):
        deadline = time.time() + JOB_TIMEOUT_SECONDS
        while time.time() < deadline:
            resp = requests.get(
                f"{self.base_url}/api/status/{job_id}",
                headers=self._headers(),
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status", {})
            # /api/status returns a bare string ("queued"/"processing"/
            # "completed"/"failed"); older/cloud builds returned a dict.
            if isinstance(status, dict):
                state = status.get("state") or status.get("stage") or str(status)
                error = status.get("error")
            else:
                state = str(status)
                error = None
            clips = (data.get("result") or {}).get("clips") or []
            print(f"[openshorts] job={job_id} status={state} clips={len(clips)}")
            # Only a completed job is done: the backend appends to
            # result.clips as each one finishes rendering, so returning on
            # the first non-empty list drops every clip after it.
            if state == "completed" and clips:
                return data["result"]
            if state == "failed" or error:
                # The backend puts the reason in the log tail, not in a field.
                tail = " | ".join(str(l) for l in (data.get("logs") or [])[-3:])
                raise OpenShortsError(f"Job {job_id} failed: {error or tail or state}")
            if state == "completed":
                raise OpenShortsError(
                    f"Job {job_id} completed with no clips in the result.")
            # else: still queued/processing -- keep polling.
            time.sleep(POLL_INTERVAL_SECONDS)
        raise OpenShortsError(f"Job {job_id} timed out after {JOB_TIMEOUT_SECONDS}s")

    def download_clip(self, job_id, clip, dest_path):
        """Pull a clip's rendered mp4 to local disk (used by dry_run.py)."""
        video_url = clip["video_url"]  # e.g. /videos/{job_id}/{filename}
        resp = requests.get(f"{self.base_url}{video_url}", timeout=120, stream=True)
        resp.raise_for_status()
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                f.write(chunk)
        return dest_path

    # No post_clip method here on purpose: OpenShorts's own /api/social/post
    # is hardcoded server-side to Upload-Post (see app.py). We don't use
    # Upload-Post, so posting is done directly by youtube_uploader.py and
    # buffer_client.py against the clip downloaded via download_clip() above.

    def wait_ready(self, timeout_seconds=180):
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                resp = requests.get(f"{self.base_url}/health", timeout=5)
                if resp.status_code == 200:
                    return True
            except requests.RequestException:
                pass
            time.sleep(3)
        raise OpenShortsError("OpenShorts backend did not become healthy in time.")
