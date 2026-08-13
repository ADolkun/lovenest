import { useTranslation } from 'react-i18next'
import { Label } from '@/components/ui/label'

interface ReviewAccountsToggleProps {
  id: string
  checked: boolean
  onChange: (checked: boolean) => void
  disabled?: boolean
}

/** The "review accounts before importing" choice, offered by every connect flow.
 *
 * Ticked, the connect creates the connection with an empty allowlist — the
 * tri-state's "sync nothing" — so nothing is imported before the user picks.
 */
export function ReviewAccountsToggle({
  id,
  checked,
  onChange,
  disabled,
}: ReviewAccountsToggleProps) {
  const { t } = useTranslation()

  return (
    <div className="flex items-start justify-between gap-4 rounded-lg border border-border p-3">
      <div className="space-y-1">
        <Label htmlFor={id}>{t('connections.reviewAccountsFirst')}</Label>
        <p className="text-xs text-muted-foreground">{t('connections.reviewAccountsFirstHint')}</p>
      </div>
      <input
        id={id}
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        disabled={disabled}
        className="mt-1 h-4 w-4 rounded border-border text-primary focus:ring-primary"
      />
    </div>
  )
}
