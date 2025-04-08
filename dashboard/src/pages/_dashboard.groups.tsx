import PageHeader from '@/components/page-header'
import { Separator } from '@/components/ui/separator'
import { Plus } from 'lucide-react'
import Groups from '@/components/groups/Groups'
import { useState } from 'react'

export default function GroupsPage() {
  const [isDialogOpen, setIsDialogOpen] = useState(false)

  return (
    <div className="flex flex-col gap-2 w-full items-start">
      <PageHeader
        title="groups"
        description="manageGroups"
        buttonIcon={Plus}
        buttonText="createGroup"
        onButtonClick={() => setIsDialogOpen(true)}
      />
      <Separator />
      <Groups isDialogOpen={isDialogOpen} onOpenChange={setIsDialogOpen} />
    </div>
  )
} 