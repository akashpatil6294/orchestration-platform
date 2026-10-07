/**
 * Documents page: upload validation, list rendering, delete flow.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ToastProvider } from '../components/ToastProvider'
import DocumentsPage from './DocumentsPage'

const apiGet = vi.hoisted(() => vi.fn())
const apiRemove = vi.hoisted(() => vi.fn())
const uploadDocument = vi.hoisted(() => vi.fn())
const downloadBlob = vi.hoisted(() => vi.fn())

vi.mock('../lib/api', () => ({
  ApiError: class ApiError extends Error {},
  api: { get: apiGet, post: vi.fn(), patch: vi.fn(), put: vi.fn(), remove: apiRemove },
  uploadDocument,
  downloadBlob,
}))

function doc(id: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    filename: 'invoice.pdf',
    content_type: 'application/pdf',
    size_bytes: 1024,
    sha256: 'abc123',
    created_at: '2026-10-06T10:00:00Z',
    ...overrides,
  }
}

function renderPage() {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <DocumentsPage />
      </ToastProvider>
    </MemoryRouter>,
  )
}

describe('DocumentsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiGet.mockResolvedValue({ items: [] })
  })

  it('lists uploaded documents with their IDs', async () => {
    apiGet.mockResolvedValue({ items: [doc('doc-1'), doc('doc-2', { filename: 'report.pdf' })] })
    renderPage()
    expect(await screen.findByText('invoice.pdf')).toBeInTheDocument()
    expect(screen.getByText('report.pdf')).toBeInTheDocument()
    expect(screen.getByText('doc-1')).toBeInTheDocument()
  })

  it('shows an empty state when there are no documents', async () => {
    renderPage()
    expect(await screen.findByText(/No documents yet/)).toBeInTheDocument()
  })

  it('rejects files larger than 5 MB before uploading', async () => {
    renderPage()
    await screen.findByText(/No documents yet/)
    const big = new File(['x'], 'huge.pdf', { type: 'application/pdf' })
    Object.defineProperty(big, 'size', { value: 6 * 1024 * 1024 })
    const input = document.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(input, { target: { files: [big] } })
    expect(await screen.findByText(/no larger than 5 MB/)).toBeInTheDocument()
    expect(uploadDocument).not.toHaveBeenCalled()
  })

  it('rejects non-PDF files before uploading', async () => {
    renderPage()
    await screen.findByText(/No documents yet/)
    const text = new File(['hello'], 'notes.txt', { type: 'text/plain' })
    const input = document.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(input, { target: { files: [text] } })
    expect(await screen.findByText(/Only PDF documents/)).toBeInTheDocument()
    expect(uploadDocument).not.toHaveBeenCalled()
  })

  it('uploads a valid PDF and reloads the list', async () => {
    uploadDocument.mockImplementation(async (_file: File, onProgress: (f: number) => void) => {
      onProgress(1)
      return doc('doc-new')
    })
    apiGet
      .mockResolvedValueOnce({ items: [] })
      .mockResolvedValueOnce({ items: [doc('doc-new')] })
    renderPage()
    await screen.findByText(/No documents yet/)
    const file = new File(['%PDF-1.4'], 'invoice.pdf', { type: 'application/pdf' })
    const input = document.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(input, { target: { files: [file] } })
    await waitFor(() => expect(uploadDocument).toHaveBeenCalledOnce())
    expect(await screen.findByText('doc-new')).toBeInTheDocument()
  })

  it('deletes a document after confirmation', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    apiGet.mockResolvedValue({ items: [doc('doc-1')] })
    apiRemove.mockResolvedValue(undefined)
    renderPage()
    await screen.findByText('invoice.pdf')
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(apiRemove).toHaveBeenCalledWith('/api/v1/documents/doc-1'))
  })
})
