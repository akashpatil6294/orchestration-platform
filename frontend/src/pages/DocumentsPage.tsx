import { useEffect, useRef, useState } from 'react'

import { ApiError, api, downloadBlob, uploadDocument } from '../lib/api'
import type { DocumentInfo } from '../lib/api'
import { useToast } from '../components/ToastProvider'

const MAX_DOCUMENT_BYTES = 5 * 1024 * 1024

function formatBytes(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  return `${Math.max(1, Math.round(bytes / 1024))} KB`
}

export default function DocumentsPage() {
  const toast = useToast()
  const [documents, setDocuments] = useState<DocumentInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [uploading, setUploading] = useState(false)
  const [progress, setProgress] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  async function load() {
    setLoading(true)
    try {
      const data = await api.get<{ items: DocumentInfo[] }>('/api/v1/documents')
      setDocuments(data.items)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Could not load documents.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  async function onFileSelected(file: File | undefined) {
    if (!file) return
    setError(null)
    if (file.size > MAX_DOCUMENT_BYTES) {
      setError(`“${file.name}” is ${formatBytes(file.size)} — documents must be no larger than 5 MB.`)
      return
    }
    if (file.type && file.type !== 'application/pdf' && !file.name.toLowerCase().endsWith('.pdf')) {
      setError('Only PDF documents are accepted.')
      return
    }
    setUploading(true)
    setProgress(0)
    try {
      const document = await uploadDocument(file, setProgress)
      toast.success(`Uploaded “${document.filename}” (${formatBytes(document.size_bytes)})`)
      await load()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'The upload failed.')
    } finally {
      setUploading(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  async function downloadSelected(document: DocumentInfo) {
    try {
      const blob = await downloadBlob(`/api/v1/documents/${document.id}`)
      const url = URL.createObjectURL(blob)
      const anchor = window.document.createElement('a')
      anchor.href = url
      anchor.download = document.filename || 'document.pdf'
      anchor.click()
      URL.revokeObjectURL(url)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Could not download the document.')
    }
  }

  async function removeDocument(document: DocumentInfo) {
    if (!window.confirm(`Delete “${document.filename}”? Runs referencing it will fail.`)) return
    try {
      await api.remove(`/api/v1/documents/${document.id}`)
      toast.success('Document deleted.')
      await load()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Could not delete the document.')
    }
  }

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Documents</h1>
          <p className="muted">PDFs up to 5 MB. Reference them from a run with the document ID — the bytes never travel inside run inputs.</p>
        </div>
        <label className="button button-primary">
          {uploading ? `Uploading… ${Math.round(progress * 100)}%` : 'Upload PDF'}
          <input
            ref={fileRef}
            type="file"
            accept="application/pdf,.pdf"
            hidden
            disabled={uploading}
            onChange={(event) => onFileSelected(event.target.files?.[0])}
          />
        </label>
      </div>
      {uploading ? (
        <div className="progress" role="progressbar" aria-valuenow={Math.round(progress * 100)} aria-valuemin={0} aria-valuemax={100}>
          <div className="progress-fill" style={{ width: `${Math.round(progress * 100)}%` }} />
        </div>
      ) : null}
      {error ? (
        <div className="banner banner-error" role="alert">
          {error}
        </div>
      ) : null}
      {loading ? (
        <p className="muted">Loading…</p>
      ) : documents.length === 0 ? (
        <p className="muted">No documents yet. Upload a PDF to use it with the document AI tasks.</p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Size</th>
              <th>Uploaded</th>
              <th>Document ID</th>
              <th aria-label="Actions" />
            </tr>
          </thead>
          <tbody>
            {documents.map((document) => (
              <tr key={document.id}>
                <td>{document.filename}</td>
                <td>{formatBytes(document.size_bytes)}</td>
                <td>{new Date(document.created_at).toLocaleString()}</td>
                <td>
                  <code>{document.id}</code>
                </td>
                <td className="table-actions">
                  <button type="button" className="button button-ghost button-sm" onClick={() => downloadSelected(document)}>
                    Download
                  </button>
                  <button type="button" className="button button-ghost button-sm button-danger" onClick={() => removeDocument(document)}>
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
