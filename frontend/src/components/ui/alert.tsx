import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

const alertVariants = cva(
  "flex items-start gap-2.5 rounded-lg border px-3.5 py-3 text-sm [&>svg]:shrink-0",
  {
    variants: {
      variant: {
        default: "border-border bg-muted/40 text-muted-foreground",
        warning: "border-warning/30 bg-warning/10 text-warning-foreground",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
)

function Alert({
  className,
  variant = "default",
  ...props
}: React.ComponentProps<"div"> & VariantProps<typeof alertVariants>) {
  return (
    <div
      role="alert"
      data-slot="alert"
      data-variant={variant}
      className={cn(alertVariants({ variant }), className)}
      {...props}
    />
  )
}

export { Alert }
