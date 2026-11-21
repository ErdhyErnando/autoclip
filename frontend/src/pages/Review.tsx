import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import {
  api,
  captionForClip,
  formatBytes,
  formatDuration,
  type CaptionStyle,
  type Clip,
  type CropPath,
  type FacecamBox,
  type FontEntry,
  type Job,
  type Preset,
  type StyleOverrides,
  type Word,
} from '../api'
import { CaptionEditor } from '../components/CaptionEditor'
import { ClipPlayer } from '../components/ClipPlayer'
import { ErrorNote } from '../components/ErrorNote'
import { StylePanel } from '../components/StylePanel'
import { TabBar } from '../components/TabBar'
import { TrimBar } from '../components/TrimBar'

const RATIOS = ['9:16', '1:1', '16:9'] as const

type ReviewTab = 'style' | 'framing' | 'webcam'

/** Tab state is local to this page — the player and trim bar never move. */
const TABS: { id: ReviewTab; label: string }[] = [
  { id: 'style', label: 'Style' },
  { id: 'framing', label: 'Framing' },
  { id: 'webcam', label: 'Webcam' },
]

export function Review() {
  const { jobId } = useParams()
  const [job, setJob] = useState<Job | null>(null)
  const [clips, setClips] = useState<Clip[]>([])
  const [styles, setStyles] = useState<CaptionStyle[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [words, setWords] = useState<Word[]>([])
  const [cropPath, setCropPath] = useState<CropPath | null>(null)
  const [wordsDirty, setWordsDirty] = useState(false)
  const [savingWords, setSavingWords] = useState(false)
  const [exporting, setExporting] = useState<Set<string>>(new Set())
  const [error, setError] = useState<Error | null>(null)
  const [tab, setTab] = useState<ReviewTab>('style')
  // Font registry and presets belong to the backend lane: until it merges these
  // endpoints 404, and the controls dim rather than taking the page down.
  const [fonts, setFonts] = useState<FontEntry[]>([])
  const [fontsUnavailable, setFontsUnavailable] = useState(false)
  const [presets, setPresets] = useState<Preset[]>([])
  const [presetsUnavailable, setPresetsUnavailable] = useState(false)
  // Local slider draft so dragging doesn't PATCH per tick; commits on release.
  const [nudgeDraft, setNudgeDraft] = useState<number | null>(null)
  const [facecam, setFacecam] = useState<FacecamBox | null>(null)
  const [detecting, setDetecting] = useState(false)
  const [pipSizeDraft, setPipSizeDraft] = useState<number | null>(null)

  const loadPresets = useCallback(async () => {
    try {
      setPresets(await api.listPresets())
      setPresetsUnavailable(false)
    } catch {
      setPresets([])
      setPresetsUnavailable(true)
    }
  }, [])

  useEffect(() => {
    if (!jobId) return
    void Promise.all([api.getJob(jobId), api.listClips(jobId), api.captionStyles()])
      .then(([loadedJob, loadedClips, loadedStyles]) => {
        setJob(loadedJob)
        setClips(loadedClips)
        setStyles(loadedStyles)
        setSelectedId((current) => current ?? loadedClips[0]?.id ?? null)
      })
      .catch((err) => setError(err as Error))
  }, [jobId])

  useEffect(() => {
    void api
      .fonts()
      .then((loaded) => {
        setFonts(loaded)
        setFontsUnavailable(false)
      })
      .catch(() => setFontsUnavailable(true))
    void loadPresets()
  }, [loadPresets])

  const selected = useMemo(
    () => clips.find((clip) => clip.id === selectedId) ?? null,
    [clips, selectedId],
  )

  // The preview renders the server's resolved snapshot; the fallback inside
  // captionForClip only covers a backend that predates the field.
  const effective = useMemo(
    () => (selected ? captionForClip(selected, styles) : null),
    [selected, styles],
  )

  useEffect(() => {
    if (!selected) return
    setWordsDirty(false)
    setNudgeDraft(null)
    setPipSizeDraft(null)
    setFacecam(null)
    api
      .getClipWords(selected.id)
      .then(setWords)
      .catch(() => setWords([]))
    // 404 is expected for audio-only sources and jobs that never reframed; the
    // player falls back to a centre crop, matching what the renderer does.
    api
      .getCropPath(selected.id)
      .then(setCropPath)
      .catch(() => setCropPath(null))
    api
      .getFacecam(selected.id)
      .then(setFacecam)
      .catch(() => setFacecam(null))
  }, [selected?.id])

  const patchClip = useCallback((updated: Clip) => {
    setClips((current) => current.map((clip) => (clip.id === updated.id ? updated : clip)))
  }, [])

  const setStatus = async (clip: Clip, status: Clip['status']) => {
    try {
      patchClip(await api.patchClip(clip.id, { status }))
    } catch (err) {
      setError(err as Error)
    }
  }

  const commitTrim = async (start: number, end: number) => {
    if (!selected) return
    try {
      patchClip(await api.patchClip(selected.id, { start_s: start, end_s: end }))
      setWords(await api.getClipWords(selected.id))
    } catch (err) {
      setError(err as Error)
    }
  }

  const saveWords = async () => {
    if (!selected) return
    setSavingWords(true)
    try {
      patchClip(await api.patchCaptions(selected.id, { words }))
      setWordsDirty(false)
    } catch (err) {
      setError(err as Error)
    } finally {
      setSavingWords(false)
    }
  }

  const setStyle = async (clip: Clip, styleKey: string) => {
    try {
      patchClip(await api.patchCaptions(clip.id, { caption_style: styleKey }))
    } catch (err) {
      setError(err as Error)
    }
  }

  const setRatio = async (clip: Clip, ratio: string) => {
    try {
      patchClip(await api.patchCaptions(clip.id, { ratio }))
    } catch (err) {
      setError(err as Error)
    }
  }

  const commitNudge = async (clip: Clip, percent: number) => {
    setNudgeDraft(null)
    const nudge = Math.min(1, Math.max(-1, Math.round(percent) / 100))
    if (Math.abs(nudge - clip.crop_nudge) < 0.005) return
    try {
      patchClip(await api.patchCaptions(clip.id, { crop_nudge: nudge }))
    } catch (err) {
      setError(err as Error)
    }
  }

  const setCentreCrop = async (clip: Clip, locked: boolean) => {
    try {
      patchClip(await api.patchCaptions(clip.id, { centre_crop: locked }))
    } catch (err) {
      setError(err as Error)
    }
  }

  const setPip = async (
    clip: Clip,
    patch: { pip_enabled?: boolean; pip_corner?: string; pip_size?: number },
  ) => {
    try {
      patchClip(await api.patchCaptions(clip.id, patch))
    } catch (err) {
      setError(err as Error)
    }
  }

  const detectFacecam = async (clip: Clip) => {
    setDetecting(true)
    setError(null)
    try {
      setFacecam(await api.detectFacecam(clip.id))
    } catch (err) {
      setFacecam(null)
      setError(err as Error)
    } finally {
      setDetecting(false)
    }
  }

  /**
   * Style edits always send the whole overrides blob — the PATCH replaces it
   * wholesale (contract §3). Spreading the clip's own overrides first keeps
   * every untouched field inheriting from the base preset instead of being
   * frozen to whatever the resolved value happened to be.
   */
  const commitOverrides = async (clip: Clip, patch: Partial<StyleOverrides>) => {
    const next: StyleOverrides = { ...(clip.style_overrides ?? {}), ...patch }
    try {
      patchClip(await api.patchCaptions(clip.id, { style_overrides: next }))
    } catch (err) {
      setError(err as Error)
    }
  }

  /** Caption drag release: both per-clip geometry ratios at once (contract §2). */
  const commitCaptionPosition = async (
    clip: Clip,
    patch: { xRatio: number | null; marginRatio: number | null },
  ) => {
    try {
      patchClip(
        await api.patchCaptions(clip.id, {
          caption_x_ratio: patch.xRatio,
          caption_margin_ratio: patch.marginRatio,
        }),
      )
    } catch (err) {
      setError(err as Error)
    }
  }

  /** Resize release — a size_ratio override on this clip, nothing global. */
  const commitCaptionSize = async (clip: Clip, sizeRatio: number) => {
    await commitOverrides(clip, { size_ratio: sizeRatio })
  }

  const applyPreset = async (clip: Clip, presetId: string) => {
    try {
      patchClip(await api.applyPreset(presetId, clip.id))
    } catch (err) {
      setError(err as Error)
    }
  }

  /** Saving captures the clip as it stands — look and layout (contract §4). */
  const savePreset = async (clip: Clip, name: string) => {
    try {
      await api.createPreset({
        name,
        base_style: clip.caption_style,
        style_overrides: clip.style_overrides ?? null,
        layout: {
          caption_margin_ratio: clip.caption_margin_ratio ?? null,
          caption_x_ratio: clip.caption_x_ratio ?? null,
        },
      })
      await loadPresets()
    } catch (err) {
      setError(err as Error)
    }
  }

  const setDefaultPreset = async (presetId: string) => {
    try {
      await api.setDefaultPreset(presetId)
      await loadPresets()
    } catch (err) {
      setError(err as Error)
    }
  }

  const deletePreset = async (presetId: string) => {
    try {
      await api.deletePreset(presetId)
      await loadPresets()
    } catch (err) {
      setError(err as Error)
    }
  }

  const exportClip = async (clip: Clip) => {
    setExporting((current) => new Set(current).add(clip.id))
    setError(null)
    try {
      await api.exportClip(clip.id, clip.ratio, clip.caption_style, false, clip.crop_nudge, clip.centre_crop, {
        enabled: clip.pip_enabled,
        corner: clip.pip_corner,
        size: clip.pip_size,
      })
      patchClip(await api.getClip(clip.id))
    } catch (err) {
      setError(err as Error)
    } finally {
      setExporting((current) => {
        const next = new Set(current)
        next.delete(clip.id)
        return next
      })
    }
  }

  const exportKept = async () => {
    const targets = clips.filter((clip) => clip.status === 'kept')
    for (const clip of targets) await exportClip(clip)
  }

  const keptCount = clips.filter((clip) => clip.status === 'kept').length

  if (error && clips.length === 0) {
    return (
      <div className="max-w-3xl pt-24">
        <ErrorNote error={error} />
      </div>
    )
  }

  if (!job) return <p className="pt-24 text-sm text-ink-500">Loading…</p>

  return (
    <div className="pt-10">
      <div className="flex flex-wrap items-baseline justify-between gap-4 border-b border-ink-800 pb-5">
        <div className="min-w-0">
          <Link to="/" className="eyebrow transition-colors hover:text-sodium-500">
            ← All jobs
          </Link>
          <h1 className="mt-2 max-w-2xl truncate font-display text-[clamp(1.5rem,3vw,2.25rem)] leading-tight text-ink-100">
            {job.source?.title || 'Untitled'}
          </h1>
        </div>

        <div className="flex items-baseline gap-6">
          <span className="numeric text-xs text-ink-500">
            {clips.length} clips · {keptCount} kept
          </span>
          <button onClick={exportKept} disabled={keptCount === 0} className="btn btn-primary">
            Export kept
          </button>
        </div>
      </div>

      {error && (
        <div className="mt-6 max-w-3xl">
          <ErrorNote error={error} onDismiss={() => setError(null)} />
        </div>
      )}

      {clips.length === 0 ? (
        <EmptyState />
      ) : (
        <div className="mt-8 grid gap-x-10 gap-y-10 lg:grid-cols-[minmax(0,20rem)_minmax(0,26rem)_minmax(0,1fr)]">
          {/* Ranked list */}
          <section className="lg:max-h-[76vh] lg:overflow-y-auto lg:pr-1">
            <p className="eyebrow border-b border-ink-800 pb-2">Ranked</p>
            <ul>
              {clips.map((clip, index) => (
                <li key={clip.id}>
                  <ClipRow
                    clip={clip}
                    index={index}
                    selected={clip.id === selectedId}
                    onSelect={() => setSelectedId(clip.id)}
                    onKeep={() =>
                      setStatus(clip, clip.status === 'kept' ? 'candidate' : 'kept')
                    }
                    onDiscard={() =>
                      setStatus(clip, clip.status === 'discarded' ? 'candidate' : 'discarded')
                    }
                  />
                </li>
              ))}
            </ul>
          </section>

          {/* Player + trim */}
          <section className="space-y-8">
            {selected && jobId && (
              <>
                <ClipPlayer
                  src={api.mediaUrl(jobId)}
                  startS={selected.start_s}
                  endS={selected.end_s}
                  words={words}
                  ratio={selected.ratio}
                  cropPath={cropPath}
                  cropNudge={selected.crop_nudge}
                  centreCrop={selected.centre_crop}
                  caption={effective}
                  captionOverrides={selected.style_overrides ?? null}
                  captionGeometry={{
                    xRatio: selected.caption_x_ratio ?? null,
                    marginRatio: selected.caption_margin_ratio ?? null,
                  }}
                  onCaptionPosition={(patch) => void commitCaptionPosition(selected, patch)}
                  onCaptionSize={(size) => void commitCaptionSize(selected, size)}
                  pip={
                    selected.pip_enabled && facecam
                      ? {
                          box: facecam,
                          corner:
                            selected.pip_corner === 'auto'
                              ? facecam.corner
                              : selected.pip_corner,
                          size: selected.pip_size,
                        }
                      : null
                  }
                />
                <TrimBar
                  words={words}
                  startS={selected.start_s}
                  endS={selected.end_s}
                  originalStart={selected.start_s}
                  originalEnd={selected.end_s}
                  onCommit={commitTrim}
                />
              </>
            )}
          </section>

          {/* Editor + style */}
          <section className="space-y-10">
            {selected && (
              <>
                <div>
                  <p className="eyebrow">Why this clip</p>
                  <p className="mt-2 max-w-prose text-[0.9375rem] leading-relaxed text-ink-300">
                    {selected.reason || 'No rationale returned for this clip.'}
                  </p>
                  {selected.hook && (
                    <p className="mt-3 border-l-2 border-ink-700 pl-3 font-display text-lg italic text-ink-200">
                      “{selected.hook}”
                    </p>
                  )}
                </div>

                <CaptionEditor
                  words={words}
                  onChange={(next) => {
                    setWords(next)
                    setWordsDirty(true)
                  }}
                  onSave={saveWords}
                  saving={savingWords}
                  dirty={wordsDirty}
                />

                <div>
                  <TabBar tabs={TABS} value={tab} onChange={setTab} />

                  {/* All three panels stay mounted and hide with display:none,
                      so switching tabs cannot lose a gesture draft and the
                      framing and webcam blocks move under them unchanged. */}
                  <div className="mt-6">
                    <div className={tab === 'style' ? undefined : 'hidden'}>
                      <StylePanel
                        clip={selected}
                        styles={styles}
                        fonts={fonts}
                        fontsUnavailable={fontsUnavailable}
                        effective={effective}
                        presets={presets}
                        presetsUnavailable={presetsUnavailable}
                        onBaseStyle={(key) => void setStyle(selected, key)}
                        onOverride={(patch) => void commitOverrides(selected, patch)}
                        onApplyPreset={(id) => applyPreset(selected, id)}
                        onSavePreset={(name) => savePreset(selected, name)}
                        onSetDefaultPreset={setDefaultPreset}
                        onDeletePreset={deletePreset}
                      />
                    </div>

                    <div className={tab === 'framing' ? 'space-y-10' : 'hidden'}>
                      <div>
                        <p className="eyebrow border-b border-ink-800 pb-2">Aspect ratio</p>
                        <div className="mt-3 flex gap-2">
                          {RATIOS.map((ratio) => (
                            <button
                              key={ratio}
                              onClick={() => setRatio(selected, ratio)}
                              className={[
                                'numeric btn',
                                ratio === selected.ratio ? 'btn-primary' : 'btn-ghost',
                              ].join(' ')}
                            >
                              {ratio}
                            </button>
                          ))}
                        </div>
                      </div>

                      <div>
                        <div className="flex items-baseline justify-between border-b border-ink-800 pb-2">
                          <p className="eyebrow">Framing</p>
                          <span className="numeric text-xs text-ink-500">
                            {(nudgeDraft ?? selected.crop_nudge * 100) > 0 ? '+' : ''}
                            {Math.round(nudgeDraft ?? selected.crop_nudge * 100)}%
                          </span>
                        </div>
                        <input
                          type="range"
                          min={-100}
                          max={100}
                          step={1}
                          value={Math.round(nudgeDraft ?? selected.crop_nudge * 100)}
                          disabled={selected.centre_crop}
                          onChange={(e) => setNudgeDraft(Number(e.target.value))}
                          onPointerUp={(e) => commitNudge(selected, Number(e.currentTarget.value))}
                          onKeyUp={(e) => {
                            if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
                              commitNudge(selected, Number(e.currentTarget.value))
                            }
                          }}
                          aria-label="Shift crop window left or right"
                          className="mt-3 h-1 w-full cursor-pointer appearance-none rounded-full bg-ink-700 accent-sodium-500 disabled:opacity-40"
                        />
                        <div className="mt-1 flex justify-between text-[0.6875rem] text-ink-600">
                          <span>← left</span>
                          <span>auto-tracked</span>
                          <span>right →</span>
                        </div>
                        <div className="mt-2 flex items-center justify-between gap-3">
                          <label className="flex items-center gap-2 text-xs text-ink-300">
                            <input
                              type="checkbox"
                              checked={selected.centre_crop}
                              onChange={(e) => setCentreCrop(selected, e.target.checked)}
                              className="size-3.5 accent-sodium-500"
                            />
                            Lock to center
                          </label>
                          {selected.crop_nudge !== 0 && !selected.centre_crop && (
                            <button
                              onClick={() => commitNudge(selected, 0)}
                              className="btn btn-quiet text-xs"
                            >
                              Reset
                            </button>
                          )}
                        </div>
                        <p className="mt-1.5 text-xs leading-snug text-ink-500">
                          Shifts the tracked window on the next export. The preview above moves
                          with the slider.
                        </p>
                      </div>
                    </div>

                    <div className={tab === 'webcam' ? undefined : 'hidden'}>
                      <div>
                        <div className="flex items-baseline justify-between border-b border-ink-800 pb-2">
                          <p className="eyebrow">Picture-in-picture</p>
                          {facecam && (
                            <span className="numeric text-xs text-ink-500">
                              facecam {facecam.corner.toUpperCase()} ·{' '}
                              {Math.round(facecam.coverage * 100)}%
                            </span>
                          )}
                        </div>
                        <label className="mt-3 flex items-center gap-2 text-sm text-ink-200">
                          <input
                            type="checkbox"
                            checked={selected.pip_enabled}
                            onChange={(e) =>
                              setPip(selected, { pip_enabled: e.target.checked })
                            }
                            className="size-4 accent-sodium-500"
                          />
                          Facecam inset over full screen
                        </label>
                        {selected.pip_enabled && (
                          <div className="mt-3 space-y-3">
                            {!facecam ? (
                              <button
                                onClick={() => detectFacecam(selected)}
                                disabled={detecting}
                                className="btn btn-ghost w-full"
                              >
                                {detecting ? 'Scanning for facecam…' : 'Detect facecam'}
                              </button>
                            ) : (
                              <>
                                <div className="flex gap-2">
                                  {['auto', 'tl', 'tr', 'bl', 'br'].map((corner) => (
                                    <button
                                      key={corner}
                                      onClick={() => setPip(selected, { pip_corner: corner })}
                                      className={[
                                        'btn flex-1',
                                        selected.pip_corner === corner
                                          ? 'btn-primary'
                                          : 'btn-ghost',
                                      ].join(' ')}
                                    >
                                      {corner === 'auto' ? 'Auto' : corner.toUpperCase()}
                                    </button>
                                  ))}
                                </div>
                                <div>
                                  <div className="flex items-baseline justify-between">
                                    <span className="text-xs text-ink-400">Inset size</span>
                                    <span className="numeric text-xs text-ink-500">
                                      {Math.round(
                                        (pipSizeDraft ?? selected.pip_size * 100),
                                      )}
                                      %
                                    </span>
                                  </div>
                                  <input
                                    type="range"
                                    min={20}
                                    max={60}
                                    step={1}
                                    value={Math.round(pipSizeDraft ?? selected.pip_size * 100)}
                                    onChange={(e) => setPipSizeDraft(Number(e.target.value))}
                                    onPointerUp={(e) => {
                                      const size = Number(e.currentTarget.value) / 100
                                      setPipSizeDraft(null)
                                      if (Math.abs(size - selected.pip_size) > 0.005) {
                                        setPip(selected, { pip_size: size })
                                      }
                                    }}
                                    aria-label="Facecam inset size"
                                    className="mt-2 h-1 w-full cursor-pointer appearance-none rounded-full bg-ink-700 accent-sodium-500"
                                  />
                                </div>
                                <button
                                  onClick={() => detectFacecam(selected)}
                                  disabled={detecting}
                                  className="btn btn-quiet text-xs"
                                >
                                  {detecting ? 'Scanning…' : 'Re-detect facecam'}
                                </button>
                              </>
                            )}
                            <p className="text-xs leading-snug text-ink-500">
                              {facecam
                                ? 'Inset follows the detected facecam; corner and size apply on export.'
                                : 'Scans this clip for a persistent corner face (about a minute per minute of clip).'}
                            </p>
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                </div>
                <div className="border-t border-ink-800 pt-6">
                  <button
                    onClick={() => exportClip(selected)}
                    disabled={exporting.has(selected.id)}
                    className="btn btn-primary w-full"
                  >
                    {exporting.has(selected.id) ? 'Rendering…' : 'Export this clip'}
                  </button>

                  {selected.exports.length > 0 && (
                    <ul className="mt-4 space-y-2">
                      {selected.exports.map((record) => (
                        <li
                          key={record.id}
                          className="flex items-baseline justify-between gap-3 text-xs"
                        >
                          <a
                            href={record.download_url}
                            download
                            className="truncate text-sodium-500 underline underline-offset-4"
                          >
                            {record.ratio} · {record.style}
                          </a>
                          <span className="numeric shrink-0 text-ink-600">
                            {formatBytes(record.size_bytes)}
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              </>
            )}
          </section>
        </div>
      )}
    </div>
  )
}

function ClipRow({
  clip,
  index,
  selected,
  onSelect,
  onKeep,
  onDiscard,
}: {
  clip: Clip
  index: number
  selected: boolean
  onSelect: () => void
  onKeep: () => void
  onDiscard: () => void
}) {
  return (
    <div
      className={[
        'rise group grid cursor-pointer grid-cols-[2.25rem_1fr] gap-x-3 border-b border-ink-850 py-3 pl-2 transition-colors duration-200',
        selected ? 'bg-ink-850/70' : 'hover:bg-ink-850/35',
        clip.status === 'discarded' ? 'opacity-40' : '',
      ].join(' ')}
      style={{ animationDelay: `${Math.min(index, 10) * 35}ms` }}
      onClick={onSelect}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === 'Enter') onSelect()
      }}
    >
      {/* The score is the thing you scan by, so it gets the display face. */}
      <span
        className={[
          'numeric self-start font-display text-2xl leading-none',
          clip.score >= 85 ? 'text-sodium-500' : selected ? 'text-ink-200' : 'text-ink-500',
        ].join(' ')}
      >
        {clip.score}
      </span>

      <div className="min-w-0">
        <p className="truncate text-[0.9375rem] leading-snug text-ink-100">
          {clip.title || 'Untitled clip'}
        </p>
        <div className="mt-1 flex items-baseline gap-3">
          <span className="numeric text-xs text-ink-500">{formatDuration(clip.duration_s)}</span>
          {clip.user_trimmed && <span className="text-xs text-ink-600">trimmed</span>}
          {clip.exports.length > 0 && (
            <span className="text-xs text-signal-good">exported</span>
          )}

          {/* Secondary actions appear on hover or when the row is current —
              progressive disclosure keeps the scan column clean. */}
          <span
            className={[
              'ml-auto flex gap-3 pr-2 transition-opacity duration-200',
              selected ? 'opacity-100' : 'opacity-0 group-hover:opacity-100',
            ].join(' ')}
          >
            <button
              onClick={(e) => {
                e.stopPropagation()
                onKeep()
              }}
              className={`text-xs ${clip.status === 'kept' ? 'text-signal-good' : 'text-ink-400 hover:text-ink-100'}`}
            >
              {clip.status === 'kept' ? 'kept' : 'keep'}
            </button>
            <button
              onClick={(e) => {
                e.stopPropagation()
                onDiscard()
              }}
              className="text-xs text-ink-400 hover:text-signal-bad"
            >
              {clip.status === 'discarded' ? 'undo' : 'drop'}
            </button>
          </span>
        </div>
      </div>
    </div>
  )
}

function EmptyState() {
  return (
    <div className="max-w-xl pt-20">
      <h2 className="font-display text-3xl text-ink-200">No clips came back.</h2>
      <p className="mt-4 text-sm leading-relaxed text-ink-400">
        The model found nothing self-contained enough to stand alone — which is a real
        answer for some source material, not necessarily a failure.
      </p>
      <p className="mt-3 text-sm leading-relaxed text-ink-400">
        If you expected clips, try a larger provider model, or widen the length range so
        shorter moments qualify.
      </p>
    </div>
  )
}
