import { Component, type ErrorInfo, type ReactNode } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  error: Error | null
}

/**
 * Last-resort render guard. Without it an unexpected exception in a page
 * unmounts the whole tree and leaves a blank screen.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // No token or credential is present in a stack trace; the message is enough
    // to correlate with the API request id shown to the user.
    console.error('Unhandled UI error', error, info.componentStack)

    // React throws this when a DOM node it tracked was reparented by an
    // external actor (browser translate, extensions, or a bug in a portal
    // component). Recovery is deterministic: one clean reload.
    const msg = String(error?.message || '')
    const isDomMismatch = msg.includes('removeChild') || msg.includes('insertBefore')
    if (isDomMismatch && typeof window !== 'undefined') {
      let alreadyTried = false
      try {
        alreadyTried = sessionStorage.getItem('__dom_reload') === '1'
      } catch {
        /* sessionStorage can be blocked; treat as not-tried */
      }
      if (!alreadyTried) {
        try { sessionStorage.setItem('__dom_reload', '1') } catch {}
        window.location.reload()
      }
    }
  }

  render() {
    if (!this.state.error) return this.props.children
    return (
      <div className="full-page-status">
        <div className="state-card state-card-error">
          <h2>This page could not be displayed</h2>
          <p>{this.state.error.message}</p>
          <button type="button" className="button button-primary" onClick={() => window.location.assign('/')}>
            Reload the application
          </button>
        </div>
      </div>
    )
  }
}

export default ErrorBoundary
