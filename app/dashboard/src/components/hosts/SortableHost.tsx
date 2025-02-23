import {
    useSortable,
} from "@dnd-kit/sortable"
import { CSS } from "@dnd-kit/utilities"

import { HostResponse, removeHost } from "@/service/api"
import { Card } from "../ui/card"
import { Copy, GripVertical, MoreVertical, Pencil, Power, Trash2 } from "lucide-react"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "../ui/dropdown-menu"
import { Button } from "../ui/button"
import { useTranslation } from "react-i18next"
import useDirDetection from "@/hooks/use-dir-detection"
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from "../ui/alert-dialog"
import { cn } from "@/lib/utils"
import { toast } from "@/hooks/use-toast"
import { useState } from "react"
import { queryClient } from "@/utils/query-client"
import { useMutation } from "@tanstack/react-query"

interface SortableHostProps {
    host: HostResponse
}

const DeleteAlertDialog = ({
    host,
    isOpen,
    onClose,
    onConfirm,
}: {
    host: HostResponse;
    isOpen: boolean;
    onClose: () => void;
    onConfirm: () => void;
}) => {
    const { t } = useTranslation();
    const dir = useDirDetection();

    return (
        <div>
            <AlertDialog open={isOpen} onOpenChange={onClose}>
                <AlertDialogContent>
                    <AlertDialogHeader className={cn(dir === "rtl" && "sm:text-right")}>
                        <AlertDialogTitle>{t("deleteHost.title")}</AlertDialogTitle>
                        <AlertDialogDescription>
                            <span dir={dir} dangerouslySetInnerHTML={{ __html: t("deleteHost.prompt", { name: host.inbound_tag }) }} />
                        </AlertDialogDescription>
                    </AlertDialogHeader>
                    <AlertDialogFooter className={cn(dir === "rtl" && "sm:gap-x-2 sm:flex-row-reverse")}>
                        <AlertDialogCancel onClick={onClose}>{t("cancel")}</AlertDialogCancel>
                        <AlertDialogAction variant="destructive" onClick={onConfirm}>
                            {t("delete")}
                        </AlertDialogAction>
                    </AlertDialogFooter>
                </AlertDialogContent>
            </AlertDialog>
        </div>
    );
};

export default function SortableHost({ host }: SortableHostProps) {
    const [isDeleteDialogOpen, setDeleteDialogOpen] = useState(false);
    const dir = useDirDetection()
    const { t } = useTranslation();
    const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: host.id })

    const style = {
        transform: CSS.Transform.toString(transform),
        transition,
        zIndex: isDragging ? 2 : 1,
        opacity: isDragging ? 0.8 : 1,
    }
    const cursor = isDragging ? "grabbing" : "grab";


    const handleDeleteClick = () => {
        setDeleteDialogOpen(true);
    };

    const handleCloseDeleteDialog = () => {
        setDeleteDialogOpen(false);
    };

    const handleConfirmDelete = async () => {
        try {
            await removeHost(host.id)
            toast({
                dir,
                description: t("deleteHost.deleteSuccess", { name: host.inbound_tag }),
            })
        }
        catch (error) {
            toast({
                dir,
                description: t("deleteHost.deleteFailed", { name: host.inbound_tag }),
            })
        } finally {
            queryClient.invalidateQueries({
                queryKey: ["getGetHostsQueryKey"],
            });
            setDeleteDialogOpen(false);
        }

    };

    return (
        <div ref={setNodeRef} className="cursor-default" style={style} {...attributes}>
            <Card className="p-4 relative group h-full">
                <div className="flex items-center gap-3">
                    <button style={{ cursor: cursor }} className="touch-none opacity-50 group-hover:opacity-100 transition-opacity" {...listeners}>
                        <GripVertical className="h-5 w-5" />
                        <span className="sr-only">Drag to reorder</span>
                    </button>
                    <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2">
                            <div className="min-h-2 min-w-2 rounded-full bg-green-500" />
                            <div className="font-medium truncate">{host.remark}</div>
                        </div>
                        <div className="text-sm text-muted-foreground truncate">{host.address}</div>
                    </div>
                    <DropdownMenu>
                        <DropdownMenuTrigger asChild>
                            <Button variant="ghost" size="icon">
                                <MoreVertical className="h-4 w-4" />
                            </Button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end">
                            <DropdownMenuItem onSelect={() => "toggleHostStatus(host.id)"}>
                                <Power className="h-4 w-4 mr-2" />
                                {host?.is_disabled ? "Enable" : "Disable"}
                            </DropdownMenuItem>
                            <DropdownMenuSeparator />
                            <DropdownMenuItem>
                                <Pencil className="h-4 w-4 mr-2" />
                                Edit
                            </DropdownMenuItem>
                            <DropdownMenuItem onSelect={() => "handleDuplicate(host)"}>
                                <Copy className="h-4 w-4 mr-2" />
                                Duplicate
                            </DropdownMenuItem>
                            <DropdownMenuItem onClick={handleDeleteClick}
                                className="text-destructive" onSelect={() => "handleDelete(host.id)"}>
                                <Trash2 className="h-4 w-4 mr-2" />
                                Trash
                            </DropdownMenuItem>
                        </DropdownMenuContent>
                    </DropdownMenu>
                </div>
            </Card>
            {/* Include the Delete AlertDialog component */}
            <div>
                <DeleteAlertDialog
                    host={host}
                    isOpen={isDeleteDialogOpen}
                    onClose={handleCloseDeleteDialog}
                    onConfirm={handleConfirmDelete}
                />
            </div>
        </div>
    )
}