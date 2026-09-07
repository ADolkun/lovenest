import type { ComponentProps } from 'react'
import { ChevronDown } from 'lucide-react'
import { cn } from '@/lib/utils'

/** Keep native keyboard and touch selection, with a consistently inset arrow. */
export function NativeSelect({
  className, wrapperClassName, ...props
}: ComponentProps<'select'> & { wrapperClassName?: string }) {
  return (
    <div className={cn('relative min-w-0', wrapperClassName)}>
      <select {...props} className={cn('w-full', className, 'appearance-none pe-9')} />
      <ChevronDown
        aria-hidden="true"
        className="pointer-events-none absolute end-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground"
      />
    </div>
  )
}
