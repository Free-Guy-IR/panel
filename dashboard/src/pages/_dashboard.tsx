import { Footer } from '@/components/Footer'
import { AppSidebar } from '@/components/layout/sidebar'
import PageTransition from '@/components/PageTransition'
import RouteGuard from '@/components/RouteGuard'
import { TopLoadingBar } from '@/components/TopLoadingBar'
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
    <SidebarProvider className=''>
      <RouteGuard>
        <TopLoadingBar />
        <div className='flex w-full flex-col lg:flex-row'>
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
