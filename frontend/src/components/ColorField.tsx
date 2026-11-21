import { useEffect, useState } from 'react'

/**
 * Normalise typed colour input to `#rrggbb`, or null when it is not a colour.
 *
 * The contract stores `#RRGGBB` (§1), so the field accepts a leading `#` or
 * none, three or six digits, and commits only what it can prove is a colour —
 * anything else reverts rather than sending the server a value it would clamp.
 */
export function normaliseHex(input: string): string | null {
  const raw = input.trim().replace(/^#/, '')
  if (/^[0-9a-fA-F]{3}$/.test(raw)) {
    return `#${raw
      .split('')
      .map((character) => character + character)
      .join('')}`.toLowerCase()
  }
  if (/^[0-9a-fA-F]{6}$/.test(raw)) return `#${raw.toLowerCase()}`
  return null
}

/**
 * One colour: a native picker's swatch plus a hex field, side by side.
 *
 * The swatch is the fast path and the hex field is the exact one, which is how
 * anyone who already knows the brand colour expects to enter it. No dependency
 * buys both: `<input type="color">` is in every browser this app targets.
 */
export function ColorField({
  label,
  value,
  onChange,
  allowNone = false,
  noneHint,
}: {
  label: string
  value: string | null
  onChange: (value: string | null) => void
  /** Offer a "none" state — the contract's `accent: null` disables the highlight. */
  allowNone?: boolean
  noneHint?: string
}) {
  const [draft, setDraft] = useState(value ?? '')

  useEffect(() => setDraft(value ?? ''), [value])

  const commit = () => {
    const next = normaliseHex(draft)
    if (next === null) {
      setDraft(value ?? '')
      return
    }
    setDraft(next)
    if (next !== value) onChange(next)
  }

  return (
    <div className="flex items-center justify-between gap-3 py-1.5">
      <span className="text-xs text-ink-400">{label}</span>
      <div className="flex shrink-0 items-center gap-2">
        {allowNone && (
          <button
            type="button"
            onClick={() => onChange(null)}
            aria-pressed={value === null}
            title={noneHint ?? 'Disable'}
            className={[
              'text-[0.625rem] uppercase tracking-[0.12em] transition-colors duration-200',
              value === null ? 'text-sodium-500' : 'text-ink-600 hover:text-ink-300',
            ].join(' ')}
          >
            None
          </button>
        )}
        <input
          type="color"
          value={value ?? '#000000'}
          onChange={(event) => onChange(event.target.value)}
          aria-label={`${label} colour`}
          className="h-6 w-9 cursor-pointer appearance-none rounded-[3px] border border-ink-700 bg-transparent p-0 [&::-moz-color-swatch]:border-0 [&::-webkit-color-swatch-wrapper]:p-0 [&::-webkit-color-swatch]:rounded-[2px] [&::-webkit-color-swatch]:border-0"
        />
        <input
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onBlur={commit}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault()
              commit()
            }
            if (event.key === 'Escape') setDraft(value ?? '')
          }}
          spellCheck={false}
          aria-label={`${label} hex value`}
          className="field numeric w-[5.5rem] py-1 text-xs"
        />
      </div>
    </div>
  )
}
