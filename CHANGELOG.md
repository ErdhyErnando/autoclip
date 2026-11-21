# Changelog

Notable changes. Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [semantic versioning](https://semver.org/) once 0.1.0 ships.

## Unreleased

The promo-editor epic ([#16](https://github.com/ErdhyErnando/autoclip/issues/16))
lands here: per-clip caption styling, x-axis caption placement, more bundled
fonts, and reusable presets. The interface both workstreams build against is
[docs/promo-editor-contract.md](docs/promo-editor-contract.md).

### Added

- **Per-clip caption styling.** `clip_edits` gains a `style_overrides` blob
  (font, colours, outline, size, all-caps, animation, and words-per-group) plus
  `caption_margin_ratio` and `caption_x_ratio`, all nullable, so nothing needs a
  data migration and a clip with no overrides resolves exactly as it did before.
  `PATCH /api/clips/{id}/captions` merges the blob the way the framing
  corrections already do: a key that is present replaces it wholesale, an
  explicit `null` clears it, an absent key leaves it alone. Numbers are clamped;
  an unknown font key is a 400.
- **`ClipOut.effective_caption`** — the resolved caption snapshot for the clip,
  produced by the same resolution the exporter burns. The review player draws it
  directly instead of reimplementing the layering, which is what stops preview
  and export from drifting apart.

### Fixed

- **The review UI's export button could overwrite another clip's file.** Two
  clips sharing a title — which an LLM is free to produce — wrote one filename,
  so exporting B clobbered A's render while A's `Export` row kept pointing at
  it. Batch export already appended the clip's rank (`a699ea6`); the endpoint
  now disambiguates the same way, seeded from the job's persisted exports.
  Re-exporting the same clip still overwrites itself, which is correct.

## 0.1.0 — 2026-11-20

The first tagged release. The reframe acceptance bar was validated against a
fixed golden set — three scenarios (solo-speaker, screen-content,
facecam-stream-vod), automated pass plus a per-scenario human checklist; the
multi-speaker scenario is the gate for 0.1.1.

### Added

- **Pipeline** — ingest (YouTube via yt-dlp, or file upload), audio preparation,
  transcription with word-level timings, LLM highlight detection, speaker-tracked
  reframing, ASS caption generation, and export at 9:16 / 1:1 / 16:9.
- **Four LLM providers** behind one interface: Anthropic, OpenAI-compatible (any
  `base_url`, covering OpenRouter, Groq, DeepSeek, LM Studio, and the OpenCode
  gateway, which needs its own session headers), Google Gemini, and
  Ollama. Malformed responses are retried once with the validation error attached.
- **Reframing** with shot detection, MediaPipe face tracking, active-speaker
  selection from mouth movement correlated against diarization, and a
  One Euro Filter with dead zone and velocity clamp. Shots are framed as TRACK,
  WIDE, or GENERAL; subjects too far apart to crop get a fitted frame over a
  blurred fill rather than someone being cut out.
- **Picture-in-picture exports** — detect a stream VOD's persistent corner
  facecam once per clip, cached with the job, and export the full screen behind
  it as an inset, parked in your chosen corner and sized to taste.
- **Four caption styles** with bundled OFL fonts, so nothing is fetched at runtime.
- **Caption size and position** — global settings scale any preset's font and
  set its height off the bottom; the review player's caption overlay is draggable
  and resizable in place, writing the same values the renderer burns in.
- **Web UI** — ingest, live job progress over SSE, clip review with word-snapping
  trim handles, caption editing, crop nudging with a centre lock, and export.
- **Resumable stages.** Artifacts live on disk per job, so a retry restarts at the
  stage that failed.
- **`autoclip doctor`** — probes ffmpeg features, GPU acceleration, compute-type
  support, and provider reachability, and explains what to do about each.
- **End-to-end test** against real media, covering every stage with only the
  language model's answer scripted.

### Notable fixes during development

Each of these was found by running the thing, not by reading it:

- **Compute-type selection asked the wrong question.** Inferring from CUDA compute
  capability chose `int8_float16` on a GTX 1060, which advertises fp16 to CUDA and
  then can't use it. Selection now queries CTranslate2 for what it actually
  supports.
- **CUDA libraries were installed and unloadable.** pip places them where the OS
  loader doesn't look, so the model loaded and died at first inference. AutoClip
  registers the directories itself.
- **`h264_nvenc` being listed is not proof it works.** A build can require a newer
  NVENC API than the driver provides. Encoder selection now runs a one-frame probe.
- **Filtergraph paths need two levels of escaping**, and quoting interacts badly
  with apostrophes. Renders now use bare relative names with ffmpeg's working
  directory set, removing the problem instead of out-escaping it.
- **The wheel shipped with no UI.** hatchling honours `.gitignore`, and the built
  frontend is gitignored; declared via `artifacts`.
- **Retry could never clear a job's error message**, because the update helper
  skipped `None` values.
- **Progress ran backwards** at every stage boundary, double-counting each
  completed stage.
- **YouTube can serve AV1 inside an mp4** that the pinned format selector
  happily downloaded — and no pipeline stage could decode it. Proven while
  acquiring goldens: three downloads, one undecodable. The default selector
  now asks for H.264 (avc1) first and only falls back to whatever exists.
- **Clips that shared a title overwrote each other's exports.** The export
  filename came from the title alone, so three same-titled clips wrote one
  file and the last render silently won. The golden smoke run caught it on
  real footage; repeat titles now get the clip's rank appended.
- **A 9:16 preview rendered nearly square.** A max-height clamp shortened the box
  without narrowing it, silently violating the declared aspect ratio.
- **The preview showed different framing from the export**, centre-cropping while
  the renderer tracked the speaker.
