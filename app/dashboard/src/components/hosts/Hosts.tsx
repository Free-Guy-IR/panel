"use client"

import { useEffect, useState } from "react"
import { useForm } from "react-hook-form"
import { zodResolver } from "@hookform/resolvers/zod"
import { z } from "zod"
import { HostResponse, modifyHosts, ProxyHostFingerprint, FragmentSettings, NoiseSettings } from "@/service/api"
import AddHostModal from "../dialogs/AddHostModal"
import { closestCenter, DndContext, DragEndEvent, KeyboardSensor, PointerSensor, useSensor, useSensors } from "@dnd-kit/core"
import { arrayMove, rectSortingStrategy, SortableContext, sortableKeyboardCoordinates } from "@dnd-kit/sortable"
import SortableHost from "./SortableHost"
import { useQuery } from "@tanstack/react-query"
import { UniqueIdentifier } from "@dnd-kit/core"

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

    const handleDelete = (id: number | null) => {
        if (!id) return;
        setHosts(hosts?.filter((host) => host.id !== id));
    };

    const handleDuplicate = (host: HostResponse) => {
        if (!host.id) return;
        const maxId = Math.max(...(hosts?.map((h) => h.id ?? 0) ?? [0]), 0);
        const maxPriority = Math.min(...(hosts?.map((h) => h.priority ?? 0) ?? [0]), 0);
        const newHost = { 
            ...host, 
            id: maxId + 1,
            priority: maxPriority - 1 // Lower number = higher priority
        };
        setHosts([...hosts ?? [], newHost]);
    };

    const toggleHostStatus = (id: number | null) => {
        if (!id) return;
        setHosts(
            hosts?.map((host) =>
                host.id === id ? { ...host, is_disabled: !host.is_disabled } : host
            )
        );
    };

    const onSubmit = (data: HostFormValues) => {
        const maxId = Math.max(...(hosts?.map((h) => h.id ?? 0) ?? [0]), 0);
        const maxPriority = Math.min(...(hosts?.map((h) => h.priority ?? 0) ?? [0]), 0);
        const newHost: HostResponse = {
            ...data,
            id: maxId + 1,
            priority: maxPriority - 1, // Lower number = higher priority
            fingerprint: data.fingerprint as ProxyHostFingerprint,
            fragment_settings: data.fragment_setting ? {
                xray: data.fragment_setting
            } : null,
            noise_settings: data.noise_setting ? {
                xray: [{
                    type: "rand",
                    packet: data.noise_setting.noise_payload,
                    delay: data.noise_setting.noise_pattern
                }]
            } : null,
            port: data.port ?? null,
        };

        if (editingHost?.id) {
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
                const oldIndex = hosts.findIndex((item) => item.id === active.id)
                const newIndex = hosts.findIndex((item) => item.id === over.id)
                const reorderedHosts = arrayMove(hosts, oldIndex, newIndex)
                
                // Update priorities based on new order (lower number = higher priority)
                return reorderedHosts.map((host, index) => ({
                    ...host,
                    priority: index
                }))
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

    // Filter out hosts without IDs for the sortable context
    const sortableHosts = hosts?.filter(host => host.id !== null).map(host => ({
        id: host.id as UniqueIdentifier
    })) ?? [];

    // Sort hosts by priority (lower number = higher priority)
    const sortedHosts = [...(hosts ?? [])].sort((a, b) => (a.priority ?? 0) - (b.priority ?? 0));

    return (
        <div className="p-4">
            <div>
                <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
                    <SortableContext items={sortableHosts} strategy={rectSortingStrategy}>
                        <div className="max-w-screen-[2000px] min-h-screen overflow-hidden">
                            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                                {sortedHosts.map((host) => (
                                    <SortableHost key={host.id ?? 'new'} host={host} />
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

