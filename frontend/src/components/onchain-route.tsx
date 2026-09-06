import { Navigate } from 'react-router-dom'
import { useFeatureFlags } from '@/hooks/use-feature-flags'

/** Wraps /trace so navigating straight to the URL with ONCHAIN_ENABLED off
 *  lands on home rather than a page whose every API call 404s. Mirrors
 *  AgentsRoute. */
export function OnChainRoute({ children }: { children: React.ReactNode }) {
  const { onchainEnabled, isLoading } = useFeatureFlags()
  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary" />
      </div>
    )
  }
  if (!onchainEnabled) return <Navigate to="/" replace />
  return <>{children}</>
}
