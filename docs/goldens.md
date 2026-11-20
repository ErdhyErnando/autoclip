# The golden set: running the acceptance bar

The first tagged release is gated on one thing: the reframe quality bar being
validated against a **fixed golden video set** (README, *What has and hasn't
been verified*). "Fixed" means real footage a human picked, acquired once,
pinned by sha256 — a link is a recipe for re-acquisition, never the golden
itself (videos get taken down and re-encoded).

The bar has two halves. The automated pass catches regressions; the manual
walk decides the bar is met. Automation never signs off — you do.

## The four scenarios

| scenario | what it guards |
|---|---|
| `solo-speaker` | tracking must actually track one face; the crop must not drift or jitter |
| `multi-speaker` | the crop follows whoever speaks; nobody gets cropped away mid-sentence |
| `facecam-stream-vod` | facecam detection finds the corner box; the PiP export keeps the full screen behind a face inset |
| `screen-content` | pure screen output survives 9:16 — no half-cut lines of code, sane fitted frames |

The automated run requires all four. A partial set is a partial bar.

## Acquiring goldens

Media lives outside the repo; a directory holds the files plus the manifest.

```bash
autoclip goldens acquire "https://youtube.com/watch?v=..." \
  --scenario solo-speaker --dest ~/autoclip-goldens
autoclip goldens list --dest ~/autoclip-goldens --verify
```

One file per scenario by default; pass `--name` when a scenario gets more than
one golden. Prefer Creative-Commons videos or your own uploads. Pick sources
at least a few minutes long — the clip window is 12–45s and short footage
produces no clips.

## The automated pass

```bash
AUTOCLIP_GOLDEN_MEDIA=~/autoclip-goldens pytest -m golden -q
```

This runs the whole pipeline for real — transcription, MediaPipe face
tracking, libass burn-in, exports — against every golden, with only the
language model's answer scripted. Expect several minutes per golden. It
asserts: every stage completes; crop paths tile their clips and stay inside
the frame; a subject is tracked where the scenario demands it; the facecam
verdict matches the scenario; renders exist at 1:1 and 16:9 (plus a PiP
render when a facecam is found) with the right shape, duration, audio, and
actual non-blank picture.

Each golden is *copied* into the run's throwaway home, so expect roughly the
size of your golden set in free disk — on a small tmpfs, point pytest's
basetemp somewhere with headroom (`--basetemp`).

A changed golden (sha256 mismatch) fails the run before it starts. The set is
the set; if a video must be replaced, acquire it under a new name.

## The manual walk — the actual acceptance decision

Export the clips through the review UI (or use the renders the pipeline
produced), watch them full-screen, and check every line. This is the part no
test can do.

**Every render, every scenario**

- [ ] Plays in a browser and on a phone; audio in sync with mouths.
- [ ] Captions are readable against the actual background, in every style you ship.
- [ ] No clipping or dropout in the audio.

**solo-speaker**

- [ ] The crop tracks the speaker smoothly — no jitter, no sudden jumps.
- [ ] The face stays inside safe margins for the whole clip; never half-cropped.
- [ ] Shot boundaries don't snap the framing distractingly.

**multi-speaker**

- [ ] The crop lands on whoever is speaking — judge against the audio.
- [ ] Nobody is cut out mid-sentence; too-far-apart subjects get the fitted frame over a blurred fill instead of losing someone.
- [ ] Speaker handoffs look deliberate, not strobe-like.

**facecam-stream-vod**

- [ ] Detection's box actually covers the streamer's face (check it in the review page before exporting).
- [ ] The PiP export shows the full screen behind a blurred fit — no content cropped away.
- [ ] The face inset is readable, parked in your chosen corner, and covers nothing important.
- [ ] Captions stay readable over both the background and the inset.
- [ ] Try a different corner and a different inset size; the overlay moves and scales correctly.

**screen-content**

- [ ] Text on screen stays legible after the 9:16 crop — no half-cut lines of code or slide headers.
- [ ] Over-wide content gets the fitted frame over a blurred fill, not a bad crop.
- [ ] Captions don't cover the region you're actually trying to read.

## Recording a sign-off

When the automated pass is green and you have walked the checklist: note the
date, the commit under test, and the output of `autoclip goldens list
--verify` — that summary is what turns "validated" into a defensible claim,
and it belongs in the release notes when v0.1.0 is tagged.
