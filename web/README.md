# 焦点 Focal — web app

A free online image and research tool site in Chinese and English, built on this repository:

| Page | What it does | Engine |
|---|---|---|
| `/cutout` 智能抠图 | Removes the background and exports a transparent PNG; swap in white, black or any colour before downloading. | BRIA RMBG-1.4 via [IOPaint](https://github.com/Sanster/IOPaint) |
| `/erase` 涂抹消除 | Paint over people, clutter, watermarks or text; the painted area is filled from its surroundings. Erase repeatedly, undo the last erase, hold to compare. | LaMa via IOPaint |
| `/heatmap` 注意力热力图 | Predicts where people look on a UI screenshot, scores regions, compares two versions A/B. | HotGaze (this repository) |
| `/focus-group` AI 焦点小组 | Simulates a focus group on one research question: recruits participants and a discussion guide (reviewed and edited before it runs), streams the discussion turn by turn, then writes a structured report. Optional material: a screen flow, two A/B flows, or one page with its HotGaze heatmap. | DeepSeek API ([`focus_group.py`](focus_group.py)) |

The home page takes an upload, a drop or a paste and asks which tool to use. The
UI is a single page (`static/index.html`, `static/css/focal.css`,
`static/js/*.js`, no build step) served by FastAPI (`app.py`). Chinese is the
default; the 中/EN switch in the header is remembered per browser, and every API
error comes back as a stable code the page translates.

Nothing is stored. Uploads live in memory or a temporary directory for the
duration of one request. There is no login, so anyone with the URL can use it.
The AI focus group is the one tool that leaves the server: what people type and
upload there is sent to the DeepSeek API, and the page says so.

**Non-commercial.** BRIA RMBG-1.4's licence allows non-commercial use only, and
the site says so in the cutout panel and the footer. For commercial use, switch
`REMOVE_BG_MODEL` in `services/iopaint` to a model whose licence allows it (or
license RMBG from BRIA).

## Architecture on Render

```
browser ──HTTPS──▶ hotgaze (web, free, 0.1 CPU / 512 MB)
                     ├─ heatmap: HotGaze runs in-process
                     ├─ focus group ──HTTPS──▶ DeepSeek API (streamed, needs DEEPSEEK_API_KEY)
                     └─ cutout / erase ──private network──▶ iopaint (pserv, 1 CPU / 2 GB)
                                                             LaMa + RMBG-1.4, one at a time
```

The web service downsizes photos to at most 2048 px on the long side, sends them
to the IOPaint private service as JSON, and streams the image back. IOPaint has
no public URL. Its image, models included, is described in
[`services/iopaint/README.md`](../services/iopaint/README.md).

## Deploy to Render

The repository root holds a Render Blueprint, [`render.yaml`](../render.yaml),
with both services.

1. Push the branch that contains `render.yaml` to GitHub.
2. In the [Render Dashboard](https://dashboard.render.com), choose
   **New → Blueprint**, connect the repository, pick that branch and click
   **Apply**. If the first version of this Blueprint (the `hotgaze` web service
   alone) is already deployed, open that Blueprint and sync it instead: the web
   service updates in place and the `iopaint` private service is added.
3. Wait for both deploys. The IOPaint image downloads PyTorch and bakes in the
   models, so its first build takes several minutes.
4. Open the web service URL (`https://<name>.onrender.com`) and try the samples.
   Right after IOPaint starts it loads its first model, so the first cutout can
   take a minute or two; later ones take about 10–15 s.
5. For the AI focus group, give the web service a DeepSeek API key (see
   [Setting the DeepSeek API key](#setting-the-deepseek-api-key)).

### Setting the DeepSeek API key

The key is a secret, so `render.yaml` declares `DEEPSEEK_API_KEY` with
`sync: false` and never holds the value:

- **New Blueprint:** Render asks for `DEEPSEEK_API_KEY` while you create it.
  Paste the key from [platform.deepseek.com](https://platform.deepseek.com) →
  API keys.
- **Service that already exists** (Render only asks on first creation and
  ignores `sync: false` variables when it syncs an existing Blueprint): open the
  Dashboard → **hotgaze** → **Environment** → **Add Environment Variable**, set
  `DEEPSEEK_API_KEY` to the key, and save. Render redeploys the service.

Without a key the focus group page says the tool is not configured and still
shows the sample session. A wrong key, an empty balance or a refused request is
reported on the page in plain words (`errors.focus_*` in `static/js/i18n.js`).

| Service | Plan | Notes |
|---|---|---|
| `hotgaze` (web) | `free` | 512 MB RAM, 0.1 CPU. Sleeps after 15 minutes without traffic; the next visit waits about a minute while it wakes. |
| `iopaint` (private) | `1c-2g` | Paid: 1 CPU, 2 GB RAM, billed by Render while it exists. Private services do not sleep. A free web service may call a paid private service in the same region and workspace. |

Both services use region `singapore` (closest to East Asia). They must share a
region to reach each other, and Render cannot move a service after it is
created, so change both before the first deploy if you want another region.

Measured on one CPU: a cutout takes about 10–15 s; an erase 10–40 s depending on
the painted area. The private service runs one request at a time; others wait
(up to two minutes) in the web service's queue.

### Web service settings

| Variable | Default | Purpose |
|---|---|---|
| `IOPAINT_URL` | from `iopaint` | `host:port` (or a full URL) of the IOPaint service. Unset, the cutout and erase pages say the tools are unavailable. |
| `HOTGAZE_WEB_MAX_PIXELS` | `10000000` | Largest heatmap image (width × height). A 10 MP comparison peaks around 390 MB; raise it on larger instances. |
| `HOTGAZE_WEB_MAX_UPLOAD_MB` | `20` | Largest accepted upload, all tools. |
| `HOTGAZE_WEB_MAX_JOBS` | `1` | Heatmap analyses (and cutout/erase image preparation) that may run at once. Others wait up to two minutes, then get HTTP 503. |
| `HOTGAZE_ENABLE_DEEP` | `false` | Build time: install CPU-only PyTorch and the UNISAL weights for the deep heatmap backend. |
| `HOTGAZE_CACHE` | project dir | HotGaze's weight cache; `render.yaml` keeps it inside the deployed checkout. |
| `DEEPSEEK_API_KEY` | none | Turns on the AI focus group. Secret: set it in the Dashboard (above). |
| `DEEPSEEK_MODEL` | `deepseek-flash` | Model for planning and the discussion. It must read images for the screenshot modes (`deepseek-flash` does). |
| `DEEPSEEK_REPORT_MODEL` | `DEEPSEEK_MODEL` | Model for the final report only, which is text; `deepseek-v4-pro` is the stronger option. |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | Another OpenAI-compatible endpoint, e.g. a local mock while developing. |

Cutout and erase accept photos up to 25 MP and downscale them to 2048 px on the
long side before IOPaint sees them.

`render.yaml` also sets `MALLOC_MMAP_THRESHOLD_` and `MALLOC_ARENA_MAX`, which
keep glibc from holding on to freed image buffers, and `OPENCV_LOG_LEVEL=ERROR`.

### AI focus group

A session has three steps, each one request that answers as server-sent events:

1. **Recruit.** From the research question (plus optional background, target
   users and material) the model recruits 4–6 participants with concrete,
   differing personas (at least one sceptic) and drafts a 3–5 question guide.
   With screenshots it first describes every screen; in heatmap mode the server
   runs HotGaze on the page and the model writes notes on what the heatmap shows.
   The researcher can edit names, personas, attitudes and questions, reorder or
   remove them, or recruit again.
2. **Discuss.** The moderator asks each question; every participant answers in a
   rotating order and hears everyone before them; the moderator may probe one or
   two people once per question; a closing round asks for a final word (in A/B
   mode, a preference). Each turn streams as it is written. The page can stop a
   session and continue it later from the last finished turn, also after a
   dropped connection.
3. **Report.** A structured report from the transcript: findings with verbatim
   quotes (quotes that do not match what a participant said are dropped), final
   attitudes, pain points, needs, disagreements, opportunities, what to check
   with real users and the limits of a simulation; plus flow issues by screen,
   the A/B preference split, or predicted attention against what people said.
   It downloads as Markdown, and the whole session as JSON.

Participant turns run without DeepSeek's thinking mode, for speed; planning and
the report think. Every turn re-sends the shared context, which DeepSeek's prefix
cache serves cheaply, and each question sends only the screenshots it covers. A
typical text session (5 people, 4 questions) is about 40 calls and 2–4 minutes;
with `deepseek-flash` it costs roughly ¥0.1–0.3 at DeepSeek's November 2026
prices. The report shows the session's calls and tokens.

The sample session behind 看一场示例讨论 and the home page excerpt was written
by Claude following this pipeline, before a DeepSeek key was available, and the
page labels it that way; see [`static/samples/PROVENANCE.md`](static/samples/PROVENANCE.md)
to replace it with a real run.

### Enabling the deep heatmap backend

The deep (UNISAL) backend needs PyTorch and about 0.75 GB of memory per
analysis, more than the free plan's 512 MB. In `render.yaml`, set the web
service's `plan: 1c-2g` (or larger) and `HOTGAZE_ENABLE_DEEP` to `"true"`, then
push. The build installs the CPU-only PyTorch wheel and prefetches the weights,
and the deep model becomes selectable on the heatmap page.

## Run locally

From the repository root:

```bash
pip install . -r web/requirements.txt
uvicorn app:app --app-dir web --reload
# open http://127.0.0.1:8000
```

The heatmap works on its own. For the AI focus group, set a DeepSeek key:

```bash
DEEPSEEK_API_KEY=sk-... uvicorn app:app --app-dir web --reload
```

For cutout and erase, run the IOPaint image and point the app at it:

```bash
docker build -t focal-iopaint services/iopaint
docker run --rm -p 127.0.0.1:8080:8080 --memory 2g focal-iopaint
IOPAINT_URL=127.0.0.1:8080 uvicorn app:app --app-dir web --reload
```

## API

Interactive documentation is served at `/docs`. All endpoints take multipart
form data.

```bash
curl -F image=@photo.jpg -o cutout.png https://<name>.onrender.com/api/cutout
curl -F image=@photo.jpg -F mask=@mask.png -o erased.jpg https://<name>.onrender.com/api/erase
curl -F image=@screenshot.png -F region=cta:250,200,200,35 https://<name>.onrender.com/api/score
curl -F image_a=@baseline.png -F image_b=@candidate.png \
  -F region=cta:0.35,0.55,0.30,0.12f https://<name>.onrender.com/api/compare
```

| Endpoint | Response |
|---|---|
| `POST /api/cutout` | PNG with a transparent background. |
| `POST /api/erase` | `mask` is the same size as `image`, white where to erase. JPEG for JPEG input, PNG otherwise. |
| `POST /api/score` | `score` (schema-v1 score document, as `hotgaze score --json`), `overlay` (JPEG data URL), `elapsed_seconds` |
| `POST /api/compare` | `compare`, `score_a`, `score_b` (the documents the GitHub Action writes), `overlay_a`, `overlay_b`, `elapsed_seconds` |
| `GET /api/info` | version, available tools and backends, limits |
| `POST /api/focus-group/plan` | Events `status`, `attention` (heatmap mode: overlay and HotGaze numbers), `plan`, `usage`, `done`. Fields: `topic`, `lang`, `context`, `audience`, `participants` (3–6), `mode` (`text`, `flow`, `ab`, `heatmap`), `images` (repeatable), `images_b` (A/B), `region` (heatmap mode). |
| `POST /api/focus-group/discuss` | Events `turn`, `delta`, `turn_end`, `usage`, `done`. Fields: the brief again, `plan` (JSON, as edited), `transcript` (JSON turns to resume after), the screenshots, `overlay` (heatmap mode). |
| `POST /api/focus-group/report` | Events `status`, `report`, `usage`, `done`. Fields: the brief, `plan`, `transcript`. |

Image responses carry `X-Focal-Size` (the processed size) and `X-Focal-Elapsed`.
Heatmap fields: `region` is repeatable (or newline-separated) in the CLI's
`name:x,y,w,h` / `name:0.1,0.2,0.3,0.08f` syntax (any name without a colon,
Chinese included); optional `backend` (`fast` or `deep`), `faces`, `alpha` (0–1),
`colormap` (`jet` or `turbo`) and `overlay`: `focus` (the default) blends the colormap
in proportion to attention, so quiet parts of the page stay readable and `alpha` is the
opacity at the peak; `classic` is exactly the overlay `hotgaze run` writes.

The focus group endpoints answer with `text/event-stream`, send a `: ping`
comment while the model thinks, and report a failure mid-stream as an `error`
event with the same `{code, message, params}` shape.

Errors are JSON, `{"detail": {"code": ..., "message": ..., "params": {...}}}`:
HTTP 400 for invalid input (`unreadable_image`, `unsupported_format`,
`invalid_region`, `mask_size_mismatch`, `empty_mask`, `tool_rejected`, …), 413
for files or images over the limits, 502 when IOPaint failed (`tool_failed`),
and 503 when a service is busy or unreachable (`busy`, `tool_unavailable`). The
UI's translations live in `static/js/i18n.js`.

## Tests

```bash
pip install -e ".[dev]" -r web/requirements.txt httpx2
pytest tests/test_web.py tests/test_focus_group.py
```

The tests replace the IOPaint call and the DeepSeek model (a scripted model for
the pipeline, httpx's mock transport for the client), so they run without either
service. They also replay the sample sessions through the pipeline. They are
skipped automatically when the web dependencies are not installed.

## Design

The site's design system is recorded in [`DESIGN.md`](../DESIGN.md); product
intent in [`PRODUCT.md`](../PRODUCT.md). Image credits and provenance:
[`static/img/PROVENANCE.md`](static/img/PROVENANCE.md).
