import { useCallback, useEffect, useRef, useState } from 'react'

import {
  formatTimecode,
  type CaptionAlignment,
  type CropPath,
  type EffectiveCaption,
  type FacecamBox,
  type StyleOverrides,
  type Word,
} from '../api'

const VOLUME_KEY = 'autoclip.volume'

/** Output shapes, matching autoclip.pipeline.export.RATIOS. */
const ASPECTS: Record<string, [number, number]> = {
  '9:16': [9, 16],
  '1:1': [1, 1],
  '16:9': [16, 9],
}

/** Tallest the preview may be, so the transport row stays on screen. */
const MAX_HEIGHT_VH = 62

function readStoredVolume(): number {
  const stored = Number(window.localStorage.getItem(VOLUME_KEY))
  return Number.isFinite(stored) && stored > 0 && stored <= 1 ? stored : 1
}

/**
 * 9:16 preview of one clip, with a CSS approximation of the burned captions.
 *
 * The approximation is deliberate: rendering the real ASS would mean shipping a
 * subtitle engine to the browser. What matters at review time is timing, word
 * grouping, and whether the style reads at all — the final look comes from
 * libass at export.
 */
export function ClipPlayer({
  src,
  startS,
  endS,
  words,
  ratio,
  cropPath,
  cropNudge = 0,
  centreCrop = false,
  caption,
  captionGeometry,
  captionOverrides,
  onCaptionPosition,
  onCaptionSize,
  pip,
}: {
  src: string
  startS: number
  endS: number
  words: Word[]
  ratio: string
  cropPath?: CropPath | null
  cropNudge?: number
  centreCrop?: boolean
  /** Resolved caption snapshot (clip.effective_caption); null draws nothing. */
  caption?: EffectiveCaption | null
  /**
   * The clip's stored overrides. Only consulted for fields the snapshot does
   * not carry yet (the box colours), so a picker the user just moved still
   * moves the preview.
   */
  captionOverrides?: StyleOverrides | null
  /** Stored per-clip geometry; null ratios mean centred / preset default. */
  captionGeometry?: { xRatio: number | null; marginRatio: number | null }
  /** Drag release — both ratios at once, per clip (contract §2). */
  onCaptionPosition?: (patch: { xRatio: number | null; marginRatio: number | null }) => void
  /** Resize handle release — writes style_overrides.size_ratio (contract §1). */
  onCaptionSize?: (sizeRatio: number) => void
  /** Facecam inset preview; null renders the normal tracked crop. */
  pip?: { box: FacecamBox; corner: string; size: number } | null
}) {
  const video = useRef<HTMLVideoElement>(null)
  const pipBg = useRef<HTMLVideoElement>(null)
  const pipInset = useRef<HTMLVideoElement>(null)
  /** The preview frame — the reference for every caption gesture. */
  const frame = useRef<HTMLDivElement>(null)
  const [playing, setPlaying] = useState(false)
  const [time, setTime] = useState(startS)
  const [volume, setVolume] = useState(readStoredVolume)
  const [muted, setMuted] = useState(false)
  const [nativeControls, setNativeControls] = useState(false)
  const [audioCheck, setAudioCheck] = useState<AudioCheck | null>(null)
  const [checking, setChecking] = useState(false)

  const runAudioCheck = async () => {
    const element = video.current
    if (!element) return
    setChecking(true)
    setAudioCheck(null)
    try {
      setAudioCheck(await measureOutputLevel(element))
    } catch (error) {
      setAudioCheck({ ok: false, peakDb: null, detail: String(error) })
    } finally {
      setChecking(false)
      setPlaying(!element.paused)
    }
  }

  // Kept in sync imperatively: volume and muted are element properties, not
  // attributes, so React won't apply them from JSX on later renders.
  useEffect(() => {
    const element = video.current
    if (!element) return
    element.volume = volume
    element.muted = muted
    window.localStorage.setItem(VOLUME_KEY, String(volume))
  }, [volume, muted])

  // Re-seek whenever the clip or its trim changes, so the preview always starts
  // where the export will.
  useEffect(() => {
    const element = video.current
    if (!element) return
    element.currentTime = startS
    if (pipBg.current) pipBg.current.currentTime = startS
    if (pipInset.current) pipInset.current.currentTime = startS
    setTime(startS)
    setPlaying(false)
    element.pause()
    pipBg.current?.pause()
    pipInset.current?.pause()
  }, [src, startS])

  const onTimeUpdate = useCallback(() => {
    const element = video.current
    if (!element) return
    if (element.currentTime >= endS) {
      element.pause()
      element.currentTime = startS
      for (const other of [pipBg.current, pipInset.current]) {
        if (other) {
          other.pause()
          other.currentTime = startS
        }
      }
      setPlaying(false)
      setTime(startS)
      return
    }
    setTime(element.currentTime)
  }, [endS, startS])

  const toggle = () => {
    const element = video.current
    if (!element) return
    if (element.paused) {
      if (element.currentTime < startS || element.currentTime >= endS) {
        element.currentTime = startS
        if (pipBg.current) pipBg.current.currentTime = startS
        if (pipInset.current) pipInset.current.currentTime = startS
      }
      void element.play()
      void pipBg.current?.play()
      void pipInset.current?.play()
      setPlaying(true)
    } else {
      element.pause()
      pipBg.current?.pause()
      pipInset.current?.pause()
      setPlaying(false)
    }
  }

  const elapsed = Math.max(0, time - startS)
  const duration = Math.max(0.01, endS - startS)

  const cropStyle = cropWindowStyle(cropPath, elapsed, cropNudge, centreCrop)
  const [aspectW, aspectH] = ASPECTS[ratio] ?? ASPECTS['9:16']
  // Height alone can't bound the box: with width:100% and an aspect-ratio, a
  // max-height clamp shortens the element without narrowing it, so the rendered
  // shape stops matching the ratio — a 9:16 preview ends up looking square.
  // Deriving a matching max-width makes the box shrink along both axes instead.
  const maxWidth = `${((MAX_HEIGHT_VH * aspectW) / aspectH).toFixed(3)}vh`

  return (
    <div className="mx-auto flex w-full flex-col items-stretch" style={{ maxWidth }}>
      <div
        ref={frame}
        className="relative w-full overflow-hidden bg-ink-850"
        // `size` containment makes cqh below mean "this frame's height", so the
        // caption sizes to the same fraction of the frame the renderer burns.
        style={{ aspectRatio: `${aspectW} / ${aspectH}`, containerType: 'size' }}
        onClick={toggle}
        role="button"
        tabIndex={0}
        aria-label={playing ? 'Pause' : 'Play'}
        onKeyDown={(e) => {
          if (e.key === ' ' || e.key === 'Enter') {
            e.preventDefault()
            toggle()
          }
        }}
      >
        {pip && cropPath ? (
          <PipLayers
            src={src}
            mainRef={video}
            bgRef={pipBg}
            insetRef={pipInset}
            onTimeUpdate={onTimeUpdate}
            nativeControls={nativeControls}
            pip={pip}
            sourceW={cropPath.source_width}
            sourceH={cropPath.source_height}
          />
        ) : (
          <video
            ref={video}
            src={src}
            className={cropStyle ? 'absolute max-w-none' : 'size-full object-cover'}
            style={cropStyle ?? undefined}
            onTimeUpdate={onTimeUpdate}
            preload="auto"
            playsInline
            controls={nativeControls}
          />
        )}

        <CaptionOverlay
          words={words}
          time={time}
          caption={caption ?? null}
          geometry={captionGeometry}
          overrides={captionOverrides}
          frame={frame}
          onPosition={onCaptionPosition}
          onSize={onCaptionSize}
        />

        {!playing && (
          <div className="pointer-events-none absolute inset-0 grid place-items-center">
            <span className="grid size-16 place-items-center rounded-full bg-ink-900/70 pl-1 text-2xl text-ink-100 backdrop-blur-[2px]">
              ▶
            </span>
          </div>
        )}
      </div>

      <div className="mt-3 flex w-full items-center gap-4">
        <button onClick={toggle} className="btn btn-quiet -ml-1 w-14 justify-start">
          {playing ? 'Pause' : 'Play'}
        </button>

        <VolumeControl
          volume={volume}
          muted={muted}
          onVolume={(next) => {
            setVolume(next)
            // Dragging the slider up is an unambiguous "I want to hear this".
            if (next > 0) setMuted(false)
          }}
          onToggleMute={() => setMuted((current) => !current)}
        />

        <div className="h-px flex-1 bg-ink-800">
          <div
            className="h-px origin-left bg-sodium-500"
            style={{ transform: `scaleX(${elapsed / duration})` }}
          />
        </div>
        <span className="numeric text-xs text-ink-500">
          {formatTimecode(elapsed)} / {formatTimecode(duration)}
        </span>
      </div>

      {(muted || volume === 0) && (
        <p className="mt-2 text-xs text-sodium-500">
          Audio is muted — click the speaker to unmute.
        </p>
      )}

      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1">
        <button onClick={runAudioCheck} disabled={checking} className="btn btn-quiet -ml-1">
          {checking ? 'Listening…' : 'Test audio'}
        </button>
        <button
          onClick={() => setNativeControls((current) => !current)}
          className="btn btn-quiet"
        >
          {nativeControls ? 'Hide browser controls' : 'Browser controls'}
        </button>
      </div>

      {audioCheck && (
        <p
          className={`mt-1 max-w-prose text-xs leading-relaxed ${
            audioCheck.ok ? 'text-ink-400' : 'text-sodium-500'
          }`}
        >
          {audioCheck.detail}
        </p>
      )}
    </div>
  )
}

interface AudioCheck {
  ok: boolean
  peakDb: number | null
  detail: string
}

/**
 * Measure the real signal level leaving the video element.
 *
 * "Is it muted?" is otherwise unanswerable from inside the page: a muted tab, a
 * silenced app in the OS mixer, and audio routed to a disconnected output all
 * look identical, and none of them are distinguishable from a bug in here.
 *
 * ``captureStream`` taps the element's output rather than rerouting it, so
 * measuring cannot itself cause the silence being investigated. The analyser is
 * deliberately never connected to the context destination — doing so would play
 * the audio a second time.
 */
async function measureOutputLevel(element: HTMLVideoElement): Promise<AudioCheck> {
  const capture =
    (element as HTMLVideoElement & { captureStream?: () => MediaStream }).captureStream ??
    (element as HTMLVideoElement & { mozCaptureStream?: () => MediaStream }).mozCaptureStream

  if (!capture) {
    return {
      ok: false,
      peakDb: null,
      detail: "This browser can't measure audio output. Try the browser controls instead.",
    }
  }

  const wasPaused = element.paused
  if (wasPaused) await element.play()

  const stream = capture.call(element)
  const tracks = stream.getAudioTracks()
  if (tracks.length === 0) {
    if (wasPaused) element.pause()
    return {
      ok: false,
      peakDb: null,
      detail: 'This video exposes no audio track at all — that is a problem in AutoClip.',
    }
  }

  const context = new AudioContext()
  const analyser = context.createAnalyser()
  analyser.fftSize = 2048
  context.createMediaStreamSource(stream).connect(analyser)

  const samples = new Float32Array(analyser.fftSize)
  let peak = 0
  for (let i = 0; i < 24; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 50))
    analyser.getFloatTimeDomainData(samples)
    for (const sample of samples) peak = Math.max(peak, Math.abs(sample))
  }

  await context.close()
  if (wasPaused) element.pause()

  if (peak <= 0.005) {
    return {
      ok: false,
      peakDb: null,
      detail:
        'No signal is leaving the player. Either the clip really is silent, or the ' +
        'player is muted — check the speaker icon above.',
    }
  }

  const peakDb = 20 * Math.log10(peak)
  return {
    ok: true,
    peakDb,
    detail:
      `Audio is leaving the player at ${peakDb.toFixed(1)} dBFS — the app is producing sound. ` +
      'If you still hear nothing, it is between the browser and your speakers: right-click ' +
      'this tab and check for "Unmute site", then check Windows Volume Mixer and your ' +
      'output device.',
  }
}

/**
 * Position the source video so the preview box shows the crop the renderer will.
 *
 * Everything is expressed as a percentage of the crop window, which makes it
 * independent of how large the preview happens to be drawn. Returns null when
 * there is no crop path — the caller then falls back to a centre crop, which is
 * what the renderer does in that case too.
 *
 * The nudge/lock math mirrors shift_crop_path and the centre lock on the
 * server, so the preview and the export agree.
 */
function cropWindowStyle(
  cropPath: CropPath | null | undefined,
  elapsed: number,
  nudge = 0,
  centre = false,
): React.CSSProperties | null {
  if (!cropPath || cropPath.segments.length === 0) return null

  const segment =
    cropPath.segments.find((s) => elapsed >= s.start_s && elapsed < s.end_s) ??
    cropPath.segments[cropPath.segments.length - 1]

  // A fitted segment shows the whole frame over a blur rather than cropping.
  // Letting it fall through to object-contain is closer than any crop would be.
  if (segment.fit) return null

  const { source_width: sourceW, source_height: sourceH } = cropPath

  let x: number
  let y: number
  if (centre) {
    x = (sourceW - segment.width) / 2
    y = (sourceH - segment.height) / 2
  } else {
    const interpolated = interpolate(segment.keyframes, elapsed)
    const travel = sourceW - segment.width
    x = travel > 0 ? Math.min(travel, Math.max(0, interpolated.x + nudge * travel)) : interpolated.x
    y = interpolated.y
  }

  return {
    width: `${(sourceW / segment.width) * 100}%`,
    height: `${(sourceH / segment.height) * 100}%`,
    left: `${(-x / segment.width) * 100}%`,
    top: `${(-y / segment.height) * 100}%`,
  }
}

/**
 * Picture-in-picture preview: full-frame screen over a blurred fill, with the
 * facecam box as an inset — mirroring the export's PiP filtergraph.
 *
 * Three elements share one source; only the main one carries time updates and
 * audio. Sync drift between them is irrelevant at review fidelity.
 */
function PipLayers({
  src,
  mainRef,
  bgRef,
  insetRef,
  onTimeUpdate,
  nativeControls,
  pip,
  sourceW,
  sourceH,
}: {
  src: string
  mainRef: React.MutableRefObject<HTMLVideoElement | null>
  bgRef: React.MutableRefObject<HTMLVideoElement | null>
  insetRef: React.MutableRefObject<HTMLVideoElement | null>
  onTimeUpdate: () => void
  nativeControls: boolean
  pip: { box: FacecamBox; corner: string; size: number }
  sourceW: number
  sourceH: number
}) {
  const { box } = pip
  // Reuse the crop math with a single static segment: the inset shows exactly
  // the detected facecam box, like the export's crop filter does.
  const insetStyle = cropWindowStyle(
    {
      source_width: sourceW,
      source_height: sourceH,
      segments: [
        {
          start_s: 0,
          end_s: 1e9,
          width: Math.round(box.width),
          height: Math.round(box.height),
          keyframes: [{ t: 0, x: box.x, y: box.y }],
          strategy: 'general',
          zoom: 0,
          fit: false,
        },
      ],
    },
    0,
  )
  const margin = '2.2%'
  const corner: Record<string, React.CSSProperties> = {
    tl: { top: margin, left: margin },
    tr: { top: margin, right: margin },
    bl: { bottom: margin, left: margin },
    br: { bottom: margin, right: margin },
  }

  return (
    <>
      <video
        ref={bgRef}
        src={src}
        muted
        preload="auto"
        playsInline
        className="absolute inset-0 size-full object-cover"
        style={{ filter: 'blur(24px) brightness(0.7)', transform: 'scale(1.1)' }}
      />
      <video
        ref={mainRef}
        src={src}
        preload="auto"
        playsInline
        controls={nativeControls}
        onTimeUpdate={onTimeUpdate}
        className="absolute inset-0 size-full object-contain"
      />
      <div
        className="absolute overflow-hidden rounded-md ring-1 ring-white/30"
        style={{
          width: `${pip.size * 100}%`,
          aspectRatio: `${box.width} / ${box.height}`,
          ...(corner[pip.corner] ?? corner.tr),
        }}
      >
        <video
          ref={insetRef}
          src={src}
          muted
          preload="auto"
          playsInline
          className="absolute max-w-none"
          style={insetStyle ?? undefined}
        />
      </div>
    </>
  )
}

/** Piecewise-linear lookup, matching croppath.axis_expression on the server. */function interpolate(
  keyframes: { t: number; x: number; y: number }[],
  t: number,
): { x: number; y: number } {
  if (keyframes.length === 0) return { x: 0, y: 0 }
  if (keyframes.length === 1) return { x: keyframes[0].x, y: keyframes[0].y }

  if (t <= keyframes[0].t) return { x: keyframes[0].x, y: keyframes[0].y }
  const last = keyframes[keyframes.length - 1]
  if (t >= last.t) return { x: last.x, y: last.y }

  for (let i = 0; i < keyframes.length - 1; i += 1) {
    const a = keyframes[i]
    const b = keyframes[i + 1]
    if (t >= a.t && t <= b.t) {
      const span = b.t - a.t
      const ratio = span > 0 ? (t - a.t) / span : 0
      return { x: a.x + (b.x - a.x) * ratio, y: a.y + (b.y - a.y) * ratio }
    }
  }

  return { x: last.x, y: last.y }
}

/**
 * Mute toggle and volume slider.
 *
 * Not optional chrome: without it there is no way to see whether the preview is
 * silent because it's muted or because something upstream is wrong, and no way
 * to do anything about it. The slider stays visible rather than hiding behind a
 * hover, because "is this thing muted?" is a question you ask at a glance.
 */
function VolumeControl({
  volume,
  muted,
  onVolume,
  onToggleMute,
}: {
  volume: number
  muted: boolean
  onVolume: (value: number) => void
  onToggleMute: () => void
}) {
  const silent = muted || volume === 0

  return (
    <div className="flex items-center gap-2">
      <button
        onClick={onToggleMute}
        className={`btn btn-quiet px-1 ${silent ? 'text-sodium-500' : ''}`}
        aria-label={silent ? 'Unmute' : 'Mute'}
        title={silent ? 'Unmute' : 'Mute'}
      >
        <SpeakerIcon silent={silent} level={volume} />
      </button>
      <input
        type="range"
        min={0}
        max={1}
        step={0.05}
        value={silent ? 0 : volume}
        onChange={(e) => onVolume(Number(e.target.value))}
        aria-label="Volume"
        className="h-1 w-20 cursor-pointer appearance-none rounded-full bg-ink-700 accent-sodium-500"
      />
    </div>
  )
}

function SpeakerIcon({ silent, level }: { silent: boolean; level: number }) {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path
        d="M11 5 6 9H3v6h3l5 4V5Z"
        fill="currentColor"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      {silent ? (
        <path
          d="m16 9 5 6m0-6-5 6"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
        />
      ) : (
        <>
          <path
            d="M15.5 9.5a3.5 3.5 0 0 1 0 5"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
          />
          {level > 0.5 && (
            <path
              d="M18.5 7a7 7 0 0 1 0 10"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
            />
          )}
        </>
      )}
    </svg>
  )
}

/* -------------------------------------------------------------------------
   Caption preview — the CSS mirror of the burn-in
   ------------------------------------------------------------------------- */

/** Contract §2: a release within ±0.04 of the midpoint snaps to centred. */
const CENTRE_SNAP = 0.04
/** Contract §2 clamp range for the vertical inset. */
const MARGIN_MIN = 0
const MARGIN_MAX = 0.8
/** Contract §1 clamp range for size_ratio (fraction of frame height). */
const SIZE_MIN = 0.02
const SIZE_MAX = 0.12
/** Where a caption sits when the anchor came back without an x (see below). */
const ANCHOR_FALLBACK = 0.06
/** The preset default the renderer falls back to when nothing sets a margin. */
const DEFAULT_MARGIN_RATIO = 0.24
/** Default group size if a snapshot predates the max_words field. */
const DEFAULT_MAX_WORDS = 3

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value))
}

/**
 * Contract §2's alignment maths: either side of the midpoint the caption is
 * anchored by its edge, and exactly at the midpoint it is centred.
 */
function alignmentFor(x: number): CaptionAlignment {
  if (x < 0.5) return 'left'
  if (x > 0.5) return 'right'
  return 'center'
}

/** ASS alpha is inverted: 0 is opaque, 255 is fully transparent. */
function withAssAlpha(
  hex: string | null | undefined,
  alpha: number | null | undefined,
): string | undefined {
  const match = /^#?([0-9a-f]{6})$/i.exec(hex ?? '')
  if (!match) return undefined
  const value = match[1]
  const red = parseInt(value.slice(0, 2), 16)
  const green = parseInt(value.slice(2, 4), 16)
  const blue = parseInt(value.slice(4, 6), 16)
  const opacity = 1 - clamp(alpha ?? 0, 0, 255) / 255
  return `rgba(${red}, ${green}, ${blue}, ${opacity.toFixed(3)})`
}

/**
 * CSS stand-ins for the renderer's font registry (contract §5).
 *
 * The real faces live in backend/autoclip/assets/fonts and are burned in by
 * libass; nothing serves them to the browser and this wave adds no dependencies,
 * so each key is approximated with the closest stack the browser plus the
 * bundled Archivo variable face can offer. Letterform and weight land close —
 * metric-exact it is not, and cannot be without shipping the files.
 */
const FONT_LOOKS: Record<string, { stack: string; weight: number }> = {
  anton: { stack: 'Anton, Impact, "Arial Narrow", sans-serif', weight: 400 },
  inter: { stack: 'Inter, system-ui, sans-serif', weight: 600 },
  archivo: { stack: 'Archivo, Impact, sans-serif', weight: 900 },
  'archivo black': { stack: 'Archivo, Impact, sans-serif', weight: 900 },
  'bebas neue': { stack: '"Bebas Neue", "Arial Narrow", Impact, sans-serif', weight: 400 },
  'lilita one': { stack: '"Lilita One", Archivo, Impact, sans-serif', weight: 400 },
  'passion one': { stack: '"Passion One", Archivo, Impact, sans-serif', weight: 700 },
}

/** Registry keys arrive as slugs or display names; normalise both. */
function fontLook(key: string | null | undefined): { stack: string; weight: number } {
  const normalised = (key ?? '').trim().toLowerCase().replace(/[_-]+/g, ' ')
  return FONT_LOOKS[normalised] ?? { stack: 'Archivo, system-ui, sans-serif', weight: 700 }
}

/**
 * The live caption preview and the gesture layer over it.
 *
 * Everything visual is read straight from the server's `effective_caption`
 * (contract §8.1) — this component resolves nothing. Geometry is per clip now:
 * a drag releases `caption_x_ratio` + `caption_margin_ratio` together, and a
 * release near the middle reports `null`, which the renderer reads as centred.
 * The resize handle keeps its old gesture but reports a `size_ratio` override.
 */
function CaptionOverlay({
  words,
  time,
  caption,
  geometry,
  overrides,
  frame,
  onPosition,
  onSize,
}: {
  words: Word[]
  time: number
  caption: EffectiveCaption | null
  geometry?: { xRatio: number | null; marginRatio: number | null }
  overrides?: StyleOverrides | null
  frame: React.MutableRefObject<HTMLDivElement | null>
  onPosition?: (patch: { xRatio: number | null; marginRatio: number | null }) => void
  onSize?: (sizeRatio: number) => void
}) {
  const movedRef = useRef(false)
  const [drag, setDrag] = useState<{
    originX: number
    originY: number
    startX: number
    startMargin: number
  } | null>(null)
  const [offset, setOffset] = useState({ x: 0, y: 0 })
  const [resize, setResize] = useState<{ startY: number; base: number } | null>(null)
  const [scale, setScale] = useState(1)

  if (!caption || words.length === 0) return null

  const groups = groupWords(words, caption.maxWords ?? DEFAULT_MAX_WORDS)
  const active = groups.find((group) => time >= group[0].start && time <= group[group.length - 1].end)
  if (!active) return null

  // Resolution order mirrors the server's, highest wins, and the snapshot is
  // already the result of it — the stored per-clip values are only a backstop
  // for a payload that predates effective_caption.
  const restingMargin = clamp(
    caption.margin_ratio ?? caption.marginRatio ?? geometry?.marginRatio ?? DEFAULT_MARGIN_RATIO,
    MARGIN_MIN,
    MARGIN_MAX,
  )

  // The anchor is normally in the payload as x_ratio. If only the alignment came
  // back (a preset layout x with a null clip x), the exact anchor is unknowable
  // here, so it is approximated on the side the renderer will use rather than
  // being re-centred — centring would be the bigger lie of the two.
  const storedX = geometry?.xRatio ?? null
  const reportedX = caption.x_ratio ?? storedX
  const restingX =
    reportedX != null
      ? clamp(reportedX, 0, 1)
      : caption.alignment === 'left'
        ? ANCHOR_FALLBACK
        : caption.alignment === 'right'
          ? 1 - ANCHOR_FALLBACK
          : 0.5

  const liveX = drag ? clamp(drag.startX + offset.x, 0, 1) : restingX
  const liveMargin = drag ? clamp(drag.startMargin - offset.y, MARGIN_MIN, MARGIN_MAX) : restingMargin
  // Magnetic: inside the window the caption sits on the centre line, which is
  // exactly where releasing there will put it.
  const snapped = drag !== null && Math.abs(liveX - 0.5) <= CENTRE_SNAP
  const x = snapped ? 0.5 : liveX
  const alignment = alignmentFor(x)

  const baseSize = clamp(caption.sizeRatio ?? 0.05, SIZE_MIN, SIZE_MAX)
  const size = resize ? clamp(resize.base * scale, SIZE_MIN, SIZE_MAX) : baseSize
  const look = fontLook(caption.font)
  const animation = caption.animation ?? 'scale'
  const boxed = Boolean(caption.boxed)
  const outlineWidth = caption.outlineWidth ?? 0
  const outline = caption.outline ?? '#000000'
  const primary = caption.primary ?? '#ffffff'
  // The snapshot does not carry the box colours yet, so the stored override is
  // the best available source; the renderer's own default is a near-opaque box.
  const boxColour = caption.box_colour ?? overrides?.box_colour ?? '#000000'
  const boxAlpha = caption.box_alpha ?? overrides?.box_alpha ?? 40

  const frameBox = () => frame.current?.getBoundingClientRect() ?? null

  const beginDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!onPosition || resize) return
    event.currentTarget.setPointerCapture(event.pointerId)
    movedRef.current = false
    setOffset({ x: 0, y: 0 })
    setDrag({
      originX: event.clientX,
      originY: event.clientY,
      startX: restingX,
      startMargin: restingMargin,
    })
  }

  const trackDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!drag || resize) return
    const box = frameBox()
    if (!box || box.width === 0 || box.height === 0) return
    const dx = event.clientX - drag.originX
    const dy = event.clientY - drag.originY
    if (Math.abs(dx) > 4 || Math.abs(dy) > 4) movedRef.current = true
    setOffset({ x: dx / box.width, y: dy / box.height })
  }

  const endDrag = () => {
    if (!drag) return
    const moved = movedRef.current
    const nextX = Math.abs(liveX - 0.5) <= CENTRE_SNAP ? null : Math.round(liveX * 1000) / 1000
    const nextMargin = Math.round(liveMargin * 1000) / 1000
    setDrag(null)
    setOffset({ x: 0, y: 0 })
    if (!moved) return
    const xChanged = nextX === null ? restingX !== 0.5 : Math.abs(nextX - restingX) > 0.001
    if (!xChanged && Math.abs(nextMargin - restingMargin) < 0.005) return
    onPosition?.({ xRatio: nextX, marginRatio: nextMargin })
  }

  const endResize = () => {
    if (!resize) return
    const next = Math.round(clamp(resize.base * scale, SIZE_MIN, SIZE_MAX) * 10000) / 10000
    setResize(null)
    setScale(1)
    if (movedRef.current && Math.abs(next - resize.base) > 0.0005) onSize?.(next)
  }

  // Shared by the overlay (move release) and the resize handle, which captures
  // the pointer so its own release never bubbles to the overlay.
  const releasePointer = () => {
    if (resize) endResize()
    else endDrag()
  }

  return (
    <>
      {/* Centre guide: faint while approaching, solid once the release would
          snap. It only exists while dragging, so it never dresses the preview. */}
      {drag && (
        <div
          aria-hidden
          className={[
            'pointer-events-none absolute inset-y-0 left-1/2 w-px -translate-x-1/2 transition-opacity duration-150',
            snapped ? 'bg-sodium-500 opacity-100' : 'bg-ink-100/40 opacity-50',
          ].join(' ')}
        />
      )}

      <div
        className="pointer-events-auto absolute inset-x-0 touch-none select-none"
        style={{
          bottom: `${liveMargin * 100}%`,
          // Contract §2's CSS mirror: edge-anchored text with an inset of the
          // same fraction the renderer turns into MarginL/MarginR. No inset at
          // the centre, because BOTTOM_CENTER carries no margins.
          paddingLeft: alignment === 'left' ? `${x * 100}%` : undefined,
          paddingRight: alignment === 'right' ? `${(1 - x) * 100}%` : undefined,
          textAlign: alignment,
          cursor: onPosition ? 'move' : undefined,
        }}
        title={
          onPosition
            ? 'Drag to place · corner handle resizes · double-click re-centres'
            : undefined
        }
        onPointerDown={beginDrag}
        onPointerMove={trackDrag}
        onPointerUp={releasePointer}
        onPointerCancel={() => {
          setDrag(null)
          setOffset({ x: 0, y: 0 })
          setResize(null)
          setScale(1)
        }}
        onClickCapture={(e) => {
          if (movedRef.current) {
            e.stopPropagation()
            movedRef.current = false
          }
        }}
        onDoubleClick={(e) => {
          e.stopPropagation()
          onPosition?.({ xRatio: null, marginRatio: null })
        }}
      >
        <p
          // Inline-block so the caption box hugs the text like libass draws it,
          // instead of painting a full-width band. Anchoring is unaffected: the
          // overlay's own padding and text-align place the line boxes (§2).
          // max-w-full keeps an oversized group wrapping at the frame edge; a
          // group long enough to wrap gets a full-width box, which is the closest
          // a single block background can get to libass's per-line rectangle.
          className="relative inline-block max-w-full leading-[1.15]"
          style={{
            fontFamily: look.stack,
            // cqh is this frame's height (the preview box declares size
            // containment), so the ratio is the renderer's ratio.
            fontSize: `${(size * 100).toFixed(2)}cqh`,
            fontWeight: look.weight,
            textTransform: caption.allCaps ? 'uppercase' : 'none',
            color: primary,
            textShadow: boxed
              ? undefined
              : `0 0 ${outlineWidth}px ${outline}, 0 0 ${outlineWidth * 2}px ${outline}`,
            background: boxed
              ? (withAssAlpha(boxColour, boxAlpha) ?? 'rgba(0, 0, 0, 0.78)')
              : undefined,
            padding: boxed ? '0.15em 0.4em' : undefined,
          }}
        >
          {active.map((word, index) => {
            const isActive = time >= word.start && time <= word.end
            // `none` is a static caption: no per-word colour, no pop. A null
            // accent disables the highlight the same way (contract §1).
            const highlighted = animation !== 'none' && isActive && Boolean(caption.accent)
            const pops = highlighted && animation === 'scale'
            return (
              <span
                key={`${word.start}-${index}`}
                style={{
                  color: highlighted ? (caption.accent ?? undefined) : undefined,
                  display: 'inline-block',
                  transform: pops ? 'scale(1.08)' : undefined,
                  transition:
                    animation === 'none'
                      ? undefined
                      : 'transform 120ms cubic-bezier(0.16, 1, 0.3, 1), color 120ms linear',
                  marginInline: '0.14em',
                }}
              >
                {caption.allCaps ? word.text.toUpperCase() : word.text}
              </span>
            )
          })}
        </p>

        {/* Chrome, not content: it hangs off the overlay rather than the text so
            it stays inside the frame even when the caption spans the full width
            (centred has no margins, and the frame clips its overflow). */}
        {onSize && (
          <span
            role="button"
            aria-label="Resize captions"
            title={`Drag to resize — ${(baseSize * 100).toFixed(1)}% of frame height`}
            className="absolute right-0 bottom-0 size-4 cursor-nwse-resize rounded-[3px] border border-sodium-500/80 bg-ink-900/80"
            onPointerDown={(e) => {
              e.stopPropagation()
              e.currentTarget.setPointerCapture(e.pointerId)
              movedRef.current = false
              setScale(1)
              setResize({ startY: e.clientY, base: baseSize })
            }}
            onPointerMove={(e) => {
              if (!resize) return
              const dy = e.clientY - resize.startY
              if (Math.abs(dy) > 4) movedRef.current = true
              setScale(clamp(1 + dy / 250, 0.2, 3))
            }}
            onPointerUp={(e) => {
              e.stopPropagation()
              releasePointer()
            }}
            onClick={(e) => e.stopPropagation()}
            onDoubleClick={(e) => e.stopPropagation()}
          />
        )}
      </div>
    </>
  )
}

function groupWords(words: Word[], maxWords: number): Word[][] {
  const groups: Word[][] = []
  let current: Word[] = []

  for (const [index, word] of words.entries()) {
    if (current.length > 0) {
      const gap = word.start - current[current.length - 1].end
      if (gap > 0.4 || current.length >= maxWords) {
        groups.push(current)
        current = []
      }
    }
    current.push(word)
    if (/[.!?…]$/.test(word.text.trim()) && index !== words.length - 1) {
      groups.push(current)
      current = []
    }
  }
  if (current.length > 0) groups.push(current)
  return groups
}
