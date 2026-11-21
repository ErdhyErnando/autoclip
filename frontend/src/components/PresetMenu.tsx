import { useState } from 'react'

import type { Preset } from '../api'

/**
 * Saved looks and layouts — contract §4.
 *
 * Presets are the whole on-screen arrangement (style overrides plus the caption
 * placement), so "apply" is the one gesture that reproduces a finished look on
 * another clip. Saving captures whatever the selected clip currently is,
 * including the per-clip geometry a drag just set.
 *
 * The endpoints live behind the backend lane, so a 404 is a normal rollout
 * state here: the section says so quietly instead of showing an error the user
 * cannot act on.
 */
export function PresetMenu({
  presets,
  unavailable,
  onApply,
  onSave,
  onSetDefault,
  onDelete,
}: {
  presets: Preset[]
  unavailable: boolean
  onApply: (id: string) => Promise<void>
  onSave: (name: string) => Promise<void>
  onSetDefault: (id: string) => Promise<void>
  onDelete: (id: string) => Promise<void>
}) {
  const [naming, setNaming] = useState(false)
  const [name, setName] = useState('')
  const [pending, setPending] = useState<string | null>(null)
  const [confirming, setConfirming] = useState<string | null>(null)

  const isDefault = (preset: Preset) => Boolean(preset.is_default)

  const run = async (key: string, action: () => Promise<void>) => {
    setPending(key)
    try {
      await action()
    } catch {
      // The page surfaces the failure; this only has to stop the spinner.
    } finally {
      setPending(null)
    }
  }

  const submitName = async () => {
    const trimmed = name.trim()
    if (!trimmed) return
    await run('save', () => onSave(trimmed))
    setName('')
    setNaming(false)
  }

  return (
    <div>
      <div className="flex items-baseline justify-between border-b border-ink-800 pb-2">
        <p className="eyebrow">Presets</p>
        <button
          type="button"
          onClick={() => setNaming((current) => !current)}
          disabled={unavailable}
          className="btn btn-quiet text-xs"
        >
          {naming ? 'Cancel' : 'Save current as…'}
        </button>
      </div>

      {naming && (
        <div className="mt-3 flex items-center gap-2">
          <input
            autoFocus
            value={name}
            onChange={(event) => setName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') void submitName()
              if (event.key === 'Escape') {
                setNaming(false)
                setName('')
              }
            }}
            placeholder="Preset name"
            aria-label="Preset name"
            className="field py-1.5 text-sm"
          />
          <button
            type="button"
            onClick={() => void submitName()}
            disabled={!name.trim() || pending === 'save'}
            className="btn btn-primary shrink-0"
          >
            {pending === 'save' ? 'Saving…' : 'Create'}
          </button>
        </div>
      )}

      {unavailable ? (
        <p className="mt-3 text-xs leading-snug text-ink-500">
          Preset storage is not available in this build yet — it arrives with the
          backend lane. Styling and positioning above still apply to this clip.
        </p>
      ) : presets.length === 0 ? (
        <p className="mt-3 text-xs leading-snug text-ink-500">
          No presets yet. Style this clip, place the caption, then save it as one.
        </p>
      ) : (
        <ul className="mt-1">
          {presets.map((preset) => (
            <li
              key={preset.id}
              className="flex items-baseline justify-between gap-3 border-b border-ink-850 py-2 last:border-0"
            >
              <div className="min-w-0">
                <p className="truncate text-xs text-ink-100">{preset.name}</p>
                <p className="text-[0.6875rem] text-ink-600">
                  {preset.base_style}
                  {isDefault(preset) && <span className="text-sodium-500"> · default</span>}
                </p>
              </div>

              <div className="flex shrink-0 items-baseline gap-3">
                <button
                  type="button"
                  onClick={() => void run(`apply:${preset.id}`, () => onApply(preset.id))}
                  disabled={pending === `apply:${preset.id}`}
                  className="text-xs text-sodium-500 transition-colors hover:text-sodium-400"
                >
                  {pending === `apply:${preset.id}` ? 'Applying…' : 'Apply'}
                </button>
                {!isDefault(preset) && (
                  <button
                    type="button"
                    onClick={() =>
                      void run(`default:${preset.id}`, () => onSetDefault(preset.id))
                    }
                    disabled={pending === `default:${preset.id}`}
                    className="text-xs text-ink-400 transition-colors hover:text-ink-100"
                  >
                    Default
                  </button>
                )}
                {confirming === preset.id ? (
                  <button
                    type="button"
                    onClick={() => {
                      setConfirming(null)
                      void run(`delete:${preset.id}`, () => onDelete(preset.id))
                    }}
                    onBlur={() => setConfirming(null)}
                    className="text-xs text-signal-bad"
                  >
                    Confirm
                  </button>
                ) : (
                  <button
                    type="button"
                    onClick={() => setConfirming(preset.id)}
                    className="text-xs text-ink-500 transition-colors hover:text-signal-bad"
                  >
                    Delete
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
