# OpenShorts Autopilot — open-source-projects niche

Automated clipping pipeline for CodeAZ: finds CC-BY-licensed conference talks
about open source projects, cuts them into shorts with
[OpenShorts](https://github.com/mutonby/openshorts)'s clip generator, credits
the original speaker/talk in every caption, and posts directly to **YouTube
(OAuth, no middleman)** and **TikTok (via Buffer)**. Runs on GitHub Actions,
same shape as `mpt`'s autopilot: cron trigger, `posted.json` tracks what's
been used so nothing repeats, state gets committed back after every run.

**What this is not:** a "find whatever's performing well and repost it"
scraper. `research.py` only pulls videos whose YouTube license flag is
`creativeCommon`, re-verified per video (not just trusted from search), from
a small allowlist of channels whose CC policy has actually been checked (see
`sources.json`). Every post carries a mandatory attribution line — title,
speaker/channel, link, license — because that's what CC BY requires, not
because it's nice to have.

## Why direct YouTube API + Buffer instead of Upload-Post

OpenShorts' own `/api/social/post` endpoint is hardcoded server-side to
Upload-Post — there's no config flag to swap it. So this pipeline bypasses
that endpoint entirely: it downloads each rendered clip from OpenShorts, then
posts it itself — straight to the YouTube Data API via an OAuth refresh
token (same pattern `mpt` uses), and to TikTok via Buffer's GraphQL API
(same as `mpt`'s `buffer.py`). No Upload-Post account, no 10/month cap from
that service — YouTube's own upload quota (~6/day per Cloud project) and
Buffer's plan limits apply instead.

## One-time setup

1. **Gemini key** — [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey), free. Powers OpenShorts' clip detection.
2. **YouTube Data API key** — [console.cloud.google.com](https://console.cloud.google.com), enable "YouTube Data API v3", create an API key. Used only by `research.py` for read-only search.
3. **YouTube upload OAuth client** — same Cloud project, Credentials → Create Credentials → OAuth client ID → **Desktop app** type. Gives you `YT_CLIENT_ID` / `YT_CLIENT_SECRET`. Add yourself as a test user on the consent screen.
4. **Mint a refresh token** (once, locally, on a machine with a browser):
   ```bash
   pip install google-auth-oauthlib
   export YT_CLIENT_ID=... YT_CLIENT_SECRET=...
   NICHE=codeaz python get_youtube_token.py
   ```
   Sign in as the channel you want this niche to upload to. Save the printed token as `YT_REFRESH_TOKEN_CODEAZ` (the suffix comes from `sources.json` `niche.env_suffix`).
5. **Buffer** — [publish.buffer.com](https://publish.buffer.com), connect your TikTok account under Channels, then grab a personal API key from Settings → API. Save as `BUFFER_ACCESS_TOKEN`.
6. Copy `.env.example` to `.env` and fill it in for local testing.
7. Once you push this repo, add every secret from `.env.example` (except the two commented-optional ones) under Settings → Secrets and variables → Actions.
8. Edit `sources.json` — the `cc_channels` list currently has one entry (FOSDEM). Add more only after you've personally confirmed the channel's CC policy; don't add a channel just because it's open-source-adjacent.

## Test it locally before it ever posts anything

This is the step that actually matters — don't skip to pushing and letting
Actions post on a schedule before you've watched what comes out. The dry run
never touches YouTube or Buffer either.

```bash
git clone https://github.com/mutonby/openshorts.git openshorts_app
cd openshorts_app
cp .env.example .env
docker compose up -d backend renderer
cd ..

pip install -r requirements.txt
export GEMINI_API_KEY=...
export YOUTUBE_API_KEY=...
python dry_run.py
```

This runs the real research → clip pipeline, downloads every resulting clip
to `dry_run_output/`, and writes `dry_run_output/report.md` with each clip's
would-be title and caption. **Nothing gets posted anywhere, and
`posted.json` is not touched** — run it as many times as you want while
tuning `sources.json`.

Watch the clips and specifically check:
- Does the vertical crop keep the speaker in frame (TRACK mode can lose them during slide-heavy segments)?
- Do the subtitles actually match what's said?
- Does the AI-generated hook text overstate or misrepresent what the clip shows?
- Does the clip make sense as a standalone 20–75s piece, or does it need context from earlier in the talk?

To dry-run one specific talk instead of letting research pick:
```bash
python dry_run.py --url "https://www.youtube.com/watch?v=..."
```

**Before going live, also consider setting `BUFFER_DRAFT=1`** for the first
real run or two — clips get queued in Buffer as drafts you approve by hand
instead of publishing immediately, while you build trust in the pipeline
without a dry run's total isolation.

## Running for real

1. Push this repo to GitHub.
2. Add the secrets from `.env.example`.
3. `autopilot.yml` runs daily at 14:00 UTC. Trigger it manually anytime from the Actions tab.
4. `dry_run.yml` stays available as a manual-only workflow — run it anytime from Actions to sanity-check output without posting anything, even after you've gone live.

## Known limits (honesty section)

- **The CC-licensed pool for this niche is small.** Unlike scraping "whatever
  performs well," a properly-licensed source pool is genuinely limited —
  expect this to run out of fresh material from one channel faster than a
  general-purpose channel would. Add more verified CC channels as you find
  them; don't lower the bar to keep the pipeline fed.
- **YouTube's upload quota is per Google Cloud project, not per channel** —
  roughly 6 uploads/day. At `target_clips_per_video: 3` you'll hit that in
  two runs. Lower the clip count, run less often, or give this niche its own
  Cloud project + OAuth client if you want to scale past it (same pattern
  `mpt` uses for multi-niche: `YT_CLIENT_ID_<NICHE>` overrides).
- **Buffer has its own plan limits and TikTok's own review process** — a
  brand-new TikTok app connected to Buffer may post privately until TikTok
  approves it for public posting; check Buffer's TikTok connection status if
  posts aren't showing up publicly.
- **OpenShorts is a server app, not a script.** Every scheduled run spins up
  its Docker containers fresh inside the Actions runner, which is slower and
  heavier than `mpt`'s plain-Python approach. A long conference talk can take
  a while to transcribe + analyze + render — the workflow's 110-minute
  timeout should cover one video, but watch actual run times in Actions and
  adjust `target_clips_per_video` down if you're cutting it close.
- **Attribution is enforced in code, not just policy.** `attribution.py`
  always appends the credit line — if you edit captions downstream, keep that
  line intact. Removing it turns a licensed repost into an unlicensed one.
- **A CC license doesn't waive platform ToS or trademark/logo rights** on
  its own — if a talk shows copyrighted slide content, a company logo, or
  similar within the recording, that's still there regardless of the talk's
  own CC status. Spot-check dry runs for this, the license check doesn't
  catch it.
