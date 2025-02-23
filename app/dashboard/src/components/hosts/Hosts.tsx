"use client"

import { useEffect, useState } from "react"
import { useForm } from "react-hook-form"
import { zodResolver } from "@hookform/resolvers/zod"
import { z } from "zod"
import { HostResponse, modifyHosts, ProxyHostFingerprint } from "@/service/api"
import AddHostModal from "../dialogs/AddHostModal"
import { closestCenter, DndContext, DragEndEvent, KeyboardSensor, PointerSensor, useSensor, useSensors } from "@dnd-kit/core"
import { arrayMove, rectSortingStrategy, SortableContext, sortableKeyboardCoordinates } from "@dnd-kit/sortable"
import SortableHost from "./SortableHost"
import { useQuery } from "@tanstack/react-query"

const hostFormSchema = z.object({
    remark: z.string().min(1, "Remark is required"),
    address: z.string().min(1, "Address is required"),
    inbound_tag: z.string().min(1, "Inbound tag is required"),
    port: z.number(),
    sni: z.string().nullable(),
    host: z.string().nullable(),
    path: z.string().nullable().optional(),
    security: z.enum(["none", "tls", "inbound_default"]),
    alpn: z.enum([
        "",
        "h3",
        "h2",
        "http/1.1",
        "h3,h2,http/1.1",
        "h3,h2",
        "h2,http/1.1",
    ]),
    fingerprint: z.string(),
    allowinsecure: z.boolean(),
    is_disabled: z.boolean(),
    mux_enable: z.boolean(),
    fragment_setting: z
        .object({
            packets: z.string(),
            length: z.string(),
            interval: z.string(),
        })
        .nullable(),
    random_user_agent: z.boolean(),
    noise_setting: z
        .object({
            noise_pattern: z.string(),
            noise_payload: z.string(),
        })
        .nullable(),
    use_sni_as_host: z.boolean(),
});


export type HostFormValues = z.infer<typeof hostFormSchema>;

interface HostsProps {
    data: HostResponse[] | undefined;
    onAddHost: (open: boolean) => void
    isDialogOpen: boolean
}

export default function Hosts({ data, onAddHost, isDialogOpen }: HostsProps) {
    const [hosts, setHosts] = useState<HostResponse[] | undefined>();
    const [editingHost, setEditingHost] = useState<HostResponse | null>(null);
    const [debouncedHosts, setDebouncedHosts] = useState<HostResponse[] | undefined>([]);

    // useQuery({
    //     queryKey: ["modifyHosts", debouncedHosts],
    //     queryFn: () => modifyHosts(debouncedHosts ?? []),
    //     enabled: !!debouncedHosts,
    // });    

    useEffect(() => {
        setHosts(data ?? [])
    }, [data])    

    const form = useForm<HostFormValues>({
        resolver: zodResolver(hostFormSchema),
        defaultValues: {
            remark: "",
            address: "",
            inbound_tag: "",
            sni: null,
            host: null,
            path: null,
            security: "none",
            alpn: "h2",
            fingerprint: "",
            allowinsecure: false,
            is_disabled: false,
            mux_enable: false,
            fragment_setting: null,
            random_user_agent: false,
            noise_setting: null,
            use_sni_as_host: false,
        },
    });

    const handleDelete = (id: number) => {
        setHosts(hosts?.filter((host) => host.id !== id));
    };

    const handleDuplicate = (host: HostResponse) => {
        const newHost = { ...host, id: Math.max(...(hosts?.map((h) => h.id) ?? [0]), 0) + 1 };
        setHosts([...hosts ?? [], newHost]);
    };


    const toggleHostStatus = (id: number) => {
        setHosts(
            hosts?.map((host) =>
                host.id === id ? { ...host, is_disabled: !host.is_disabled } : host
            )
        );
    };

    const onSubmit = (data: HostFormValues) => {
        const newHost: HostResponse = {
            ...data,
            id: Math.max(...(hosts?.map((h) => h.id) ?? [0]), 0) + 1,
            fingerprint: data.fingerprint as ProxyHostFingerprint,
            fragment_setting: typeof data.fragment_setting === "string" ? data.fragment_setting : undefined,
            noise_setting: typeof data.noise_setting === "string" ? data.noise_setting : undefined,
            port: data.port ?? undefined,
        };

        if (editingHost) {
            const updatedHosts = hosts?.map((host) =>
                host.id === editingHost.id ? { ...host, ...newHost } : host
            );
            setHosts(updatedHosts);
        } else {
            setHosts([...hosts ?? [], newHost]);
        }

        setEditingHost(null);
        form.reset();
    };

    const sensors = useSensors(
        useSensor(PointerSensor),
        useSensor(KeyboardSensor, {
            coordinateGetter: sortableKeyboardCoordinates,
        }),
    )

    function handleDragEnd(event: DragEndEvent) {
        const { active, over } = event

        if (over && active.id !== over.id) {
            setHosts((hosts) => {
                if (!hosts) return [];
                const oldIndex = hosts?.findIndex((item) => item.id === active.id)
                const newIndex = hosts?.findIndex((item) => item.id === over.id)
                return arrayMove(hosts, oldIndex, newIndex)
            })
        }
    }

    useEffect(() => {
        const handler = setTimeout(() => {
            setDebouncedHosts(hosts);
        }, 1500);


        return () => {
            clearTimeout(handler);
        };
    }, [hosts]);

    useEffect(() => {
        if (debouncedHosts) {            
            modifyHosts(debouncedHosts)
        }
    }, [debouncedHosts]);


    return (
        <div className="p-4">
            <div>
                <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
                    <SortableContext items={hosts ?? []} strategy={rectSortingStrategy}>
                        <div className="max-w-screen-[2000px] min-h-screen overflow-hidden">
                            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                                {hosts?.map((host) => (
                                    <SortableHost key={host.id} host={host} />
                                ))}
                            </div>
                        </div>
                    </SortableContext>
                </DndContext>
            </div>

            {/* add props */}
            <AddHostModal
                form={form}
                isDialogOpen={isDialogOpen}
                onSubmit={(data) => onSubmit(data)}
                onOpenChange={(open) => onAddHost(open)}
            />
        </div>
    )
}

