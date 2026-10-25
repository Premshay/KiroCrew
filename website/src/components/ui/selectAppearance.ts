export const selectAppearanceClasses = {
  default: '',
  compact: 'text-[12.5px] px-2 py-1 rounded-md bg-bg-elevated text-text border border-border outline-hidden focus:border-accent cursor-pointer',
  pill: 'h-6 rounded-full border-transparent bg-transparent px-2 py-0 text-[12px]',
} as const

export type SelectAppearance = keyof typeof selectAppearanceClasses
