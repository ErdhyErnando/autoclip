export interface TabOption<T extends string> {
  id: T
  label: string
}

/**
 * Ruled tab strip.
 *
 * Same vocabulary as the rest of the review column: the active tab is a sodium
 * rule under its label, not a filled pill. A tray of pills would read as a
 * different surface than the hairline panel it sits on.
 */
export function TabBar<T extends string>({
  tabs,
  value,
  onChange,
}: {
  tabs: readonly TabOption<T>[]
  value: T
  onChange: (id: T) => void
}) {
  return (
    <div role="tablist" className="flex gap-6 border-b border-ink-800">
      {tabs.map((tab) => {
        const active = tab.id === value
        return (
          <button
            key={tab.id}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(tab.id)}
            className={[
              'eyebrow -mb-px border-b-2 pb-2 transition-colors duration-200',
              active
                ? 'border-sodium-500 text-ink-100'
                : 'border-transparent hover:text-ink-200',
            ].join(' ')}
          >
            {tab.label}
          </button>
        )
      })}
    </div>
  )
}
