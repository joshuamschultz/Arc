"use client"

import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { Tabs as TabsPrimitive } from "radix-ui"

import { cn } from "@/lib/utils"

function Tabs({
  className,
  orientation = "horizontal",
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Root>) {
  return (
    <TabsPrimitive.Root
      data-slot="tabs"
      data-orientation={orientation}
      orientation={orientation}
      className={cn(
        "group/tabs flex gap-2 data-[orientation=horizontal]:flex-col",
        className
      )}
      {...props}
    />
  )
}

const tabsListVariants = cva(
  "group/tabs-list inline-flex w-fit items-center justify-center rounded-lg p-[3px] text-muted-foreground group-data-[orientation=horizontal]/tabs:h-9 group-data-[orientation=vertical]/tabs:h-fit group-data-[orientation=vertical]/tabs:flex-col data-[variant=line]:rounded-none",
  {
    variants: {
      variant: {
        default: "bg-muted",
        line: "gap-1 bg-transparent",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
)

const FADE = 'linear-gradient(to right, transparent 0, #000 28px, #000 calc(100% - 28px), transparent 100%)'
const FADE_END = 'linear-gradient(to right, #000 0, #000 calc(100% - 28px), transparent 100%)'
const FADE_START = 'linear-gradient(to right, transparent 0, #000 28px, #000 100%)'

/** Fade only the edge that hides more tabs, so nothing looks clipped by accident. */
function applyEdgeFade(el: HTMLElement) {
  const more = el.scrollWidth - el.clientWidth > 1
  const atStart = el.scrollLeft <= 1
  const atEnd = el.scrollLeft >= el.scrollWidth - el.clientWidth - 1
  const mask = !more ? 'none' : atStart ? FADE_END : atEnd ? FADE_START : FADE
  el.style.maskImage = mask
  el.style.webkitMaskImage = mask
}

/**
 * One horizontally scrollable row at every width: tabs never wrap, the row
 * snaps, fades the edge that hides more tabs, and keeps the active tab in view.
 */
function TabsList({
  className,
  variant = "default",
  ...props
}: React.ComponentProps<typeof TabsPrimitive.List> &
  VariantProps<typeof tabsListVariants>) {
  const scrollerRef = React.useRef<HTMLDivElement>(null)

  React.useEffect(() => {
    const scroller = scrollerRef.current
    const list = scroller?.querySelector<HTMLElement>('[role="tablist"]')
    if (!scroller || !list) return
    const reveal = () => {
      const active = list.querySelector<HTMLElement>('[role="tab"][data-state="active"]')
      if (typeof active?.scrollIntoView === 'function') {
        active.scrollIntoView({ inline: 'center', block: 'nearest', behavior: 'smooth' })
      }
    }
    const refresh = () => applyEdgeFade(scroller)
    reveal()
    refresh()
    scroller.addEventListener('scroll', refresh, { passive: true })
    const mutations = new MutationObserver(() => { reveal(); refresh() })
    mutations.observe(list, { attributes: true, attributeFilter: ['data-state'], subtree: true, childList: true })
    const resizes = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(refresh)
    resizes?.observe(scroller)
    return () => {
      scroller.removeEventListener('scroll', refresh)
      mutations.disconnect()
      resizes?.disconnect()
    }
  }, [])

  return (
    <div
      ref={scrollerRef}
      data-slot="tabs-scroller"
      className="max-w-full snap-x snap-mandatory overflow-x-auto overflow-y-hidden pb-1.5 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
    >
      <TabsPrimitive.List
        data-slot="tabs-list"
        data-variant={variant}
        className={cn(tabsListVariants({ variant }), className, "w-max min-w-full flex-nowrap max-md:h-auto!")}
        {...props}
      />
    </div>
  )
}

function TabsTrigger({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Trigger>) {
  return (
    <TabsPrimitive.Trigger
      data-slot="tabs-trigger"
      className={cn(
        "relative inline-flex h-[calc(100%-1px)] shrink-0 snap-start flex-1 max-md:h-11 items-center justify-center gap-1.5 rounded-md border border-transparent px-2.5 py-1 text-sm font-medium whitespace-nowrap text-muted-foreground transition-all duration-150 group-data-[orientation=vertical]/tabs:w-full group-data-[orientation=vertical]/tabs:justify-start hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/60 disabled:pointer-events-none disabled:opacity-50 group-data-[variant=default]/tabs-list:data-[state=active]:shadow-sm group-data-[variant=line]/tabs-list:data-[state=active]:shadow-none [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
        "group-data-[variant=line]/tabs-list:bg-transparent group-data-[variant=line]/tabs-list:data-[state=active]:bg-transparent dark:group-data-[variant=line]/tabs-list:data-[state=active]:border-transparent dark:group-data-[variant=line]/tabs-list:data-[state=active]:bg-transparent",
        "data-[state=active]:bg-card data-[state=active]:text-foreground dark:data-[state=active]:border-input dark:data-[state=active]:bg-input/40 dark:data-[state=active]:text-foreground",
        "after:absolute after:bg-primary after:opacity-0 after:transition-opacity group-data-[orientation=horizontal]/tabs:after:inset-x-0 group-data-[orientation=horizontal]/tabs:after:bottom-[-5px] group-data-[orientation=horizontal]/tabs:after:h-0.5 group-data-[orientation=vertical]/tabs:after:inset-y-0 group-data-[orientation=vertical]/tabs:after:-right-1 group-data-[orientation=vertical]/tabs:after:w-0.5 group-data-[variant=line]/tabs-list:data-[state=active]:after:opacity-100",
        className
      )}
      {...props}
    />
  )
}

function TabsContent({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Content>) {
  return (
    <TabsPrimitive.Content
      data-slot="tabs-content"
      className={cn("flex-1 outline-none", className)}
      {...props}
    />
  )
}

export { Tabs, TabsList, TabsTrigger, TabsContent, tabsListVariants }
