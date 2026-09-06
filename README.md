# OpenShorts Autopilot — CodeAZ tech shorts

Automated clipping pipeline for CodeAZ: finds CC-BY-licensed tech videos —
Linux, security, self-hosting, dev tooling, hardware — cuts them into shorts with
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
8. Edit `sources.json` — see **Tuning what gets picked** below. Add a channel to `cc_channels` only after you've personally watched one of its videos and confirmed its CC policy; `find_channels.py` narrows the field for you but does not vet anything.

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
- **Is any on-screen text cut off at the left and right edges?** This is the failure that shipped six times: see `content_height_ratio` below.
- Do the subtitles actually match what's said?
- Does the AI-generated hook text overstate or misrepresent what the clip shows?
- Does the clip make sense as a standalone 20–75s piece, or does it need context from earlier in the video?

To dry-run one specific talk instead of letting research pick:
```bash
python dry_run.py --url "https://www.youtube.com/watch?v=..."
```

**Before going live, also consider setting `BUFFER_DRAFT=1`** for the first
real run or two — clips get queued in Buffer as drafts you approve by hand
instead of publishing immediately, while you build trust in the pipeline
without a dry run's total isolation.

## Tuning what gets picked, and how it's framed

Everything below lives in `sources.json` under `niche`, so a change to any of
it is one commit and no YAML edit.

**Topic** — `topic_terms`, `exclude_terms`, `allowed_languages`. A candidate
must match at least one topic term (in its title, the first 600 characters of
its description, or its tags), must match no exclude term, and must not
declare a language outside `allowed_languages`. Exclude terms are checked
first and win.

This check did not exist until 5-sep-2026, and its absence is the whole reason
six consecutive posts on a channel called *CodeAZ Tech Shorts* were AskReddit
stories about undiagnosed medical conditions. Licence verification answers
"may we repost this"; the channel allowlist answers "does this uploader
license CC". **Neither answers "is it about our subject"** — and a vetted
channel can change format under you, which is exactly what happened.
`allowed_languages` is a separate axis because keywords cannot see language: a
Hindi ML lecture matches `machine learning` perfectly and is still unusable
here.

**Clip topic** — `clip_exclude_terms`, checked by `clip_verdict` *after*
clipping, against the title/description/hook Gemini wrote for each clip.

This is a second gate because source metadata is a weak proxy that fails in a
specific direction: a channel's description says what the *channel* is, not
what this video is. "Her Brother Won't Let Her Play on the Computer" carries a
hardware description because it comes from a PC channel — it passes the source
filter — and the three clips cut from it were "I realized I was a terrible
brother", "Why I gave a stranger a gaming PC" and "The 1% rule that changed my
life". All three had to be deleted by hand. The generated title is the honest
signal, because it describes the 40 seconds actually being posted.

Rejecting here wastes a render instead of a post, which is the cheaper
mistake. Only exclusions run at this stage, never `topic_terms` — a legitimate
clip can open on a line that names no technology at all.

**Framing** — `content_height_ratio` and `layouts`.

`content_height_ratio` becomes the backend's `GENERAL_CONTENT_HEIGHT_RATIO`:
how much of the frame height the content fills in the blurred-background
layout, paid for by **cropping the sides**.

There is a hard floor and it is **0.316** — a 16:9 source fills the 1080px
frame width at 608px tall, and 608/1920 = 0.316. Every value above that
discards width. Measured against `reframe_v2`'s own filtergraph on a real
source:

| ratio | content height | width kept |
|-------|----------------|------------|
| 0.60 (was hardcoded in both workflows) | 1152px | **53%** |
| 0.42 (upstream default) | 806px | **75%** |
| 0.31 (now) | 608px | **100%** |

At 0.60 a 75s clip of a Reddit thread shipped with every line sliced off both
edges. 0.42 is not enough either — it still cuts a quarter of the width, which
is still mid-sentence. 0.31 sits under the floor, so the `max()` in
`general_filtergraph` pins the content to full width and nothing is ever cut.

The cost is real: at full width a 16:9 source is 32% of the frame height with
blurred filler above and below, so text is intact but small. That is the
better failure — cropping throws information away, small is still readable.
**If a clip looks like a thumbnail floating in soup, the fix is the source.**
A video that is a static full-frame text page cannot be made good by any 9:16
layout, at any ratio.

`layouts` now includes `screencast`, which turns on OpenShorts'
`SCREENCAST_LAYOUT`. It is off by default upstream, and its own module
docstring describes our exact failure: *"a screen recording that happens to
contain a face gets classified TRACK, the 9:16 crop keeps a centre strip, and
the chart or headline the shot is actually about comes out sliced mid-word."*
It measures how much of the frame **width** the content spans and routes those
scenes to a stacked layout (content over speaker) or to full-width, instead of
cropping them.

**Source order** — `research.py` interleaves its search arms rather than
concatenating them. A channel-scoped search returns up to 25 videos and
`autopilot.py` only ever posts `candidates[0]`, so concatenation meant one
channel owned every reachable slot and the keyword arm — appended after five
channel arms — could never be reached at all.

## Finding new source channels

```bash
export YOUTUBE_API_KEY=...
python find_channels.py --json
python find_channels.py --query "neovim config" --query "proxmox homelab"
```

No API key on the machine? It falls back to the OAuth refresh token this repo
already uses to upload, and `--env-file` can read it from another project:

```bash
python find_channels.py --env-file ../mpt/.env --json
```

`search.list` and `videos.list` are public reads, so either credential
authorizes them. If YouTube answers `403 insufficientPermissions`, the token
was minted upload-only — re-mint it with the `youtube.readonly` scope.

Runs the niche's search queries with YouTube's CC filter, re-verifies every
hit's licence per video, drops what the topic filter rejects, and groups the
survivors **by channel** — so what you see is how many long, on-topic,
genuinely CC-licensed videos a channel actually has, plus its median view
count and a sample of titles.

Read the titles before adding anything. That is the step that would have
caught "Humor Studios", listed with the note *"Security/hacker story format"*,
as an AskReddit storytime channel before it posted six times.

## Running for real

1. Push this repo to GitHub.
2. Add the secrets from `.env.example`.
3. `autopilot.yml` runs daily at 14:00 UTC. Trigger it manually anytime from the Actions tab.
4. `dry_run.yml` stays available as a manual-only workflow — run it anytime from Actions to sanity-check output without posting anything, even after you've gone live.

## Known limits (honesty section)

- **The CC-licensed pool for this niche is small, and the topic filter makes
  it smaller.** Unlike scraping "whatever performs well," a properly-licensed
  source pool is genuinely limited — expect this to run out of fresh material
  from one channel faster than a general-purpose channel would, and
  `topic_terms` / `exclude_terms` deliberately throw away more of what's left.
  That trade is the right one: the alternative is what shipped before the
  filter existed. Widen `topic_terms` or run `find_channels.py` for more
  sources; don't lower the bar to keep the pipeline fed.
- **A channel's note in `sources.json` is a claim, not a fact.** Two of the
  five originally-listed channels turned out not to match their own notes —
  one an AskReddit storytime channel described as "security/hacker story
  format", one Hindi-language lectures described as "AI coding and dev
  education". Watch a video before you add a channel, and re-check the ones
  that are already there when their output starts looking wrong.
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
