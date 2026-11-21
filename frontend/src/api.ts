/**
 * Typed client for the AutoClip API.
 *
 * Same-origin throughout: the server hands out the built bundle, so there is no
 * base URL to configure and no CORS to negotiate.
 */

export interface Source {
  id: string
  type: string
  title: string
  url: string | null
  filename: string | null
  channel: string | null
  duration_s: number
  width: number | null
  height: number | null
  fps: number | null
  has_audio: boolean
  has_video: boolean
  created_at: string
}

export type JobStatus = 'queued' | 'running' | 'failed' | 'done' | 'cancelled'

export interface Job {
  id: string
  source_id: string
  status: JobStatus
  current_stage: string
  progress: number
  error: string | null
  provider: string
  created_at: string
  updated_at: string
  started_at: string | null
  finished_at: string | null
  source: Source | null
}

export interface ExportRecord {
  id: string
  clip_id: string
  ratio: string
  style: string
  size_bytes: number
  created_at: string
  download_url: string
}

export type ClipStatus = 'candidate' | 'kept' | 'discarded' | 'exported'

export interface Clip {
  id: string
  job_id: string
  rank: number
  start_s: number
  end_s: number
  duration_s: number
  start_word: number
  end_word: number
  title: string
  hook: string
  score: number
  reason: string
  status: ClipStatus
  user_trimmed: boolean
  caption_style: string
  ratio: string
  crop_nudge: number
  centre_crop: boolean
  pip_enabled: boolean
  pip_corner: string
  pip_size: number
  /** Per-clip overrides; null inherits the base preset (contract §1). */
  style_overrides?: StyleOverrides | null
  /** Distance from the frame bottom, 0.0–0.8; null = the preset's default. */
  caption_margin_ratio?: number | null
  /** Horizontal anchor, 0.0–1.0; null = horizontally centred. */
  caption_x_ratio?: number | null
  /** Resolved snapshot for the preview (contract §3). */
  effective_caption?: EffectiveCaption | null
  exports: ExportRecord[]
}

export interface Word {
  text: string
  start: number
  end: number
  speaker: string | null
}

export interface CropKeyframe {
  t: number
  x: number
  y: number
}

export interface CropSegment {
  start_s: number
  end_s: number
  width: number
  height: number
  keyframes: CropKeyframe[]
  strategy: 'track' | 'wide' | 'general'
  zoom: number
  fit: boolean
}

/** Mirrors autoclip.pipeline.reframe.croppath.CropPath. */
export interface CropPath {
  source_width: number
  source_height: number
  segments: CropSegment[]
}

export interface FacecamBox {
  x: number
  y: number
  width: number
  height: number
  corner: string
  coverage: number
}

export interface CaptionPreview {
  font: string
  primary: string
  accent: string | null
  outline: string
  outlineWidth: number
  allCaps: boolean
  sizeRatio: number
  marginRatio: number | null
  boxed: boolean
  animation: string
  maxWords: number
}

export interface CaptionStyle {
  key: string
  label: string
  description: string
  preview: CaptionPreview
}

/**
 * Per-clip style overrides — contract §1.
 *
 * Every field is optional: a missing one falls through to the base preset, then
 * the legacy globals, then the built-in defaults. The PATCH replaces the whole
 * blob, so the UI always sends it complete (contract §3).
 */
export interface StyleOverrides {
  /** Registry key from GET /api/fonts; unknown keys are a 400. */
  font?: string
  /** Fraction of frame height, 0.02–0.12. */
  size_ratio?: number
  primary?: string
  /** null disables the per-word highlight. */
  accent?: string | null
  outline?: string
  /** 0.0–8.0. */
  outline_width?: number
  all_caps?: boolean
  animation?: Animation
  /** Words per caption group, 1–8 — the pacing control. */
  max_words?: number
  boxed?: boolean
  box_colour?: string
  /** ASS alpha: 0 = opaque, 255 = transparent. */
  box_alpha?: number
}

export type Animation = 'scale' | 'karaoke' | 'none'

export type CaptionAlignment = 'left' | 'center' | 'right'

/**
 * The resolved caption the server burns in, handed to the preview verbatim
 * (contract §3). Rendering this instead of resolving locally is what keeps the
 * preview honest (§8.1).
 */
export interface EffectiveCaption extends CaptionPreview {
  /** Contract §3 spelling of `marginRatio`; both are accepted during the
   *  transition so neither side has to guess. */
  margin_ratio?: number | null
  /** Horizontal anchor the resolution settled on. §3's field list does not
   *  name it, but §2's CSS mirror needs it; `caption_x_ratio` backs it up. */
  x_ratio?: number | null
  /** Derived from the anchor by §2's alignment maths. */
  alignment?: CaptionAlignment
  box_colour?: string | null
  box_alpha?: number | null
}

/** A font the renderer can burn in (contract §5). */
export interface FontEntry {
  key: string
  label: string
}

/** A saved look-and-layout preset (contract §4). */
export interface Preset {
  id: string
  name: string
  base_style: string
  style_overrides: StyleOverrides | null
  layout: PresetLayout | null
  /** SQLite integer; truthiness is the portable read. */
  is_default: boolean | number
  created_at?: string
}

/** `layout` on a preset — Phase 2 appends the `pip_*` placement fields here. */
export interface PresetLayout {
  caption_margin_ratio: number | null
  caption_x_ratio: number | null
}

export interface PresetInput {
  name: string
  base_style: string
  style_overrides?: StyleOverrides | null
  layout?: PresetLayout | null
}

export interface ProviderStatus {
  name: string
  available: boolean
  detail: string
  models: string[]
  requires_key: boolean
  has_key: boolean
}

export interface Settings {
  active_provider: string
  providers: Record<string, { model: string; base_url: string | null }>
  whisper: { model: string; compute_type: string; language: string; diarization: boolean }
  clips: { min_duration_s: number; max_duration_s: number; max_clips: number }
  ingest: {
    ytdlp_format: string
    cookies_from_browser: string
    prefer_youtube_captions: boolean
  }
  export: {
    ratio: string
    caption_style: string
    caption_size_scale: number
    caption_position_ratio: number | null
    loudness_lufs: number
    prefer_hardware_encoder: boolean
    crf: number
    write_srt: boolean
  }
  insecure_secret_storage: boolean
  keys_present: Record<string, boolean>
}

/** Partial settings update — the backend merges each section. */
export type SettingsPatch = {
  active_provider?: string
  providers?: Record<string, { model: string; base_url: string | null }>
  whisper?: Partial<Settings['whisper']>
  clips?: Partial<Settings['clips']>
  ingest?: Partial<Settings['ingest']>
  export?: Partial<Settings['export']>
}

export interface SystemStatus {
  ready: boolean
  python_version: string
  platform: string
  ffmpeg_version: string | null
  has_libass: boolean
  nvenc_works: boolean
  accel: string
  gpu_name: string | null
  compute_type: string
  diarization_available: boolean
}

export interface JobSettingsOverrides {
  provider?: string
  whisper_model?: string
  language?: string
  diarization?: boolean
  min_duration_s?: number
  max_duration_s?: number
  max_clips?: number
  caption_style?: string
  ratio?: string
}

/**
 * PATCH /api/clips/{id}/captions — contract §3 merge rules.
 *
 * `undefined` is absent (JSON.stringify drops it) and leaves the stored value
 * alone; an explicit `null` clears. The style blob and both geometry ratios are
 * therefore always sent deliberately, never by default.
 */
export interface CaptionPatch {
  words?: Word[]
  caption_style?: string
  ratio?: string
  crop_nudge?: number
  centre_crop?: boolean
  pip_enabled?: boolean
  pip_corner?: string
  pip_size?: number
  /** Replaces the stored blob wholesale; null clears it. */
  style_overrides?: StyleOverrides | null
  /** Present ⇒ set (null restores the preset default); absent ⇒ unchanged. */
  caption_margin_ratio?: number | null
  /** Present ⇒ set (null re-centres); absent ⇒ unchanged. */
  caption_x_ratio?: number | null
}

/** An API error carrying the server's message and its actionable hint. */
export class ApiError extends Error {
  hint: string
  status: number

  constructor(message: string, status: number, hint = '') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.hint = hint
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      ...(init?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
      ...init?.headers,
    },
  })

  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`
    let hint = ''
    try {
      const body = await response.json()
      const detail = body.detail
      if (typeof detail === 'string') {
        message = detail
      } else if (detail && typeof detail === 'object') {
        // The ingest endpoints return {message, hint} so the UI can show the
        // fix alongside the failure.
        message = detail.message ?? message
        hint = detail.hint ?? ''
      }
    } catch {
      /* Response had no JSON body; the status line is the best we have. */
    }
    throw new ApiError(message, response.status, hint)
  }

  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const api = {
  health: () => request<{ status: string; version: string }>('/api/health'),
  system: () => request<SystemStatus>('/api/system'),
  fetchModels: () => request<void>('/api/system/models', { method: 'POST' }),

  listSources: () => request<Source[]>('/api/sources'),

  ingestYouTube: (url: string, cookiesFromBrowser?: string) =>
    request<Source>('/api/sources/youtube', {
      method: 'POST',
      body: JSON.stringify({ url, cookies_from_browser: cookiesFromBrowser || null }),
    }),

  uploadSource: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<Source>('/api/sources/upload', { method: 'POST', body: form })
  },

  listJobs: (limit = 25) => request<Job[]>(`/api/jobs?limit=${limit}`),
  getJob: (id: string) => request<Job>(`/api/jobs/${id}`),

  createJob: (sourceId: string, settings: JobSettingsOverrides = {}) =>
    request<Job>('/api/jobs', {
      method: 'POST',
      body: JSON.stringify({ source_id: sourceId, settings }),
    }),

  cancelJob: (id: string) => request<Job>(`/api/jobs/${id}/cancel`, { method: 'POST' }),
  retryJob: (id: string) => request<Job>(`/api/jobs/${id}/retry`, { method: 'POST' }),

  listClips: (jobId: string) => request<Clip[]>(`/api/jobs/${jobId}/clips`),
  getClip: (clipId: string) => request<Clip>(`/api/clips/${clipId}`),

  getCropPath: (clipId: string) => request<CropPath>(`/api/clips/${clipId}/crop-path`),
  getClipWords: (clipId: string) => request<Word[]>(`/api/clips/${clipId}/words`),

  patchClip: (
    clipId: string,
    patch: { start_s?: number; end_s?: number; title?: string; status?: ClipStatus },
  ) => request<Clip>(`/api/clips/${clipId}`, { method: 'PATCH', body: JSON.stringify(patch) }),

  patchCaptions: (clipId: string, patch: CaptionPatch) =>
    request<Clip>(`/api/clips/${clipId}/captions`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    }),

  exportClip: (
    clipId: string,
    ratio: string,
    style: string,
    writeSrt = false,
    cropNudge?: number,
    centreCrop?: boolean,
    pip?: { enabled?: boolean; corner?: string; size?: number },
  ) =>
    request<ExportRecord>(`/api/clips/${clipId}/export`, {
      method: 'POST',
      body: JSON.stringify({
        ratio,
        style,
        write_srt: writeSrt,
        crop_nudge: cropNudge,
        centre_crop: centreCrop,
        pip_enabled: pip?.enabled,
        pip_corner: pip?.corner,
        pip_size: pip?.size,
      }),
    }),

  getFacecam: (clipId: string) => request<FacecamBox>(`/api/clips/${clipId}/facecam`),

  detectFacecam: (clipId: string) =>
    request<FacecamBox>(`/api/clips/${clipId}/facecam/detect`, { method: 'POST' }),

  captionStyles: () => request<CaptionStyle[]>('/api/caption-styles'),
  fonts: () => request<FontEntry[]>('/api/fonts'),

  listPresets: () => request<Preset[]>('/api/presets'),
  createPreset: (input: PresetInput) =>
    request<Preset>('/api/presets', { method: 'POST', body: JSON.stringify(input) }),
  updatePreset: (id: string, input: Partial<PresetInput>) =>
    request<Preset>(`/api/presets/${id}`, { method: 'PUT', body: JSON.stringify(input) }),
  setDefaultPreset: (id: string) =>
    request<Preset>(`/api/presets/${id}/default`, { method: 'PUT' }),
  deletePreset: (id: string) => request<void>(`/api/presets/${id}`, { method: 'DELETE' }),

  /** Copies a preset's look and layout onto one clip — server-side and atomic. */
  applyPreset: (id: string, clipId: string) =>
    request<Clip>(`/api/presets/${id}/apply`, {
      method: 'POST',
      body: JSON.stringify({ clip_id: clipId }),
    }),

  providerStatus: () => request<ProviderStatus[]>('/api/providers/status'),

  getSettings: () => request<Settings>('/api/settings'),
  putSettings: (patch: SettingsPatch) =>
    request<Settings>('/api/settings', { method: 'PUT', body: JSON.stringify(patch) }),

  putSecret: (key: string, value: string) =>
    request<void>('/api/settings/secrets', {
      method: 'PUT',
      body: JSON.stringify({ key, value }),
    }),

  deleteSecret: (key: string) =>
    request<void>(`/api/settings/secrets/${key}`, { method: 'DELETE' }),

  mediaUrl: (jobId: string) => `/api/jobs/${jobId}/media`,
}

/**
 * The caption snapshot the preview draws for one clip.
 *
 * Contract §3 has the server hand out `effective_caption` and §8.1 forbids the
 * UI from reimplementing resolution, so a server snapshot is used verbatim.
 * The base-preset fallback below only runs against a backend that predates the
 * field: without it the editor would blank out rather than merely lag behind.
 */
export function captionForClip(
  clip: Clip,
  styles: CaptionStyle[],
): EffectiveCaption | null {
  if (clip.effective_caption) return clip.effective_caption
  const base = styles.find((style) => style.key === clip.caption_style)?.preview
  if (!base) return null
  return { ...base, alignment: 'center' }
}

/** Format seconds as m:ss, or h:mm:ss past an hour. */
export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.round(seconds))
  const s = total % 60
  const m = Math.floor(total / 60) % 60
  const h = Math.floor(total / 3600)
  const pad = (n: number) => String(n).padStart(2, '0')
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`
}

/** Format seconds as timecode with centiseconds, for trim handles. */
export function formatTimecode(seconds: number): string {
  const total = Math.max(0, seconds)
  const cs = Math.floor((total % 1) * 100)
  const s = Math.floor(total) % 60
  const m = Math.floor(total / 60)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(m)}:${pad(s)}.${pad(cs)}`
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB']
  let value = bytes / 1024
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`
}
