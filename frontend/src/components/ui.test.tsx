/** Design system primitives (Stage H, H6). */
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Badge, Button, Card, EmptyState, Input, Modal } from './ui'

describe('Button', () => {
  it('renders with variant class', () => {
    render(<Button variant="primary">Save</Button>)
    const btn = screen.getByRole('button', { name: 'Save' })
    expect(btn.className).toContain('button-primary')
  })

  it('renders small size', () => {
    render(<Button size="sm">X</Button>)
    expect(screen.getByRole('button').className).toContain('button-sm')
  })
})

describe('Card', () => {
  it('renders title and children', () => {
    render(<Card title="Hello"><p>body</p></Card>)
    expect(screen.getByText('Hello')).toBeDefined()
    expect(screen.getByText('body')).toBeDefined()
  })
})

describe('Input', () => {
  it('associates label and shows error', () => {
    const { container } = render(<Input label="Name" error="Required" />)
    expect(container.querySelector('input')).toBeDefined()
    expect(screen.getByText('Required')).toBeDefined()
  })
})

describe('Badge', () => {
  it('renders tone class', () => {
    render(<Badge tone="danger">Failed</Badge>)
    expect(screen.getByText('Failed').className).toContain('ds-badge-danger')
  })
})

describe('Modal', () => {
  it('renders dialog with title', () => {
    render(<Modal title="Confirm" onClose={() => {}}><p>body</p></Modal>)
    expect(screen.getByRole('dialog')).toBeDefined()
    expect(screen.getByText('Confirm')).toBeDefined()
  })
})

describe('EmptyState', () => {
  it('renders title and description', () => {
    render(<EmptyState title="Empty" description="Nothing here" />)
    expect(screen.getByText('Empty')).toBeDefined()
    expect(screen.getByText('Nothing here')).toBeDefined()
  })
})
