import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { detectFileType, ImageViewer, JsonlViewer } from '../components/FileRenderers'

describe('detectFileType', () => {
  it('returns jsonl for .jsonl files', () => {
    expect(detectFileType('data.jsonl')).toBe('jsonl')
    expect(detectFileType('/path/to/session.jsonl')).toBe('jsonl')
  })

  it('returns json for .json files (not jsonl)', () => {
    expect(detectFileType('config.json')).toBe('json')
  })
})

describe('JsonlViewer', () => {
  it('renders line count and initial page of lines', () => {
    const content = '{"a":1}\n{"b":2}\n{"c":3}\n'
    render(<JsonlViewer content={content} />)
    expect(screen.getByText('3 lines')).toBeInTheDocument()
  })

  it('shows remaining count when more lines exist than page size', () => {
    const lines = Array.from({ length: 150 }, (_, i) => JSON.stringify({ i }))
    render(<JsonlViewer content={lines.join('\n')} />)
    expect(screen.getByText('150 lines')).toBeInTheDocument()
    expect(screen.getByText(/50 remaining/)).toBeInTheDocument()
  })

  it('skips empty lines', () => {
    const content = '{"a":1}\n\n\n{"b":2}\n'
    render(<JsonlViewer content={content} />)
    expect(screen.getByText('2 lines')).toBeInTheDocument()
  })
})

describe('ImageViewer', () => {
  it('offers direct open and download actions for a path-backed image', () => {
    render(<ImageViewer filePath="/workspace/images/tour.svg" />)

    expect(screen.getByText('Open in new tab')).toHaveAttribute(
      'href',
      '/api/file-raw?path=%2Fworkspace%2Fimages%2Ftour.svg',
    )
    expect(screen.getByText('Save')).toHaveAttribute(
      'href',
      '/api/file-download?path=%2Fworkspace%2Fimages%2Ftour.svg',
    )
    expect(screen.getByText('Save')).toHaveAttribute('download')
  })
})
