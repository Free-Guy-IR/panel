import { useState, useCallback } from 'react'

async function copyToClipboard(text: string): Promise<boolean> {
  // Try modern clipboard API first (required for iOS)
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch (err) {
      console.error('Clipboard API failed:', err)
      // Fall through to fallback method
    }
  }

  // Fallback: use execCommand for older browsers
  const input = document.createElement('input')
  input.value = text
  input.style.position = 'fixed'
  input.style.left = '-9999px'
  input.style.top = '-9999px'
  document.body.appendChild(input)
  input.focus()
  input.select()

  try {
    const successful = document.execCommand('copy')
    document.body.removeChild(input)
    return successful
  } catch (err) {
    document.body.removeChild(input)
    return false
  }
}

export function useClipboard({ timeout = 1500 } = {}) {
  const [error, setError] = useState<Error | null>(null)
  const [copied, setCopied] = useState(false)
  const [copyTimeout, setCopyTimeout] = useState<number | null>(null)

  const handleCopyResult = (value: boolean) => {
    window.clearTimeout(copyTimeout!)
    setCopyTimeout(window.setTimeout(() => setCopied(false), timeout))
    setCopied(value)
  }

  const copy = useCallback(
    async (text: string) => {
      try {
        const success = await copyToClipboard(text)
        if (success) {
          handleCopyResult(true)
          setError(null)
        } else {
          setError(new Error('useClipboard: copyToClipboard failed'))
          handleCopyResult(false)
        }
      } catch (err) {
        setError(err instanceof Error ? err : new Error('useClipboard: copyToClipboard failed'))
        handleCopyResult(false)
      }
    },
    [timeout],
  )

  const reset = () => {
    setCopied(false)
    setError(null)
    window.clearTimeout(copyTimeout!)
  }

  return { copy, reset, error, copied }
}
