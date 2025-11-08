import { useGetAllGroups, useModifyGroup } from '@/service/api'
import { GroupResponse } from '@/service/api'
import Group from './Group'
import { useState } from 'react'
import GroupModal, { groupFormSchema, GroupFormValues } from '@/components/dialogs/GroupModal'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { ScrollArea } from '@/components/ui/scroll-area'
import { toast } from 'sonner'
import { useTranslation } from 'react-i18next'
import { queryClient } from '@/utils/query-client'
import useDirDetection from '@/hooks/use-dir-detection'
import { Skeleton } from '@/components/ui/skeleton'
import { Card } from '@/components/ui/card'

const initialDefaultValues: Partial<GroupFormValues> = {
  name: '',
  inbound_tags: [],
  is_disabled: false,
}

interface GroupsProps {
  isDialogOpen: boolean
  onOpenChange: (open: boolean) => void
}

export default function Groups({ isDialogOpen, onOpenChange }: GroupsProps) {
  const [editingGroup, setEditingGroup] = useState<GroupResponse | null>(null)
  const { t } = useTranslation()
  const modifyGroupMutation = useModifyGroup()
  const dir = useDirDetection()
  const { data: groupsData, isLoading } = useGetAllGroups({})

  const form = useForm<GroupFormValues>({
    resolver: zodResolver(groupFormSchema),
    defaultValues: initialDefaultValues,
  })

  const handleEdit = (group: GroupResponse) => {
    setEditingGroup(group)
    form.reset({
      name: group.name,
      inbound_tags: group.inbound_tags || [],
      is_disabled: group.is_disabled,
    })
    onOpenChange(true)
  }

  const handleToggleStatus = async (group: GroupResponse) => {
    try {
      await modifyGroupMutation.mutateAsync({
        groupId: group.id,
        data: {
          name: group.name,
          inbound_tags: group.inbound_tags,
          is_disabled: !group.is_disabled,
        },
      })

      toast.success(t('success', { defaultValue: 'Success' }), {
        description: t(group.is_disabled ? 'group.enableSuccess' : 'group.disableSuccess', {
          name: group.name,
          defaultValue: `Group "{name}" has been ${group.is_disabled ? 'enabled' : 'disabled'} successfully`,
        }),
      })

      // Invalidate the groups query to refresh the list
      queryClient.invalidateQueries({
        queryKey: ['/api/groups'],
      })
    } catch (error) {
      toast.error(t('error', { defaultValue: 'Error' }), {
        description: t(group.is_disabled ? 'group.enableFailed' : 'group.disableFailed', {
          name: group.name,
          defaultValue: `Failed to ${group.is_disabled ? 'enable' : 'disable'} group "{name}"`,
        }),
      })
    }
  }

  return (
    <div className="w-full flex-1 space-y-4 pt-4">
      <ScrollArea className="h-[calc(100vh-8rem)]">
        <div dir={dir} className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          {isLoading
            ? [...Array(6)].map((_, i) => (
                <Card key={i} className="px-4 py-5">
                  <div className="flex items-center gap-2 sm:gap-3">
                    <Skeleton className="h-8 w-8 shrink-0 rounded-full" />
                    <div className="min-w-0 flex-1 space-y-2">
                      <Skeleton className="h-5 w-24 sm:w-32" />
                      <Skeleton className="h-4 w-20 sm:w-24" />
                    </div>
                    <Skeleton className="h-8 w-8 shrink-0" />
                  </div>
                </Card>
              ))
            : groupsData?.groups.map(group => <Group key={group.id} group={group} onEdit={handleEdit} onToggleStatus={handleToggleStatus} />)}
        </div>
      </ScrollArea>

      <GroupModal
        isDialogOpen={isDialogOpen}
        onOpenChange={(open: boolean) => {
          if (!open) {
            setEditingGroup(null)
            form.reset(initialDefaultValues)
          }
          onOpenChange(open)
        }}
        form={form}
        editingGroup={!!editingGroup}
        editingGroupId={editingGroup?.id}
      />
    </div>
  )
}
