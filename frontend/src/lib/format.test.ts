import { describe, expect, it } from 'vitest'
import { formatCurrency } from './format'

describe('formatCurrency', () => {
  it('formats an ISO currency', () => {
    expect(formatCurrency(1234.5, 'USD', 'en-US')).toBe('$1,234.50')
  })

  it('falls back for a currency code Intl rejects', () => {
    expect(formatCurrency(12, 'USDT', 'en-US')).toBe('12.00 USDT')
  })

  it('renders a dash for no value', () => {
    expect(formatCurrency(null)).toBe('—')
  })
})
