import i18n from 'i18next'
import { beforeAll, describe, expect, it } from 'vitest'

import en from '@/locales/en.json'
import ptBR from '@/locales/pt-BR.json'

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

describe('localized API errors', () => {
  beforeAll(async () => {
    await i18n.init({
      lng: 'en',
      fallbackLng: false,
      resources: {
        en: { translation: en },
        'pt-BR': { translation: ptBR },
      },
    })
  })

  it.each([
    ['en', 'The amount or date of a synchronized transaction cannot be changed'],
    ['pt-BR', 'Não é possível alterar o valor ou a data de uma transação sincronizada'],
  ])('shows the synchronized amount/date error in %s', async (language, expected) => {
    await i18n.changeLanguage(language)
    const message = 'Cannot change amount or date on a synchronized transaction'

    expect(extractApiError(apiError(message))).toBe(expected)
    expect(extractApiError(apiError({ message }))).toBe(expected)
  })

  it.each([
    ['en', 'Enter a valid http or https endpoint URL'],
    ['pt-BR', 'Informe uma URL de endpoint http ou https válida'],
  ])('shows the invalid connection base URL error in %s', async (language, expected) => {
    await i18n.changeLanguage(language)

    expect(extractApiError(apiError('Enter a valid http or https endpoint URL'))).toBe(expected)
  })

  it.each([
    ['en', { code: 'mortgage_linked', message: 'A mortgage payment with a breakdown cannot also be a transfer' }, 'A mortgage payment with a breakdown cannot also be a transfer. Remove its mortgage breakdown first.'],
    ['pt-BR', { code: 'mortgage_linked', message: 'A mortgage payment with a breakdown cannot also be a transfer' }, 'Um pagamento de financiamento com divisão não pode ser também uma transferência. Remova primeiro a divisão do financiamento.'],
    ['pt-BR', { message: 'A budget for this category already exists for this month' }, 'Esta categoria já tem um orçamento neste mês.'],
    ['pt-BR', { message: 'A budget for one of these categories already exists for this month' }, 'Uma destas categorias já tem um orçamento neste mês.'],
  ])('translates reconciliation and budget refusals in %s', async (language, detail, expected) => {
    await i18n.changeLanguage(language)

    expect(extractApiError(apiError(detail))).toBe(expected)
  })
})
