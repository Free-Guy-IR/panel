import { Footer } from '@/components/footer'
import { AppSidebar } from '@/components/layout/sidebar'
import PageTransition from '@/components/page-transition'
import RouteGuard from '@/components/route-guard'
import { TopLoadingBar } from '@/components/top-loading-bar'
import { SidebarInset, SidebarProvider } from '@/components/ui/sidebar'
import { getCurrentAdmin } from '@/service/api'
import { Outlet } from 'react-router'

export const clientLoader = async (): Promise<any> => {
  try {
    const response = await getCurrentAdmin()
    return response
  } catch (error) {
    throw Response.redirect('/login')
  }
}

export default function DashboardLayout() {
  return (
    <SidebarProvider className="">
      <RouteGuard>
        <TopLoadingBar />
        <div className="flex w-full flex-col lg:flex-row">
          <AppSidebar />
          <SidebarInset>
            <div className="flex min-h-screen w-full flex-col justify-between gap-y-4">
              <PageTransition duration={250}>
                <Outlet />
              </PageTransition>
              <Footer />
            </div>
          </SidebarInset>
        </div>
      </RouteGuard>
    </SidebarProvider>
  )
}
