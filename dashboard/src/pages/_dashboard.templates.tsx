import PageHeader from '@/components/page-header'
import { useTranslation } from 'react-i18next'
import { Outlet } from 'react-router'

const Templates = () => {
  const { t } = useTranslation()

  return (
    <div className="flex flex-col gap-0 w-full items-start">
      <PageHeader title="templates.title" description="createAndManageTemplates" />
      <div className="w-full px-4">
          <Outlet />
      </div>
    </div>
  )
}

export default Templates
