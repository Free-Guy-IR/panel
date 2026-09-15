import type { ComponentType } from 'react'
import type { LucideIcon } from 'lucide-react'
import type { RouteObject } from 'react-router'

export type ForkRouteParent = 'root' | 'settings' | 'bulk' | 'nodes'

export type ForkNavSlot = 'main' | 'settings' | 'bulk'

export type ForkNavItem = {
  id: string
  label: string
  url: string
  icon?: LucideIcon
  title?: string
  description?: string
}

type ForkRouteEntry = {
  parent: ForkRouteParent
  route: RouteObject
}

const routes: ForkRouteEntry[] = []
const components = new Map<string, Map<string, ComponentType<any>>>()
const navItems = new Map<ForkNavSlot, ForkNavItem[]>()

export function registerRoute(route: RouteObject, parent: ForkRouteParent = 'root'): void {
  const path = route.path
  if (path && routes.some(entry => entry.parent === parent && entry.route.path === path)) {
    return
  }
  routes.push({ parent, route })
}

export function extraRoutes(parent?: ForkRouteParent): RouteObject[] {
  return routes.filter(entry => parent == null || entry.parent === parent).map(entry => entry.route)
}

export function registerComponent(slot: string, id: string, component: ComponentType<any>): void {
  let bucket = components.get(slot)
  if (!bucket) {
    bucket = new Map()
    components.set(slot, bucket)
  }
  bucket.set(id, component)
}

export function extraComponents(slot: string): Record<string, ComponentType<any>> {
  return Object.fromEntries(components.get(slot) ?? [])
}

export function extraComponent(slot: string, id: string): ComponentType<any> | undefined {
  return components.get(slot)?.get(id)
}

export function registerHostSection(id: string, component: ComponentType<any>): void {
  registerComponent('host-section', id, component)
}

export function extraHostSections(): Record<string, ComponentType<any>> {
  return extraComponents('host-section')
}

export function registerCoreEditor(id: string, component: ComponentType<any>): void {
  registerComponent('core-editor', id, component)
}

export function extraCoreEditors(): Record<string, ComponentType<any>> {
  return extraComponents('core-editor')
}

export function extraCoreEditor(id: string): ComponentType<any> | undefined {
  return extraComponent('core-editor', id)
}

export function registerNavItem(slot: ForkNavSlot, item: ForkNavItem): void {
  const existing = navItems.get(slot) ?? []
  if (existing.some(entry => entry.id === item.id || entry.url === item.url)) {
    return
  }
  navItems.set(slot, [...existing, item])
}

export function extraNavItems(slot: ForkNavSlot): ForkNavItem[] {
  return [...(navItems.get(slot) ?? [])]
}

export const registerDashboardComponent = registerComponent
export const extraDashboardComponents = extraComponents
