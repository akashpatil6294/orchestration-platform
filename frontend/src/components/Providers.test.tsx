import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { ConfirmProvider, useConfirm } from './ConfirmDialogProvider'
import { ToastProvider, useToast } from './ToastProvider'

function ToastHarness({ onReady }: { onReady: (api: ReturnType<typeof useToast>) => void }) {
  const toast = useToast()
  onReady(toast)
  return <div>toast harness</div>
}

describe('ToastProvider', () => {
  it('renders a success toast that auto-dismisses', () => {
    vi.useFakeTimers()
    let api!: ReturnType<typeof useToast>
    render(
      <ToastProvider>
        <ToastHarness onReady={(value) => (api = value)} />
      </ToastProvider>,
    )
    act(() => api.success('Saved', 'Everything is fine'))
    expect(screen.getByText('Saved')).toBeInTheDocument()
    expect(screen.getByRole('status')).toBeInTheDocument()

    act(() => {
      vi.advanceTimersByTime(5000)
    })
    expect(screen.queryByText('Saved')).not.toBeInTheDocument()
    vi.useRealTimers()
  })

  it('keeps error toasts until dismissed and exposes a dismiss control', () => {
    let api!: ReturnType<typeof useToast>
    render(
      <ToastProvider>
        <ToastHarness onReady={(value) => (api = value)} />
      </ToastProvider>,
    )
    act(() => api.error('Boom', 'It broke'))
    expect(screen.getByRole('alert')).toHaveTextContent('Boom')

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss notification' }))
    expect(screen.queryByText('Boom')).not.toBeInTheDocument()
  })
})

function ConfirmHarness({ onReady }: { onReady: (confirm: ReturnType<typeof useConfirm>['confirm']) => void }) {
  const { confirm } = useConfirm()
  onReady(confirm)
  return <div>confirm harness</div>
}

describe('ConfirmDialogProvider', () => {
  it('resolves true only when the confirm button is pressed and restores focus afterwards', async () => {
    let confirm!: ReturnType<typeof useConfirm>['confirm']
    render(
      <ConfirmProvider>
        <button type="button" id="trigger" onClick={() => confirm({ title: 'Delete?', message: 'Really?', confirmLabel: 'Delete it', danger: true })}>
          open
        </button>
        <ConfirmHarness onReady={(value) => (confirm = value)} />
      </ConfirmProvider>,
    )

    fireEvent.click(screen.getByRole('button', { name: 'open' }))
    const dialog = await screen.findByRole('dialog', { name: 'Delete?' })
    expect(dialog).toHaveTextContent('Really?')

    fireEvent.click(await screen.findByRole('button', { name: 'Delete it' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('resolves false on Escape and does not call the action', async () => {
    let confirm!: ReturnType<typeof useConfirm>['confirm']
    let outcome: boolean | null = null
    render(
      <ConfirmProvider>
        <ConfirmHarness
          onReady={(value) => {
            confirm = value
            void confirm({ title: 'Proceed?', message: 'Are you sure?' }).then((value) => (outcome = value))
          }}
        />
      </ConfirmProvider>,
    )
    await screen.findByRole('dialog', { name: 'Proceed?' })
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(outcome).toBe(false))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })
})
