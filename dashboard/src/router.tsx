import { Suspense, lazy } from 'react'
import { useAdmin } from '@/hooks/use-admin'
import { getCurrentAdmin } from '@/service/api'
import { createHashRouter, Navigate, RouteObject } from 'react-router'
// Replace direct imports with lazy imports for route-level components
const CoreSettings = lazy(() => import('@/pages/_dashboard.nodes.cores'))
const ThemePage = lazy(() => import('@/pages/_dashboard.settings.theme'))
const DashboardLayout = lazy(() => import('./pages/_dashboard'))
const Dashboard = lazy(() => import('./pages/_dashboard._index'))
const AdminsPage = lazy(() => import('./pages/_dashboard.admins'))
const BulkPage = lazy(() => import('./pages/_dashboard.bulk'))
const BulkDataPage = lazy(() => import('./pages/_dashboard.bulk.data'))
const BulkExpirePage = lazy(() => import('./pages/_dashboard.bulk.expire'))
const BulkGroupsPage = lazy(() => import('./pages/_dashboard.bulk.groups'))
const BulkProxyPage = lazy(() => import('./pages/_dashboard.bulk.proxy'))
const Groups = lazy(() => import('./pages/_dashboard.groups'))
const Hosts = lazy(() => import('./pages/_dashboard.hosts'))
const Nodes = lazy(() => import('./pages/_dashboard.nodes'))
const NodesPage = lazy(() => import('./pages/_dashboard.nodes._index'))
const NodeLogs = lazy(() => import('./pages/_dashboard.nodes.logs'))
const Settings = lazy(() => import('./pages/_dashboard.settings'))
const CleanupSettings = lazy(() => import('./pages/_dashboard.settings.cleanup'))
const DiscordSettings = lazy(() => import('./pages/_dashboard.settings.discord'))
const GeneralSettings = lazy(() => import('./pages/_dashboard.settings.general'))
const NotificationSettings = lazy(() => import('./pages/_dashboard.settings.notifications'))
const SubscriptionSettings = lazy(() => import('./pages/_dashboard.settings.subscriptions'))
const TelegramSettings = lazy(() => import('./pages/_dashboard.settings.telegram'))
const WebhookSettings = lazy(() => import('./pages/_dashboard.settings.webhook'))
const Statistics = lazy(() => import('./pages/_dashboard.statistics'))
const UserTemplates = lazy(() => import('./pages/_dashboard.templates'))
const Users = lazy(() => import('./pages/_dashboard.users'))
const Login = lazy(() => import('./pages/login'))

// Component to handle default settings routing based on user permissions
function SettingsIndex() {
  const { admin } = useAdmin()
  const is_sudo = admin?.is_sudo || false

  // For sudo admins, default to notifications; for non-sudo admins, default to theme
  const defaultPath = is_sudo ? '/settings/general' : '/settings/theme'

  return <Navigate to={defaultPath} replace />
}

const fetchAdminLoader = async (): Promise<any> => {
  try {
    const response = await getCurrentAdmin()
    return response
  } catch (error) {
    throw Response.redirect('/login')
  }
}

// Wrap all route elements in <Suspense fallback={<div>Loading...</div>}>
export const router = createHashRouter([
  {
    element: <Suspense fallback={<div>Loading...</div>}><DashboardLayout /></Suspense>,
    errorElement: <Suspense fallback={<div>Loading...</div>}><Login /></Suspense>,
    loader: fetchAdminLoader,
    children: [
      {
        path: '/',
        index: true,
        element: <Suspense fallback={<div>Loading...</div>}><Dashboard /></Suspense>,
      },
      {
        path: '/users',
        element: <Suspense fallback={<div>Loading...</div>}><Users /></Suspense>,
      },
      {
        path: '/statistics',
        element: <Suspense fallback={<div>Loading...</div>}><Statistics /></Suspense>,
      },
      {
        path: '/hosts',
        element: <Suspense fallback={<div>Loading...</div>}><Hosts /></Suspense>,
      },
      {
        path: '/nodes',
        element: <Suspense fallback={<div>Loading...</div>}><Nodes /></Suspense>,
        children: [
          {
            path: '/nodes',
            element: <Suspense fallback={<div>Loading...</div>}><NodesPage /></Suspense>,
          },
          {
            path: '/nodes/cores',
            element: <Suspense fallback={<div>Loading...</div>}><CoreSettings /></Suspense>,
          },
          {
            path: '/nodes/logs',
            element: <Suspense fallback={<div>Loading...</div>}><NodeLogs /></Suspense>,
          },
        ],
      },
      {
        path: '/groups',
        element: <Suspense fallback={<div>Loading...</div>}><Groups /></Suspense>,
      },
      {
        path: '/templates',
        element: <Suspense fallback={<div>Loading...</div>}><UserTemplates /></Suspense>,
      },
      {
        path: '/admins',
        element: <Suspense fallback={<div>Loading...</div>}><AdminsPage /></Suspense>,
      },
      {
        path: '/settings',
        element: <Suspense fallback={<div>Loading...</div>}><Settings /></Suspense>,
        children: [
          {
            path: '/settings',
            index: true,
            element: <Suspense fallback={<div>Loading...</div>}><SettingsIndex /></Suspense>,
          },
          {
            path: '/settings/general',
            element: <Suspense fallback={<div>Loading...</div>}><GeneralSettings /></Suspense>,
          },
          {
            path: '/settings/notifications',
            element: <Suspense fallback={<div>Loading...</div>}><NotificationSettings /></Suspense>,
          },
          {
            path: '/settings/subscriptions',
            element: <Suspense fallback={<div>Loading...</div>}><SubscriptionSettings /></Suspense>,
          },
          {
            path: '/settings/telegram',
            element: <Suspense fallback={<div>Loading...</div>}><TelegramSettings /></Suspense>,
          },
          {
            path: '/settings/discord',
            element: <Suspense fallback={<div>Loading...</div>}><DiscordSettings /></Suspense>,
          },
          {
            path: '/settings/webhook',
            element: <Suspense fallback={<div>Loading...</div>}><WebhookSettings /></Suspense>,
          },
          {
            path: '/settings/cleanup',
            element: <Suspense fallback={<div>Loading...</div>}><CleanupSettings /></Suspense>,
          },
          {
            path: '/settings/theme',
            element: <Suspense fallback={<div>Loading...</div>}><ThemePage /></Suspense>,
          },
        ],
      },
      {
        path: '/bulk',
        element: <Suspense fallback={<div>Loading...</div>}><BulkPage /></Suspense>,
        children: [
          {
            path: '/bulk',
            element: <Suspense fallback={<div>Loading...</div>}><BulkGroupsPage /></Suspense>,
          },
          {
            path: '/bulk/proxy',
            element: <Suspense fallback={<div>Loading...</div>}><BulkProxyPage /></Suspense>,
          },
          {
            path: '/bulk/expire',
            element: <Suspense fallback={<div>Loading...</div>}><BulkExpirePage /></Suspense>,
          },
          {
            path: '/bulk/data',
            element: <Suspense fallback={<div>Loading...</div>}><BulkDataPage /></Suspense>,
          },
        ],
      },
      {
        path: 'theme',
        element: <Suspense fallback={<div>Loading...</div>}><ThemePage /></Suspense>,
      },
    ],
  },
  {
    path: '/login',
    element: <Suspense fallback={<div>Loading...</div>}><Login /></Suspense>,
  },
] as RouteObject[])
