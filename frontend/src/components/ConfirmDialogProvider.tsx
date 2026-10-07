/**
 * Confirmation dialog for destructive actions.
 *
 * `useConfirm().confirm({ title, message, danger })` returns a promise that
 * resolves `true` only when the operator activates the confirm button. Escape
 * and the backdrop cancel. Focus moves into the dialog when it opens and goes
 * back to the trigger when it closes, so keyboard flows stay intact.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'

export interface ConfirmOptions {
  title: string
  message: string
  confirmLabel?: string
  cancelLabel?: string
  danger?: boolean
}

interface PendingConfirm {
  options: ConfirmOptions
  resolve: (value: boolean) => void
}

interface ConfirmApi {
  confirm: (options: ConfirmOptions) => Promise<boolean>
}

const ConfirmContext = createContext<ConfirmApi | null>(null)

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [pending, setPending] = useState<PendingConfirm | null>(null)
  const dialogRef = useRef<HTMLDivElement | null>(null)
  const confirmButtonRef = useRef<HTMLButtonElement | null>(null)
  const restoreFocusRef = useRef<HTMLElement | null>(null)

  const settle = useCallback((value: boolean) => {
    setPending((current) => {
      current?.resolve(value)
      return null
    })
  }, [])

  const confirm = useCallback(
    (options: ConfirmOptions) =>
      new Promise<boolean>((resolve) => {
        restoreFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
        setPending({ options, resolve })
      }),
    [],
  )

  useEffect(() => {
    if (!pending) {
      restoreFocusRef.current?.focus?.()
      restoreFocusRef.current = null
      return
    }
    confirmButtonRef.current?.focus()
  }, [pending])

  useEffect(() => {
    if (!pending) return
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        settle(false)
        return
      }
      if (event.key !== 'Tab' || !dialogRef.current) return
      // Keep Tab cycling inside the dialog while it is open.
      const focusable = dialogRef.current.querySelectorAll<HTMLElement>('button, [href], input, textarea, select')
      if (focusable.length === 0) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [pending, settle])

  const api = useMemo<ConfirmApi>(() => ({ confirm }), [confirm])

  return (
    <ConfirmContext.Provider value={api}>
      {children}
      {pending ? (
        <div className="dialog-backdrop" onClick={() => settle(false)}>
          <div
            ref={dialogRef}
            className="dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="confirm-dialog-title"
            aria-describedby="confirm-dialog-message"
            onClick={(event) => event.stopPropagation()}
          >
            <h2 id="confirm-dialog-title">{pending.options.title}</h2>
            <p id="confirm-dialog-message">{pending.options.message}</p>
            <div className="dialog-actions">
              <button type="button" className="button button-secondary" onClick={() => settle(false)}>
                {pending.options.cancelLabel ?? 'Cancel'}
              </button>
              <button
                ref={confirmButtonRef}
                type="button"
                className={`button ${pending.options.danger ? 'button-danger' : 'button-primary'}`}
                onClick={() => settle(true)}
              >
                {pending.options.confirmLabel ?? 'Confirm'}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </ConfirmContext.Provider>
  )
}

export function useConfirm(): ConfirmApi {
  const context = useContext(ConfirmContext)
  if (!context) throw new Error('useConfirm must be used inside <ConfirmProvider>')
  return context
}
