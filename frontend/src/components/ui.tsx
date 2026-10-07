/**
 * Design system primitives (Stage H, H6).
 *
 * Single source for Button, Card, Input, Badge, Modal, EmptyState.
 * All use CSS variables so dark/light themes apply automatically.
 */
import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode } from 'react'

// --- Button -------------------------------------------------------------------

type ButtonVariant = 'primary' | 'secondary' | 'danger' | 'ghost' | 'small'
type ButtonSize = 'sm' | 'md'

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: ButtonSize
}

export function Button({ variant = 'secondary', size = 'md', className = '', ...props }: ButtonProps) {
  const variantClass =
    variant === 'primary' ? 'button-primary'
    : variant === 'danger' ? 'button-danger'
    : variant === 'ghost' ? 'button-ghost'
    : variant === 'small' ? 'button-small'
    : 'button-secondary'
  const sizeClass = size === 'sm' ? 'button-sm' : ''
  return <button className={`button ${variantClass} ${sizeClass} ${className}`.trim()} {...props} />
}

// --- Card ---------------------------------------------------------------------

export function Card({ title, actions, children, className = '' }: {
  title?: ReactNode
  actions?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <section className={`ds-card ${className}`.trim()}>
      {(title || actions) && (
        <header className="ds-card-header">
          {title && <h2>{title}</h2>}
          {actions && <div className="ds-card-actions">{actions}</div>}
        </header>
      )}
      <div className="ds-card-body">{children}</div>
    </section>
  )
}

// --- Input --------------------------------------------------------------------

interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  label: string
  hint?: string
  error?: string
}

export function Input({ label, hint, error, id, ...props }: InputProps) {
  const inputId = id ?? `input-${label.toLowerCase().replace(/[^a-z0-9]/g, '-')}`
  return (
    <label className={`ds-field ${error ? 'ds-field-error' : ''}`} htmlFor={inputId}>
      <span className="ds-label">{label}</span>
      <input id={inputId} className="ds-input" aria-invalid={Boolean(error)} aria-describedby={hint ? `${inputId}-hint` : undefined} {...props} />
      {hint && <span className="ds-hint" id={`${inputId}-hint`}>{hint}</span>}
      {error && <span className="ds-error" role="alert">{error}</span>}
    </label>
  )
}

// --- Badge --------------------------------------------------------------------

type BadgeTone = 'success' | 'warning' | 'danger' | 'info' | 'muted'

export function Badge({ tone = 'muted', children }: { tone?: BadgeTone; children: ReactNode }) {
  return <span className={`ds-badge ds-badge-${tone}`}>{children}</span>
}

// --- Modal --------------------------------------------------------------------

export function Modal({ title, onClose, children, wide }: {
  title: ReactNode
  onClose: () => void
  children: ReactNode
  wide?: boolean
}) {
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label={typeof title === 'string' ? title : undefined} onClick={onClose}>
      <div className={`modal ${wide ? 'modal-wide' : ''}`} onClick={(e) => e.stopPropagation()}>
        <h2>{title}</h2>
        {children}
      </div>
    </div>
  )
}

// --- EmptyState ----------------------------------------------------------------

export function EmptyState({ title, description, action }: {
  title: string
  description?: string
  action?: ReactNode
}) {
  return (
    <div className="ds-empty">
      <h3>{title}</h3>
      {description && <p>{description}</p>}
      {action}
    </div>
  )
}
