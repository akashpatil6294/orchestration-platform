/**
 * A button that runs an async action with a built-in pending state.
 *
 * The button disables itself while the action is in flight, optionally swaps
 * its label, and reports failures through the toast system so callers do not
 * have to repeat the same error handling on every destructive control.
 */
import { useState, type MouseEvent } from 'react'

import { useToast } from './ToastProvider'

interface ActionButtonProps {
  label: string
  pendingLabel?: string
  onAction: () => Promise<unknown> | unknown
  className?: string
  disabled?: boolean
  title?: string
  /** Error toast title; defaults to a generic message. */
  errorTitle?: string
  /** When false, failures are re-thrown for the caller to handle. */
  toastErrors?: boolean
  variant?: 'primary' | 'secondary' | 'danger' | 'ghost'
}

export function ActionButton({
  label,
  pendingLabel,
  onAction,
  className = '',
  disabled = false,
  title,
  errorTitle = 'The action failed',
  toastErrors = true,
  variant = 'secondary',
}: ActionButtonProps) {
  const [pending, setPending] = useState(false)
  const toast = useToast()

  const handleClick = async (event: MouseEvent<HTMLButtonElement>) => {
    event.stopPropagation()
    if (pending || disabled) return
    setPending(true)
    try {
      await onAction()
    } catch (cause) {
      if (toastErrors) {
        const message = cause instanceof Error ? cause.message : 'Unexpected error'
        toast.error(errorTitle, message)
      } else {
        throw cause
      }
    } finally {
      setPending(false)
    }
  }

  return (
    <button
      type="button"
      className={`button button-${variant}${className ? ` ${className}` : ''}`}
      onClick={handleClick}
      disabled={disabled || pending}
      title={title}
      aria-busy={pending}
    >
      {pending && pendingLabel ? pendingLabel : label}
    </button>
  )
}
