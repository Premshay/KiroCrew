/** Copy code, trimming leading + trailing whitespace so a pasted command lands
 *  clean at the prompt — no leading indent, no trailing space. */
export function copyCode(text: string): Promise<boolean> {
  return copyToClipboard(text.trim())
}

export async function copyToClipboard(text: string): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {}
  }

  const ta = document.createElement('textarea')
  ta.value = text
  ta.style.position = 'fixed'
  ta.style.opacity = '0'
  document.body.appendChild(ta)
  try {
    ta.select()
    return document.execCommand('copy')
  } finally {
    document.body.removeChild(ta)
  }
}
