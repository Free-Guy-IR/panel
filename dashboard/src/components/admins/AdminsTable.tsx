import { Button } from '@/components/ui/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { useTranslation } from 'react-i18next'
import { Edit2, Trash2 } from 'lucide-react'

interface Admin {
  id: string
  username: string
  is_sudo: boolean
  created_at: string
}

interface AdminsTableProps {
  data: Admin[]
  onEdit: (admin: Admin) => void
  onDelete: (admin: Admin) => void
}

export default function AdminsTable({ data, onEdit, onDelete }: AdminsTableProps) {
  const { t } = useTranslation()

  return (
    <div className="rounded-md border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>{t('username')}</TableHead>
            <TableHead>{t('role')}</TableHead>
            <TableHead>{t('createdAt')}</TableHead>
            <TableHead className="text-right">{t('actions')}</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {data.map((admin) => (
            <TableRow key={admin.id}>
              <TableCell>{admin.username}</TableCell>
              <TableCell>{admin.is_sudo ? t('sudo') : t('admin')}</TableCell>
              <TableCell>{new Date(admin.created_at).toLocaleDateString()}</TableCell>
              <TableCell className="text-right">
                <div className="flex justify-end gap-2">
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={() => onEdit(admin)}
                    title={t('edit')}
                  >
                    <Edit2 className="h-4 w-4" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={() => onDelete(admin)}
                    title={t('delete')}
                  >
                    <Trash2 className="h-4 w-4 text-destructive" />
                  </Button>
                </div>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
} 