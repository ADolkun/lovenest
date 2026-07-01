import { describe, expect, it } from 'vitest'

import { localDateString } from './date-utils'

describe('localDateString', () => {
  it('formats the browser-local calendar day', () => {
    expect(localDateString(new Date(2026, 5, 30, 23, 30))).toBe('2026-06-30')
  })
})
