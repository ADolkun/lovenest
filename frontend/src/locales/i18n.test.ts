/// <reference types="node" />
import { readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const LOCALES_DIR = path.dirname(fileURLToPath(import.meta.url))
const LOCALES = readdirSync(LOCALES_DIR)
  .filter((f: string) => f.endsWith('.json'))
  .map((f: string) => f.replace(/\.json$/, ''))

// ponytail: these upstream locales fall back to English for Lovenest-only keys;
// remove a locale once its downstream strings have translations.
const LOCALES_WITH_DOWNSTREAM_FALLBACK = new Set(['el', 'hi', 'ja', 'sk'])

// #146 balance explanations and #133 ship EN + PT-BR. Other locales use English for these shared labels,
// while existing common/evidence keys still require translations.
const OWNED_TRANSFER_SHARED_KEYS = [
  'common.all',
  'evidence.unknown',
  'evidence.direction.in',
  'evidence.direction.out',
  'evidence.direction.unknown',
  'evidence.field.token_program',
  'evidence.field.source_address',
  'evidence.field.destination_address',
  'evidence.field.source_owner',
  'evidence.field.destination_owner',
  'evidence.field.raw_units',
  'evidence.field.decimals',
  'evidence.field.quantity_role',
  'evidence.field.fee_payer',
  'evidence.field.fee_semantics',
]

function readRaw(locale: string): string {
  return readFileSync(path.join(LOCALES_DIR, `${locale}.json`), 'utf-8')
}

function flattenKeys(obj: Record<string, unknown>, prefix = ''): string[] {
  return Object.entries(obj).flatMap(([k, v]) => {
    const full = prefix ? `${prefix}.${k}` : k
    return v !== null && typeof v === 'object' && !Array.isArray(v)
      ? flattenKeys(v as Record<string, unknown>, full)
      : [full]
  })
}

function flattenValues(obj: Record<string, unknown>, prefix = ''): Map<string, string> {
  const result = new Map<string, string>()
  for (const [k, v] of Object.entries(obj)) {
    const full = prefix ? `${prefix}.${k}` : k
    if (typeof v === 'string') result.set(full, v)
    else if (v !== null && typeof v === 'object' && !Array.isArray(v))
      for (const entry of flattenValues(v as Record<string, unknown>, full))
        result.set(entry[0], entry[1])
  }
  return result
}

function extractPlaceholders(value: string): Set<string> {
  return new Set((value.match(/\{\{(\w+)\}\}/g) ?? []))
}

// i18next plural suffixes — some languages (e.g. Polish) expand a single base
// key into _zero/_one/_two/_few/_many/_other forms instead of using one string.
const PLURAL_SUFFIXES = ['_zero', '_one', '_two', '_few', '_many', '_other']

function pluralBase(key: string): string | null {
  for (const s of PLURAL_SUFFIXES) {
    if (key.endsWith(s)) return key.slice(0, -s.length)
  }
  return null
}

function hasKeyOrPluralForms(keys: Set<string>, baseKey: string): boolean {
  return keys.has(baseKey) || PLURAL_SUFFIXES.some((s) => keys.has(`${baseKey}${s}`))
}

/**
 * Minimal JSON parser that detects duplicate keys at any nesting level.
 * JSON.parse silently discards duplicates (last-wins), so we need our own walk.
 */
function findDuplicateKeys(source: string): string[] {
  const duplicates: string[] = []
  let i = 0

  const ws = () => { while (i < source.length && ' \t\r\n'.includes(source[i])) i++ }

  const str = (): string => {
    i++ // skip "
    let s = ''
    while (i < source.length && source[i] !== '"') {
      if (source[i] === '\\') { i++; s += source[i++] }
      else s += source[i++]
    }
    i++ // skip "
    return s
  }

  const val = (keyPath: string): void => {
    ws()
    if (source[i] === '{') obj(keyPath)
    else if (source[i] === '[') arr(keyPath)
    else if (source[i] === '"') str()
    else while (i < source.length && !' \t\r\n,}]'.includes(source[i])) i++
  }

  const obj = (keyPath: string): void => {
    i++ // skip {
    ws()
    const seen = new Set<string>()
    while (i < source.length && source[i] !== '}') {
      ws()
      if (source[i] !== '"') break
      const key = str()
      const full = keyPath ? `${keyPath}.${key}` : key
      if (seen.has(key)) duplicates.push(full)
      seen.add(key)
      ws(); i++ // skip :
      val(full)
      ws()
      if (source[i] === ',') i++
      ws()
    }
    i++ // skip }
  }

  const arr = (keyPath: string): void => {
    i++ // skip [
    ws()
    let idx = 0
    while (i < source.length && source[i] !== ']') {
      val(`${keyPath}[${idx++}]`)
      ws()
      if (source[i] === ',') i++
      ws()
    }
    i++ // skip ]
  }

  ws(); val('')
  return duplicates
}

describe('i18n locale files', () => {
  it('ships balance explanations in English and Portuguese with verified English fallback elsewhere', async () => {
    const { default: i18n } = await import('@/lib/i18n')
    await i18n.loadLanguages(LOCALES)
    const english = JSON.parse(readRaw('en')).balanceExplanation
    const portuguese = JSON.parse(readRaw('pt-BR')).balanceExplanation
    expect(flattenKeys(portuguese).sort()).toEqual(flattenKeys(english).sort())
    expect(i18n.getFixedT('pt-BR')('balanceExplanation.title')).toBe('Detalhes do saldo')
    for (const locale of LOCALES.filter((locale) => !['en', 'pt-BR'].includes(locale))) {
      for (const [key, value] of flattenValues(english, 'balanceExplanation')) {
        expect(i18n.getFixedT(locale)(key)).toBe(value)
      }
    }
  })

  it('ships owned transfers in English and Brazilian Portuguese with explicit fallback elsewhere', async () => {
    const { default: i18n } = await import('@/lib/i18n')
    await i18n.loadLanguages(LOCALES)
    const english = JSON.parse(readRaw('en'))
    const portuguese = JSON.parse(readRaw('pt-BR'))
    expect(flattenKeys(portuguese.ownedTransfers).sort()).toEqual(flattenKeys(english.ownedTransfers).sort())
    const portugueseKeys = new Set(flattenKeys(portuguese))
    expect(OWNED_TRANSFER_SHARED_KEYS.filter((key) => !portugueseKeys.has(key))).toEqual([])
    const pt = i18n.getFixedT('pt-BR')
    expect(pt('ownedTransfers.originalCost')).toBe('Custo original de aquisição')
    expect(pt('ownedTransfers.performanceCost')).toBe('Base para cálculo de desempenho')
    expect(pt('ownedTransfers.fields.known_acquisition_cost')).toBe('Subtotal dos custos de aquisição conhecidos')
    expect(pt('ownedTransfers.failed')).toBe('Falhou')
    expect(pt('ownedTransfers.reviewFailed')).not.toBe(pt('ownedTransfers.failed'))
    expect(pt('ownedTransfers.settledEvidence')).not.toBe(pt('ownedTransfers.settled'))
    const enValues = flattenValues(english)
    const fallbackKeys = [...flattenValues(english.ownedTransfers, 'ownedTransfers').keys(), ...OWNED_TRANSFER_SHARED_KEYS]
    for (const locale of LOCALES.filter((locale) => !['en', 'pt-BR'].includes(locale))) {
      for (const key of fallbackKeys) expect(i18n.getFixedT(locale)(key)).toBe(enValues.get(key))
    }
  })

  it('ships historical evidence in English and Brazilian Portuguese with explicit fallback elsewhere', async () => {
    const { default: i18n } = await import('@/lib/i18n')
    await i18n.loadLanguages(LOCALES)
    const english = JSON.parse(readRaw('en')).history
    const portuguese = JSON.parse(readRaw('pt-BR')).history
    expect(flattenKeys(portuguese).sort()).toEqual(flattenKeys(english).sort())
    expect(i18n.getFixedT('pt-BR')('history.collect')).toBe('Coletar evidências')
    expect(i18n.getFixedT('pt-BR')('history.values.payload_unavailable')).toBe('dados originais indisponíveis')
    for (const locale of LOCALES.filter((locale) => !['en', 'pt-BR'].includes(locale))) {
      expect(i18n.getFixedT(locale)('history.collect')).toBe(english.collect)
    }
  })

  it('keeps Hindi translations and falls back to English for Lovenest-only keys', async () => {
    const { default: i18n } = await import('@/lib/i18n')
    await i18n.loadLanguages('hi')
    const hindi = i18n.getFixedT('hi')

    expect(hindi('nav.assets')).toBe(JSON.parse(readRaw('hi')).nav.assets)
    expect(i18n.getResource('hi', 'translation', 'nav.trace')).toBeUndefined()
    expect(hindi('nav.trace')).toBe(JSON.parse(readRaw('en')).nav.trace)
  })

  describe('no duplicate keys', () => {
    for (const locale of LOCALES) {
      it(`${locale}.json`, () => {
        const duplicates = findDuplicateKeys(readRaw(locale))
        expect(duplicates, `Duplicate keys: ${duplicates.join(', ')}`).toEqual([])
      })
    }
  })

  describe('all languages contain all keys from en.json', () => {
    const enKeys = new Set(flattenKeys(JSON.parse(readRaw('en'))))

    for (const locale of LOCALES.filter(
      (l: string) => l !== 'en' && !LOCALES_WITH_DOWNSTREAM_FALLBACK.has(l),
    )) {
      it(locale, () => {
        const keys = new Set(flattenKeys(JSON.parse(readRaw(locale))))
        // A key is covered if the locale has the key directly OR has at least one
        // i18next plural form of it (e.g. _one/_few/_many/_other for Polish).
        // #133 and #144 ship EN + PT-BR; other languages use the runtime
        // English fallback only for these namespaces and exact shared keys.
        const missing = [...enKeys].filter((k) =>
          !(locale !== 'pt-BR' && (k.startsWith('history.') || k.startsWith('ownedTransfers.') || k.startsWith('balanceExplanation.') || OWNED_TRANSFER_SHARED_KEYS.includes(k))) && !hasKeyOrPluralForms(keys, k),
        )
        expect(missing, `Keys missing in ${locale}:`).toEqual([])
      })
    }
  })

  describe('no extra keys not present in en.json', () => {
    const enKeys = new Set(flattenKeys(JSON.parse(readRaw('en'))))

    for (const locale of LOCALES.filter((l: string) => l !== 'en')) {
      it(locale, () => {
        const keys = new Set(flattenKeys(JSON.parse(readRaw(locale))))
        // A key is valid if it exists in en directly, OR if it is a plural form
        // of a key that exists in en (e.g. "foo_few" is valid when en has "foo").
        const extra = [...keys].filter((k) => {
          if (enKeys.has(k)) return false
          const base = pluralBase(k)
          return !(base && enKeys.has(base))
        })
        expect(extra, `Extra keys in ${locale} not in en:`).toEqual([])
      })
    }
  })

  describe('placeholder variables match en.json', () => {
    const enValues = flattenValues(JSON.parse(readRaw('en')))

    for (const locale of LOCALES.filter((l: string) => l !== 'en')) {
      it(locale, () => {
        const localeValues = flattenValues(JSON.parse(readRaw(locale)))
        const mismatches: string[] = []

        for (const [key, enValue] of enValues) {
          const enPlaceholders = extractPlaceholders(enValue)
          if (enPlaceholders.size === 0) continue

          // For plural-form locales the base key won't exist — check each plural form instead.
          const keysToCheck = localeValues.has(key)
            ? [key]
            : PLURAL_SUFFIXES.map((s) => `${key}${s}`).filter((k) => localeValues.has(k))

          for (const localeKey of keysToCheck) {
            const missing = [...enPlaceholders].filter(
              (p) => !extractPlaceholders(localeValues.get(localeKey)!).has(p),
            )
            if (missing.length > 0)
              mismatches.push(`${localeKey}: missing ${missing.map((p) => `{{${p}}}`).join(', ')}`)
          }
        }

        expect(mismatches, `Placeholder mismatches in ${locale}:`).toEqual([])
      })
    }
  })

  // Every screen that offers a language picker reads SUPPORTED_LANGS, so a
  // translation that ships a bundle without landing in that list is offered
  // nowhere. Check every bundle against the language picker's registry.
  it('offers every locale bundle in SUPPORTED_LANGS', () => {
    const source = readFileSync(path.join(LOCALES_DIR, '..', 'lib', 'i18n.ts'), 'utf-8')
    const block = source.match(/SUPPORTED_LANGS[^=]*=\s*\[([\s\S]*?)\]/)
    expect(block, 'SUPPORTED_LANGS not found in lib/i18n.ts').not.toBeNull()

    const offered = [...block![1].matchAll(/code:\s*'([^']+)'/g)].map((m) => m[1])
    expect(LOCALES.filter((locale) => !offered.includes(locale)).sort()).toEqual([])
    expect(offered.filter((code) => !LOCALES.includes(code)).sort()).toEqual([])
  })

  it('contains imported-description normalization labels in every locale', () => {
    const required = [
      'transactions.originalDescription',
      'rules.setDescription',
      'rules.descriptionValuePlaceholder',
      'rules.invalidDescriptionValue',
      'rules.fieldRawPayee',
    ]

    for (const locale of LOCALES) {
      const keys = new Set(flattenKeys(JSON.parse(readRaw(locale))))
      expect(
        required.filter((key) => !keys.has(key)),
        `Normalization labels missing in ${locale}:`,
      ).toEqual([])
    }
  })

  it('contains new recurrence labels in every locale', () => {
    const required = ['recurring.biweekly', 'recurring.semiannual']

    for (const locale of LOCALES) {
      const keys = new Set(flattenKeys(JSON.parse(readRaw(locale))))
      expect(
        required.filter((key) => !keys.has(key)),
        `Recurrence labels missing in ${locale}:`,
      ).toEqual([])
    }
  })
})
