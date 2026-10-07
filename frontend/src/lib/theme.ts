/**
 * Light/dark theme toggle, persisted in localStorage.
 *
 * The stylesheet defaults to the dark palette; the light palette activates on
 * `html[data-theme='light']`. With no stored preference the browser's own
 * `prefers-color-scheme` wins.
 */
import { useCallback, useEffect, useState } from 'react'

export type Theme = 'light' | 'dark'

const STORAGE_KEY = 'orchestrator.theme'

function storedTheme(): Theme | null {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY)
    return value === 'light' || value === 'dark' ? value : null
  } catch {
    return null
  }
}

function systemTheme(): Theme {
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

function apply(theme: Theme) {
  document.documentElement.dataset.theme = theme
}

export function initialTheme(): Theme {
  const theme = storedTheme() ?? systemTheme()
  apply(theme)
  return theme
}

export function useTheme(): { theme: Theme; toggle: () => void } {
  const [theme, setTheme] = useState<Theme>(initialTheme)

  useEffect(() => {
    apply(theme)
  }, [theme])

  const toggle = useCallback(() => {
    setTheme((current) => {
      const next: Theme = current === 'dark' ? 'light' : 'dark'
      try {
        window.localStorage.setItem(STORAGE_KEY, next)
      } catch {
        // Storage can be unavailable (private mode); the toggle still works.
      }
      return next
    })
  }, [])

  return { theme, toggle }
}
