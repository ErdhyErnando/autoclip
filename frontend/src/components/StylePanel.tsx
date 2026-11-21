import { useState } from 'react'

import type {
  Animation,
  CaptionStyle,
  Clip,
  EffectiveCaption,
  FontEntry,
  Preset,
  StyleOverrides,
} from '../api'
import { ColorField } from './ColorField'
import { PresetMenu } from './PresetMenu'

/** Contract §1's enumeration, labelled for the control rather than the wire. */
const ANIMATIONS: { value: Animation; label: string }[] = [
  { value: 'scale', label: 'Pop' },
  { value: 'karaoke', label: 'Karaoke' },
  { value: 'none', label: 'None' },
]

/**
 * The STYLE tab: everything about how the caption looks, for one clip.
 *
 * Every control writes through `onOverride`, which patches the clip's complete
 * `style_overrides` blob (contract §3 — the PATCH replaces wholesale, so a
 * partial send would silently drop the other fields). Values that are not
 * overridden display the resolved value from `effective_caption`, which is what
 * makes the controls read as "this is what the export will look like" rather
 * than "this is what someone once typed".
 */
export function StylePanel({
  clip,
  styles,
  fonts,
  fontsUnavailable,
  effective,
  presets,
  presetsUnavailable,
  onBaseStyle,
  onOverride,
  onApplyPreset,
  onSavePreset,
  onSetDefaultPreset,
  onDeletePreset,
}: {
  clip: Clip
  styles: CaptionStyle[]
  fonts: FontEntry[]
  fontsUnavailable: boolean
  effective: EffectiveCaption | null
  presets: Preset[]
  presetsUnavailable: boolean
  onBaseStyle: (key: string) => void
  onOverride: (patch: Partial<StyleOverrides>) => void
  onApplyPreset: (id: string) => Promise<void>
  onSavePreset: (name: string) => Promise<void>
  onSetDefaultPreset: (id: string) => Promise<void>
  onDeletePreset: (id: string) => Promise<void>
}) {
  // Slider drafts: the controls stay local while dragging and commit on release,
  // so a drag is one PATCH instead of sixty.
  const [wordsDraft, setWordsDraft] = useState<number | null>(null)
  const [outlineDraft, setOutlineDraft] = useState<number | null>(null)
  const [boxAlphaDraft, setBoxAlphaDraft] = useState<number | null>(null)

  const overrides = clip.style_overrides ?? {}

  const fontValue = overrides.font ?? effective?.font ?? ''
  const maxWords = overrides.max_words ?? effective?.maxWords ?? 3
  const outlineWidth = overrides.outline_width ?? effective?.outlineWidth ?? 0
  const boxed = overrides.boxed ?? effective?.boxed ?? false
  const animation = overrides.animation ?? effective?.animation ?? 'scale'
  const allCaps = overrides.all_caps ?? effective?.allCaps ?? false
  // ASS alpha: 0 is opaque, 255 is clear. Store what we read; display the
  // inversion, because "opacity" is the word people reach for.
  const boxAlpha = overrides.box_alpha ?? effective?.box_alpha ?? 0

  const primary = overrides.primary ?? effective?.primary ?? '#ffffff'
  const accent = overrides.accent !== undefined ? overrides.accent : (effective?.accent ?? null)
  const outline = overrides.outline ?? effective?.outline ?? '#000000'
  const boxColour = overrides.box_colour ?? effective?.box_colour ?? '#000000'

  // A select whose value is not among its options renders blank, so the current
  // font is always present even when the registry endpoint is not.
  const fontOptions = fonts.length > 0 ? fonts : fontValue ? [{ key: fontValue, label: fontValue }] : []

  return (
    <div className="space-y-7">
      {/* Base preset ---------------------------------------------------- */}
      <div>
        <p className="eyebrow border-b border-ink-800 pb-2">Base preset</p>
        <div className="mt-3 space-y-1">
          {styles.map((style) => (
            <button
              key={style.key}
              type="button"
              onClick={() => onBaseStyle(style.key)}
              className={[
                'block w-full border-l-2 py-2 pl-3 text-left transition-colors duration-200',
                style.key === clip.caption_style
                  ? 'border-sodium-500 bg-ink-850/60'
                  : 'border-transparent hover:border-ink-700 hover:bg-ink-850/30',
              ].join(' ')}
            >
              <span className="text-sm text-ink-100">{style.label}</span>
              <span className="mt-0.5 block text-xs leading-snug text-ink-500">
                {style.description}
              </span>
            </button>
          ))}
        </div>
      </div>

      {/* Type ----------------------------------------------------------- */}
      <div>
        <p className="eyebrow border-b border-ink-800 pb-2">Type</p>

        <label className="mt-3 block">
          <span className="text-xs text-ink-400">Font</span>
          <select
            value={fontValue}
            onChange={(event) => onOverride({ font: event.target.value })}
            className="field mt-1 text-sm"
          >
            {fontOptions.length === 0 && <option value="">No fonts listed</option>}
            {fontOptions.map((font) => (
              <option key={font.key} value={font.key}>
                {font.label}
              </option>
            ))}
          </select>
        </label>
        {fontsUnavailable && (
          <p className="mt-1 text-[0.6875rem] leading-snug text-ink-600">
            The font registry endpoint is not available in this build yet.
          </p>
        )}

        <label className="mt-3 flex items-center gap-2 text-xs text-ink-300">
          <input
            type="checkbox"
            checked={allCaps}
            onChange={(event) => onOverride({ all_caps: event.target.checked })}
            className="size-3.5 accent-sodium-500"
          />
          All caps
        </label>

        <div className="mt-2 flex items-baseline justify-between gap-3">
          <span className="text-xs text-ink-400">Size</span>
          <span className="numeric text-xs text-ink-500">
            {effective ? `${(effective.sizeRatio * 100).toFixed(1)}% of height` : '—'}
          </span>
        </div>
        <p className="mt-1 text-[0.6875rem] leading-snug text-ink-600">
          Drag the corner handle on the preview to resize.
        </p>
      </div>

      {/* Pacing --------------------------------------------------------- */}
      <div>
        <div className="flex items-baseline justify-between border-b border-ink-800 pb-2">
          <p className="eyebrow">Words per screen</p>
          <span className="numeric text-xs text-ink-500">{wordsDraft ?? maxWords}</span>
        </div>
        <input
          type="range"
          min={1}
          max={8}
          step={1}
          value={wordsDraft ?? maxWords}
          onChange={(event) => setWordsDraft(Number(event.target.value))}
          onPointerUp={(event) => {
            const next = Number(event.currentTarget.value)
            setWordsDraft(null)
            if (next !== maxWords) onOverride({ max_words: next })
          }}
          onKeyUp={(event) => {
            if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
              const next = Number(event.currentTarget.value)
              setWordsDraft(null)
              if (next !== maxWords) onOverride({ max_words: next })
            }
          }}
          aria-label="Words per caption group"
          className="mt-3 h-1 w-full cursor-pointer appearance-none rounded-full bg-ink-700 accent-sodium-500"
        />
        <div className="mt-1 flex justify-between text-[0.6875rem] text-ink-600">
          <span>one word at a time</span>
          <span>whole lines</span>
        </div>
      </div>

      {/* Animation ------------------------------------------------------ */}
      <div>
        <p className="eyebrow border-b border-ink-800 pb-2">Animation</p>
        <div className="mt-2 flex gap-2">
          {ANIMATIONS.map((option) => (
            <button
              key={option.value}
              type="button"
              onClick={() => onOverride({ animation: option.value })}
              aria-pressed={animation === option.value}
              className={[
                'btn flex-1',
                animation === option.value ? 'btn-primary' : 'btn-ghost',
              ].join(' ')}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>

      {/* Colour --------------------------------------------------------- */}
      <div>
        <p className="eyebrow border-b border-ink-800 pb-2">Colour</p>
        <div className="mt-1">
          <ColorField
            label="Text"
            value={primary}
            onChange={(value) => {
              if (value) onOverride({ primary: value })
            }}
          />
          <ColorField
            label="Spoken word"
            value={accent}
            allowNone
            noneHint="Disable the per-word highlight"
            onChange={(value) => onOverride({ accent: value })}
          />
          <ColorField
            label="Outline"
            value={outline}
            onChange={(value) => {
              if (value) onOverride({ outline: value })
            }}
          />
        </div>
      </div>

      {/* Outline -------------------------------------------------------- */}
      <div>
        <div className="flex items-baseline justify-between border-b border-ink-800 pb-2">
          <p className="eyebrow">Outline width</p>
          <span className="numeric text-xs text-ink-500">
            {(outlineDraft ?? outlineWidth).toFixed(1)}
          </span>
        </div>
        <input
          type="range"
          min={0}
          max={8}
          step={0.5}
          value={outlineDraft ?? outlineWidth}
          onChange={(event) => setOutlineDraft(Number(event.target.value))}
          onPointerUp={(event) => {
            const next = Number(event.currentTarget.value)
            setOutlineDraft(null)
            if (Math.abs(next - outlineWidth) > 0.001) onOverride({ outline_width: next })
          }}
          onKeyUp={(event) => {
            if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
              const next = Number(event.currentTarget.value)
              setOutlineDraft(null)
              if (Math.abs(next - outlineWidth) > 0.001) onOverride({ outline_width: next })
            }
          }}
          aria-label="Caption outline width"
          className="mt-3 h-1 w-full cursor-pointer appearance-none rounded-full bg-ink-700 accent-sodium-500"
        />
      </div>

      {/* Box ------------------------------------------------------------ */}
      <div>
        <p className="eyebrow border-b border-ink-800 pb-2">Caption box</p>
        <label className="mt-3 flex items-center gap-2 text-xs text-ink-300">
          <input
            type="checkbox"
            checked={boxed}
            onChange={(event) => onOverride({ boxed: event.target.checked })}
            className="size-3.5 accent-sodium-500"
          />
          Put the caption on a block
        </label>

        {boxed && (
          <div className="mt-2">
            <ColorField
              label="Box"
              value={boxColour}
              onChange={(value) => {
                if (value) onOverride({ box_colour: value })
              }}
            />
            <div className="flex items-baseline justify-between gap-3">
              <span className="text-xs text-ink-400">Box opacity</span>
              <span className="numeric text-xs text-ink-500">
                {Math.round((1 - (boxAlphaDraft ?? boxAlpha) / 255) * 100)}%
              </span>
            </div>
            <input
              type="range"
              min={0}
              max={255}
              step={5}
              // Runs clear-to-opaque on screen; the stored value is ASS alpha,
              // which counts the other way (0 = opaque, contract §1).
              value={255 - (boxAlphaDraft ?? boxAlpha)}
              onChange={(event) => setBoxAlphaDraft(255 - Number(event.target.value))}
              onPointerUp={(event) => {
                const next = 255 - Number(event.currentTarget.value)
                setBoxAlphaDraft(null)
                if (next !== boxAlpha) onOverride({ box_alpha: next })
              }}
              onKeyUp={(event) => {
                if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
                  const next = 255 - Number(event.currentTarget.value)
                  setBoxAlphaDraft(null)
                  if (next !== boxAlpha) onOverride({ box_alpha: next })
                }
              }}
              aria-label="Caption box opacity"
              className="mt-2 h-1 w-full cursor-pointer appearance-none rounded-full bg-ink-700 accent-sodium-500"
            />
          </div>
        )}
      </div>

      <PresetMenu
        presets={presets}
        unavailable={presetsUnavailable}
        onApply={onApplyPreset}
        onSave={onSavePreset}
        onSetDefault={onSetDefaultPreset}
        onDelete={onDeletePreset}
      />
    </div>
  )
}
