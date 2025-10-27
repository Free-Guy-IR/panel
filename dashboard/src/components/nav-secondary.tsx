import { SidebarGroup, SidebarGroupContent, SidebarGroupLabel, SidebarMenu, SidebarMenuButton, SidebarMenuItem, useSidebar } from '@/components/ui/sidebar'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { type LucideIcon } from 'lucide-react'
import * as React from 'react'
import { useTranslation } from 'react-i18next'

export function NavSecondary({
  items,
  label,
  ...props
}: {
  label?: string
  items: {
    title: string
    url: string
    icon: LucideIcon
    target?: string
  }[]
} & React.ComponentPropsWithoutRef<typeof SidebarGroup>) {
  const { t } = useTranslation()
  const { state, isMobile } = useSidebar()

  // Collapsed state (desktop only) - show only icons with popover
  // On mobile, always use expanded UI since there's no collapsed sidebar concept
  if (state === 'collapsed' && !isMobile) {
    return (
      <SidebarGroup {...props}>
        <SidebarMenu>
          {items.map(item => (
            <SidebarMenuItem key={item.title}>
              {item.title === t('supportUs') ? (
                // Direct link for Support Us
                <SidebarMenuButton asChild tooltip={t(item.title)}>
                  <a href={item.url} target={item.target}>
                    <item.icon />
                  </a>
                </SidebarMenuButton>
              ) : (
                // Popover for other items
                <Popover>
                  <PopoverTrigger asChild>
                    <SidebarMenuButton tooltip={t(item.title)}>
                      <item.icon />
                    </SidebarMenuButton>
                  </PopoverTrigger>
                  <PopoverContent className="w-48 p-3" side="right" align="start">
                    <div className="space-y-2">
                      <div className="flex items-center gap-2">
                        <item.icon className="h-4 w-4" />
                        <span className="font-semibold text-sm">{t(item.title)}</span>
                      </div>
                      <Button asChild className="w-full">
                        <a href={item.url} target={item.target} className="flex items-center justify-center gap-2">
                          {t('open', { defaultValue: 'Open' })}
                        </a>
                      </Button>
                    </div>
                  </PopoverContent>
                </Popover>
              )}
            </SidebarMenuItem>
          ))}
        </SidebarMenu>
      </SidebarGroup>
    )
  }

  // Expanded state - full sidebar group
  return (
    <SidebarGroup {...props}>
      {!!label && <SidebarGroupLabel>{t(label)}</SidebarGroupLabel>}
      <SidebarGroupContent>
        <SidebarMenu>
          {items.map(item => (
            <SidebarMenuItem key={item.title}>
              <SidebarMenuButton asChild>
                <a href={item.url} target={item.target}>
                  <item.icon />
                  <span>{t(item.title)}</span>
                </a>
              </SidebarMenuButton>
            </SidebarMenuItem>
          ))}
        </SidebarMenu>
      </SidebarGroupContent>
    </SidebarGroup>
  )
}
