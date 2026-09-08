import { describe, expect, it } from 'vitest'

import { extractApiError } from './api-errors'
import { assetErrorMessage } from './api'

const legacyFallback = 'An unexpected error occurred'

it('shows structured asset conflicts and preserves legacy error fallbacks', () => {
  expect(assetErrorMessage({ response: { status: 409, data: { detail: { code: 'dependent_activity', message: 'Reverse the dependent transfer first.' } } } }, 'Could not save')).toBe('Reverse the dependent transfer first.')
  expect(assetErrorMessage({ response: { data: { detail: 'Asset missing' } } }, 'Could not save')).toBe('Asset missing')
  expect(assetErrorMessage({ response: { status: 503, data: { detail: {} } } }, 'Could not save')).toBe('Could not save (503)')
})

function apiError(detail: unknown): unknown {
  return { response: { data: { detail } } }
}

describe('extractApiError', () => {
  it('returns a string detail verbatim', () => {
    expect(extractApiError(apiError('Category is still in use'))).toBe(
      'Category is still in use',
    )
  })

  it('formats FastAPI validation details using the legacy format', () => {
    expect(
      extractApiError(
        apiError([
          { loc: ['body', 'name'], msg: 'Field required' },
          { loc: ['body', 'amount'] },
        ]),
      ),
    ).toBe('name: Field required, amount: invalid')
  })

  it('uses the legacy fallback when detail is missing', () => {
    expect(extractApiError({ response: { data: {} } })).toBe(legacyFallback)
  })

  it('uses the legacy fallback for non-API errors', () => {
    expect(extractApiError(new Error('network failure'))).toBe(legacyFallback)
    expect(extractApiError(null)).toBe(legacyFallback)
  })

  it('preserves an empty string detail', () => {
    expect(extractApiError(apiError(''))).toBe('')
  })

  it('preserves a whitespace-only string detail', () => {
    expect(extractApiError(apiError('   '))).toBe('   ')
  })

  it('uses an explicit fallback when no detail is available', () => {
    expect(extractApiError(new Error('network failure'), 'Localized fallback')).toBe(
      'Localized fallback',
    )
  })
})
