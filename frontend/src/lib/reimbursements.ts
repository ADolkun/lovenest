import type { ReimbursementLink } from '@/types'

export function linkedTotal(links: ReimbursementLink[]): number {
  return roundCents(links.reduce((sum, link) => sum + Number(link.amount), 0))
}

export function roundCents(value: number): number {
  return Math.round(value * 100) / 100
}
