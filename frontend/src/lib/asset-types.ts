import {
  Banknote,
  Bitcoin,
  Car,
  Gem,
  Home,
  Layers,
  LineChart,
  Package,
  PieChart,
  TrendingUp,
} from 'lucide-react'
import type { ElementType } from 'react'

export const ASSET_TYPE_CONFIG: Record<string, { icon: ElementType; color: string; bg: string }> = {
  real_estate: { icon: Home, color: 'text-blue-600', bg: 'bg-blue-100' },
  vehicle: { icon: Car, color: 'text-violet-600', bg: 'bg-violet-100' },
  valuable: { icon: Gem, color: 'text-amber-600', bg: 'bg-amber-100' },
  investment: { icon: TrendingUp, color: 'text-emerald-600', bg: 'bg-emerald-100' },
  stock: { icon: LineChart, color: 'text-sky-600', bg: 'bg-sky-100' },
  etf: { icon: Layers, color: 'text-teal-600', bg: 'bg-teal-100' },
  crypto: { icon: Bitcoin, color: 'text-orange-600', bg: 'bg-orange-100' },
  fund: { icon: PieChart, color: 'text-indigo-600', bg: 'bg-indigo-100' },
  cash_equivalent: { icon: Banknote, color: 'text-lime-600', bg: 'bg-lime-100' },
  other: { icon: Package, color: 'text-slate-600', bg: 'bg-slate-100' },
}

export function getTypeConfig(type: string) {
  return ASSET_TYPE_CONFIG[type] ?? ASSET_TYPE_CONFIG['other']
}

/** Translation key for an asset class, e.g. `real_estate` → `assets.typeRealEstate`. */
export function assetTypeI18nKey(type: string): string {
  const camel = type.replace(/_([a-z])/g, (_, c: string) => c.toUpperCase())
  return `assets.type${camel.charAt(0).toUpperCase()}${camel.slice(1)}`
}

export const ASSET_TYPES = [
  'stock',
  'etf',
  'crypto',
  'fund',
  // The user's call on what behaves as Liquid Cash — the backend only seeds
  // the guess for well-known tickers, and never overwrites this.
  'cash_equivalent',
  'real_estate',
  'vehicle',
  'valuable',
  'investment',
  'other',
] as const

// Map a yfinance `quoteType` to Securo's asset type. Lives here (not the
// backend) so if we ever swap the market-price provider the service stays
// clean — all provider-specific vocabulary is translated at the edge. The
// backend keeps its own copy for the holdings it creates without a client
// (`app/services/asset_transaction_service.py:_type_from_quote`), which also
// consults the seeded Cash Equivalent ticker list this side cannot see.
export function assetTypeFromQuoteType(quoteType: string | null | undefined): string {
  switch ((quoteType || '').toUpperCase()) {
    case 'EQUITY':
      return 'stock'
    case 'ETF':
      return 'etf'
    case 'CRYPTOCURRENCY':
      return 'crypto'
    case 'MUTUALFUND':
    case 'INDEX':
      return 'fund'
    case 'MONEYMARKET':
      return 'cash_equivalent'
    default:
      return 'investment'
  }
}
