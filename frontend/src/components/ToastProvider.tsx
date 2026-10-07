/**
 * App-wide toast notifications.
 *
 * `ToastProvider` renders the stack; any component calls `useToast().push(...)`
 * to raise one. Success/info messages clear themselves after a few seconds;
 * errors stay until dismissed so a failed action is never missed.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'

export type ToastTone = 'success' | 'error' | 'info'

export interface ToastInput {
  title: string
  message?: string
  tone?: ToastTone
}

interface ToastEntry {
  id: number
  title: string
  message: string
  tone: ToastTone
}

interface ToastApi {
  push: (toast: ToastInput) => void
  success: (title: string, message?: string) => void
  error: (title: string, message?: string) => void
  info: (title: string, message?: string) => void
}

const ToastContext = createContext<ToastApi | null>(null)

const AUTO_DISMISS_MS: Record<ToastTone, number> = { success: 4500, info: 4500, error: 0 }

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastEntry[]>([])
  const nextId = useRef(1)
  const timers = useRef<number[]>([])

  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((toast) => toast.id !== id))
  }, [])

  const push = useCallback(
    (input: ToastInput) => {
      const id = nextId.current++
      const tone = input.tone ?? 'info'
      setToasts((current) => [...current.slice(-4), { id, title: input.title, message: input.message ?? '', tone }])
      const ttl = AUTO_DISMISS_MS[tone]
      if (ttl > 0) {
        const handle = window.setTimeout(() => dismiss(id), ttl)
        timers.current.push(handle)
      }
    },
    [dismiss],
  )

  const api = useMemo<ToastApi>(
    () => ({
      push,
      success: (title, message) => push({ title, message, tone: 'success' }),
      error: (title, message) => push({ title, message, tone: 'error' }),
      info: (title, message) => push({ title, message, tone: 'info' }),
    }),
    [push],
  )

  useEffect(() => () => timers.current.forEach((handle) => window.clearTimeout(handle)), [])

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="toast-stack" aria-label="Notifications">
        {toasts.map((toast) => (
          <div
            key={toast.id}
            className={`toast toast-${toast.tone}`}
            role={toast.tone === 'error' ? 'alert' : 'status'}
            aria-live={toast.tone === 'error' ? 'assertive' : 'polite'}
          >
            <div className="toast-body">
              <p className="toast-title">{toast.title}</p>
              {toast.message ? <p className="toast-message">{toast.message}</p> : null}
            </div>
            <button type="button" className="toast-dismiss" aria-label="Dismiss notification" onClick={() => dismiss(toast.id)}>
              ×
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast(): ToastApi {
  const context = useContext(ToastContext)
  if (!context) throw new Error('useToast must be used inside <ToastProvider>')
  return context
}
