import { useEffect, useState } from "react"
import { useForm } from "react-hook-form"
import { zodResolver } from "@hookform/resolvers/zod"
import * as z from "zod"
import { HostResponse, modifyHosts, CreateHost, addHost, modifyHost, XHttpModes, GRPCSettings, KCPSettings, TcpSettings, WebSocketSettings, XHttpSettingsOutput } from "@/service/api"
import { closestCenter, DndContext, DragEndEvent, KeyboardSensor, PointerSensor, useSensor, useSensors } from "@dnd-kit/core"
import { arrayMove, rectSortingStrategy, SortableContext, sortableKeyboardCoordinates } from "@dnd-kit/sortable"
import SortableHost from "./SortableHost"
import { UniqueIdentifier } from "@dnd-kit/core"
import HostModal from "../dialogs/HostModal"
import { queryClient } from "@/utils/query-client"
import { toast } from "@/hooks/use-toast"
import useDirDetection from "@/hooks/use-dir-detection"
import { useTranslation } from "react-i18next"

const MultiplexProtocol = {
    smux: 'smux',
    yamux: 'yamux',
    h2mux: 'h2mux',
} as const;
interface Brutal {
    up_mbps: number;
    down_mbps: number;
}

interface XrayMuxSettings {
    concurrency: number | null;
    xudp_concurrency: number | null;
    xudp_proxy_443: string;
}

interface SingBoxMuxSettings {
    protocol: string | null;
    max_connections: number | null;
    max_streams: number | null;
    min_streams: number | null;
    padding: boolean | null;
    brutal: Brutal | null;
}

interface ClashMuxSettings {
    protocol: string | null;
    max_connections: number | null;
    max_streams: number | null;
    min_streams: number | null;
    padding: boolean | null;
    brutal: Brutal | null;
    statistic: boolean | null;
    only_tcp: boolean | null;
}

interface MuxSettings {
    xray?: XrayMuxSettings;
    sing_box?: SingBoxMuxSettings;
    clash?: ClashMuxSettings;
}

export interface HostFormValues {
    remark: string;
    address: string;
    port: number;
    inbound_tag: string;
    status: ('active' | 'disabled' | 'limited' | 'expired' | 'on_hold')[];
    host?: string;
    sni?: string;
    path?: string;
    http_headers?: Record<string, string>;
    security: 'none' | 'tls' | 'inbound_default';
    alpn?: string;
    fingerprint?: string;
    allowinsecure: boolean;
    is_disabled: boolean;
    random_user_agent: boolean;
    use_sni_as_host: boolean;
    priority: number;
    fragment_settings?: {
        xray?: {
            packets?: string;
            length?: string;
            interval?: string;
        };
    };
    noise_settings?: {
        xray?: {
            type: string;
            packet: string;
            delay: string;
        }[];
    };
    mux_settings?: MuxSettings;
    transport_settings?: {
        xhttp_settings?: {
            mode?: 'auto' | 'packet-up' | 'stream-up' | 'stream-one';
            no_grpc_header?: boolean;
            x_padding_bytes?: string;
            sc_max_each_post_bytes?: string;
            sc_min_posts_interval_ms?: string;
            sc_max_buffered_posts?: string;
            sc_stream_up_server_secs?: string;
            xmux?: {
                max_concurrency?: string;
                max_connections?: string;
                c_max_reuse_times?: string;
                c_max_lifetime?: string;
                h_max_request_times?: string;
                h_keep_alive_period?: string;
            };
        };
        grpc_settings?: {
            multi_mode?: boolean;
            idle_timeout?: number;
            health_check_timeout?: number;
            permit_without_stream?: number;
            initial_windows_size?: number;
        };
        kcp_settings?: {
            header?: string;
            mtu?: number;
            tti?: number;
            uplink_capacity?: number;
            downlink_capacity?: number;
            congestion?: number;
            read_buffer_size?: number;
            write_buffer_size?: number;
        };
        tcp_settings?: {
            header?: string;
            request?: {
                version?: string;
                headers?: Record<string, string[]>;
                method?: string;
            };
            response?: {
                version?: string;
                headers?: Record<string, string[]>;
                status?: string;
                reason?: string;
            };
        };
        websocket_settings?: {
            heartbeatPeriod?: number;
        };
    };
}

// Update the transport settings schema
const transportSettingsSchema = z.object({
    xhttp_settings: z.object({
        mode: z.enum(["auto", "packet-up", "stream-up", "stream-one"]).nullish(),
        no_grpc_header: z.boolean().nullish(),
        x_padding_bytes: z.string().nullish(),
        sc_max_each_post_bytes: z.string().nullish(),
        sc_min_posts_interval_ms: z.string().nullish(),
        sc_max_buffered_posts: z.string().nullish(),
        sc_stream_up_server_secs: z.string().nullish()
    }).nullish(),
    grpc_settings: z.object({
        multi_mode: z.boolean().nullish(),
        idle_timeout: z.number().nullish(),
        health_check_timeout: z.number().nullish(),
        permit_without_stream: z.number().nullish(),
        initial_windows_size: z.number().nullish()
    }).nullish(),
    kcp_settings: z.object({
        header: z.string().nullish(),
        mtu: z.number().nullish(),
        tti: z.number().nullish(),
        uplink_capacity: z.number().nullish(),
        downlink_capacity: z.number().nullish(),
        congestion: z.number().nullish(),
        read_buffer_size: z.number().nullish(),
        write_buffer_size: z.number().nullish()
    }).nullish(),
    tcp_settings: z.object({
        header: z.string().nullish(),
        request: z.object({
            version: z.string().nullish(),
            method: z.string().nullish(),
            headers: z.record(z.array(z.string())).nullish()
        }).nullish(),
        response: z.object({
            version: z.string().nullish(),
            status: z.string().nullish(),
            reason: z.string().nullish(),
            headers: z.record(z.array(z.string())).nullish()
        }).nullish()
    }).nullish(),
    websocket_settings: z.object({
        heartbeatPeriod: z.number().nullish()
    }).nullish()
}).nullish();

export const HostFormSchema = z.object({
    remark: z.string().min(1, "Remark is required"),
    address: z.string().min(1, "Address is required"),
    port: z.number().min(1, "Port must be at least 1").max(65535, "Port must be at most 65535"),
    inbound_tag: z.string().min(1, "Inbound tag is required"),
    status: z.array(z.string()).default([]),
    host: z.string().default(""),
    sni: z.string().default(""),
    path: z.string().default(""),
    http_headers: z.record(z.string()).default({}),
    security: z.enum(["inbound_default", "tls", "reality"]).default("inbound_default"),
    alpn: z.string().default("default"),
    fingerprint: z.string().default("default"),
    allowinsecure: z.boolean().default(false),
    random_user_agent: z.boolean().default(false),
    use_sni_as_host: z.boolean().default(false),
    priority: z.number().default(0),
    is_disabled: z.boolean().default(false),
    fragment_settings: z.object({
        xray: z.object({
            fragments: z.array(z.object({
                interval: z.string(),
                length: z.string(),
                times: z.string()
            }))
        }).optional()
    }).optional(),
    noise_settings: z.object({
        xray: z.object({
            type: z.string()
        }).optional()
    }).optional(),
    mux_settings: z.object({
        xray: z.object({
            concurrency: z.number().nullable(),
            xudp_concurrency: z.number().nullable(),
            xudp_proxy_443: z.string()
        }).optional(),
        sing_box: z.object({
            protocol: z.string().nullable(),
            max_connections: z.number().nullable(),
            max_streams: z.number().nullable(),
            min_streams: z.number().nullable(),
            padding: z.boolean().nullable(),
            brutal: z.object({
                up_mbps: z.number(),
                down_mbps: z.number()
            }).nullable()
        }).optional(),
        clash: z.object({
            protocol: z.string().nullable(),
            max_connections: z.number().nullable(),
            max_streams: z.number().nullable(),
            min_streams: z.number().nullable(),
            padding: z.boolean().nullable(),
            brutal: z.object({
                up_mbps: z.number(),
                down_mbps: z.number()
            }).nullable(),
            statistic: z.boolean().nullable(),
            only_tcp: z.boolean().nullable()
        }).optional()
    }).optional(),
    transport_settings: transportSettingsSchema
});

// Define initial default values separately
const initialDefaultValues: HostFormValues = {
    remark: "",
    address: "",
    port: 443,
    inbound_tag: "",
    status: [],
    host: "",
    sni: "",
    path: "",
    http_headers: {},
    security: "inbound_default",
    alpn: "default",
    fingerprint: "default",
    allowinsecure: false,
    is_disabled: false,
    random_user_agent: false,
    use_sni_as_host: false,
    priority: 0,
    fragment_settings: {
        xray: {
            packets: "",
            length: "",
            interval: ""
        }
    },
    noise_settings: {
        xray: [{
            type: "",
            packet: "",
            delay: ""
        }]
    },
    mux_settings: {
        xray: {
            concurrency: null,
            xudp_concurrency: null,
            xudp_proxy_443: "reject"
        },
        sing_box: {
            protocol: null,
            max_connections: 0,
            max_streams: 0,
            min_streams: 0,
            padding: false,
            brutal: null
        },
        clash: {
            protocol: null,
            max_connections: 0,
            max_streams: 0,
            min_streams: 0,
            padding: false,
            brutal: null,
            statistic: false,
            only_tcp: false
        }
    },
    transport_settings: {
        xhttp_settings: {
            mode: "auto",
            no_grpc_header: false,
            x_padding_bytes: "",
            sc_max_each_post_bytes: "",
            sc_min_posts_interval_ms: "",
            sc_max_buffered_posts: "",
            sc_stream_up_server_secs: "",
            xmux: {
                max_concurrency: "",
                max_connections: "",
                c_max_reuse_times: "",
                c_max_lifetime: "",
                h_max_request_times: "",
                h_keep_alive_period: ""
            }
        },
        grpc_settings: {
            multi_mode: false,
            idle_timeout: 0,
            health_check_timeout: 0,
            permit_without_stream: 0,
            initial_windows_size: 0
        },
        kcp_settings: {
            header: "none",
            mtu: 0,
            tti: 0,
            uplink_capacity: 0,
            downlink_capacity: 0,
            congestion: 0,
            read_buffer_size: 0,
            write_buffer_size: 0
        },
        tcp_settings: {
            header: "",
            request: {
                version: "",
                headers: {},
                method: ""
            },
            response: {
                version: "",
                headers: {},
                status: "",
                reason: ""
            }
        },
        websocket_settings: {
            heartbeatPeriod: 0
        }
    }
};

export interface HostsProps {
    data: HostResponse[];
    isDialogOpen: boolean;
    onDialogOpenChange: (open: boolean) => void;
    onAddHost: (open: boolean) => void;
}

export default function Hosts({ data, onAddHost, isDialogOpen }: HostsProps) {
    const [hosts, setHosts] = useState<HostResponse[] | undefined>();
    const [editingHost, setEditingHost] = useState<HostResponse | null>(null);
    const [debouncedHosts, setDebouncedHosts] = useState<HostResponse[] | undefined>([]);
    const dir = useDirDetection();
    const { t } = useTranslation();

    // useQuery({
    //     queryKey: ["modifyHosts", debouncedHosts],
    //     queryFn: () => modifyHosts(debouncedHosts ?? []),
    //     enabled: !!debouncedHosts,
    // });    

    useEffect(() => {
        setHosts(data ?? [])
    }, [data])    

    const form = useForm<HostFormValues>({
        resolver: zodResolver(HostFormSchema),
        defaultValues: initialDefaultValues,
    });

    const handleEdit = (host: HostResponse) => {
        const formData: HostFormValues = {
            remark: host.remark || "",
            address: host.address || "",
            port: host.port ? Number(host.port) : 443,
            inbound_tag: host.inbound_tag || "",
            status: host.status || [],
            host: host.host || "",
            sni: host.sni || "",
            path: host.path || "",
            http_headers: host.http_headers || {},
            security: host.security || "inbound_default",
            alpn: host.alpn || "default",
            fingerprint: host.fingerprint || "default",
            allowinsecure: host.allowinsecure || false,
            random_user_agent: host.random_user_agent || false,
            use_sni_as_host: host.use_sni_as_host || false,
            priority: host.priority ? Number(host.priority) : (hosts?.length ?? 0),
            is_disabled: host.is_disabled || false,
            fragment_settings: host.fragment_settings ? {
                xray: host.fragment_settings.xray ?? undefined
            } : undefined,
            noise_settings: host.noise_settings ? {
                xray: host.noise_settings.xray ?? undefined
            } : undefined,
            mux_settings: host.mux_settings ? {
                xray: host.mux_settings.xray ? {
                    concurrency: host.mux_settings.xray.concurrency ?? null,
                    xudp_concurrency: host.mux_settings.xray.xudp_concurrency ?? null,
                    xudp_proxy_443: host.mux_settings.xray.xudp_proxy_443 ?? "reject"
                } : undefined,
                sing_box: host.mux_settings.sing_box ? {
                    protocol: host.mux_settings.sing_box.protocol ?? null,
                    max_connections: host.mux_settings.sing_box.max_connections ?? null,
                    max_streams: host.mux_settings.sing_box.max_streams ?? null,
                    min_streams: host.mux_settings.sing_box.min_streams ?? null,
                    padding: host.mux_settings.sing_box.padding ?? null,
                    brutal: host.mux_settings.sing_box.brutal ?? null
                } : undefined,
                clash: host.mux_settings.clash ? {
                    protocol: host.mux_settings.clash.protocol ?? null,
                    max_connections: host.mux_settings.clash.max_connections ?? null,
                    max_streams: host.mux_settings.clash.max_streams ?? null,
                    min_streams: host.mux_settings.clash.min_streams ?? null,
                    padding: host.mux_settings.clash.padding ?? null,
                    brutal: host.mux_settings.clash.brutal ?? null,
                    statistic: host.mux_settings.clash.statistic ?? null,
                    only_tcp: host.mux_settings.clash.only_tcp ?? null
                } : undefined
            } : undefined,
            transport_settings: host.transport_settings ? {
                xhttp_settings: host.transport_settings.xhttp_settings ? {
                    mode: host.transport_settings.xhttp_settings.mode ?? undefined,
                    no_grpc_header: host.transport_settings.xhttp_settings.no_grpc_header === null ? undefined : !!host.transport_settings.xhttp_settings.no_grpc_header,
                    x_padding_bytes: host.transport_settings.xhttp_settings.x_padding_bytes ?? undefined,
                    sc_max_each_post_bytes: host.transport_settings.xhttp_settings.sc_max_each_post_bytes ?? undefined,
                    sc_min_posts_interval_ms: host.transport_settings.xhttp_settings.sc_min_posts_interval_ms ?? undefined,
                    sc_max_buffered_posts: host.transport_settings.xhttp_settings.sc_max_buffered_posts ?? undefined,
                    sc_stream_up_server_secs: host.transport_settings.xhttp_settings.sc_stream_up_server_secs ?? undefined
                } : undefined,
                grpc_settings: host.transport_settings.grpc_settings ? {
                    multi_mode: host.transport_settings.grpc_settings.multi_mode === null ? undefined : !!host.transport_settings.grpc_settings.multi_mode,
                    idle_timeout: host.transport_settings.grpc_settings.idle_timeout ?? undefined,
                    health_check_timeout: host.transport_settings.grpc_settings.health_check_timeout ?? undefined,
                    permit_without_stream: host.transport_settings.grpc_settings.permit_without_stream ?? undefined,
                    initial_windows_size: host.transport_settings.grpc_settings.initial_windows_size ?? undefined
                } : undefined,
                kcp_settings: host.transport_settings.kcp_settings ? {
                    header: host.transport_settings.kcp_settings.header ?? undefined,
                    mtu: host.transport_settings.kcp_settings.mtu ?? undefined,
                    tti: host.transport_settings.kcp_settings.tti ?? undefined,
                    uplink_capacity: host.transport_settings.kcp_settings.uplink_capacity ?? undefined,
                    downlink_capacity: host.transport_settings.kcp_settings.downlink_capacity ?? undefined,
                    congestion: host.transport_settings.kcp_settings.congestion ?? undefined,
                    read_buffer_size: host.transport_settings.kcp_settings.read_buffer_size ?? undefined,
                    write_buffer_size: host.transport_settings.kcp_settings.write_buffer_size ?? undefined
                } : undefined,
                tcp_settings: host.transport_settings.tcp_settings ? {
                    header: host.transport_settings.tcp_settings.header ?? undefined,
                    request: host.transport_settings.tcp_settings.request ? {
                        version: host.transport_settings.tcp_settings.request.version ?? undefined,
                        method: host.transport_settings.tcp_settings.request.method ?? undefined,
                        headers: host.transport_settings.tcp_settings.request.headers ?? undefined
                    } : undefined,
                    response: host.transport_settings.tcp_settings.response ? {
                        version: host.transport_settings.tcp_settings.response.version ?? undefined,
                        status: host.transport_settings.tcp_settings.response.status ?? undefined,
                        reason: host.transport_settings.tcp_settings.response.reason ?? undefined,
                        headers: host.transport_settings.tcp_settings.response.headers ?? undefined
                    } : undefined
                } : undefined,
                websocket_settings: host.transport_settings.websocket_settings ? {
                    heartbeatPeriod: host.transport_settings.websocket_settings.heartbeatPeriod ?? undefined
                } : undefined
            } : undefined,
        };
        form.reset(formData);
        setEditingHost(host);
        onAddHost(true);
    };

    const handleDuplicate = async (host: HostResponse) => {
        if (!host) return;
        
        try {
            // Find all hosts with priorities equal to or less than the host being duplicated
            // Get the host's index in the sorted array
            const sortedHosts = [...(hosts ?? [])].sort((a, b) => (a.priority ?? 0) - (b.priority ?? 0));
            const hostIndex = sortedHosts.findIndex(h => h.id === host.id);
            
            if (hostIndex === -1) return;
            
            // Find the next host's priority
            let newPriority: number;
            if (hostIndex === sortedHosts.length - 1) {
                // If it's the last host, just add 1 to its priority
                newPriority = (host.priority ?? 0) + 1;
            } else {
                // Set priority after the current host but before the next host
                // Always use an integer value
                const nextHostPriority = sortedHosts[hostIndex + 1].priority ?? hostIndex + 1;
                const currentPriority = host.priority ?? 0;
                
                if (nextHostPriority > currentPriority + 1) {
                    // If there's space between priorities, simply add 1
                    newPriority = currentPriority + 1;
                } else {
                    // Otherwise increment all priorities after this host
                    newPriority = currentPriority + 1;
                    
                    // Update all hosts after this one to have higher priorities
                    const hostsToUpdate = sortedHosts.slice(hostIndex + 1).map(h => ({
                        ...h, 
                        priority: (h.priority ?? 0) + 1
                    }));
                    
                    if (hostsToUpdate.length > 0) {
                        // Update priorities in batch
                        modifyHosts(hostsToUpdate);
                    }
                }
            }
            
            // Create duplicate with new priority and slightly modified name
            const newHost: CreateHost = {
            ...host, 
                id: undefined, // Remove ID so a new one is generated
                remark: `${host.remark || ""} (copy)`,
                priority: newPriority,
                // Special handling for enum values
                alpn: host.alpn === "" ? undefined : host.alpn,
                fingerprint: host.fingerprint === "" ? undefined : host.fingerprint,
            };
            
            await addHost(newHost);
            
            // Show success toast
            toast({
                dir,
                description: t("host.duplicateSuccess", { name: host.remark || "" }),
                defaultValue: `Host "${host.remark || ""}" duplicated successfully`
            });
            
            // Refresh the hosts data
            queryClient.invalidateQueries({
                queryKey: ["getGetHostsQueryKey"],
            });
            
        } catch (error) {
            console.error('Error duplicating host:', error);
            
            // Show error toast
            toast({
                dir,
                variant: "destructive",
                description: t("host.duplicateFailed", { name: host.remark || "" }),
                defaultValue: `Failed to duplicate host "${host.remark || ""}"`
            });
        }
    };

    const onSubmit = async (data: HostFormValues) => {
        try {
            // Helper function to recursively remove empty objects, arrays, or default values
            const cleanEmptyValues = (obj: any, preserveZeroValues: boolean = false): any => {
                if (obj === null || obj === undefined) return undefined;
                
                // If it's an array, filter out empty items
                if (Array.isArray(obj)) {
                    const filteredArray = obj.map(item => cleanEmptyValues(item, preserveZeroValues)).filter(item => item !== undefined);
                    return filteredArray.length > 0 ? filteredArray : undefined;
                }
                
                // If it's an object, recursively clean it
                if (typeof obj === 'object') {
                    const cleaned: Record<string, any> = {};
                    let hasValues = false;
                    
                    for (const key in obj) {
                        const value = cleanEmptyValues(obj[key], preserveZeroValues);
                        if (value !== undefined) {
                            cleaned[key] = value;
                            hasValues = true;
                        }
                    }
                    
                    return hasValues ? cleaned : undefined;
                }
                
                // For strings, return undefined if empty or just whitespace
                if (typeof obj === 'string' && obj.trim() === '') return undefined;
                
                // For numbers, return undefined if 0 (default), unless preserveZeroValues is true
                if (typeof obj === 'number' && obj === 0 && !preserveZeroValues) return undefined;
                
                // For booleans, return undefined if false (default)
                if (typeof obj === 'boolean' && obj === false) return undefined;
                
                // Otherwise return the value
                return obj;
            };
            
            // Special cleaner for transport settings to only include sections that have values
            const cleanTransportSettings = (settings: any) => {
                if (!settings) return undefined;
                
                const cleaned: Record<string, any> = {};
                let hasTransportSettings = false;
                
                // Clean xhttp_settings
                if (settings.xhttp_settings) {
                    const cleanedXhttp = cleanEmptyValues(settings.xhttp_settings);
                    if (cleanedXhttp) {
                        cleaned.xhttp_settings = cleanedXhttp;
                        hasTransportSettings = true;
                    }
                }
                
                // Clean grpc_settings
                if (settings.grpc_settings) {
                    const cleanedGrpc = cleanEmptyValues(settings.grpc_settings);
                    if (cleanedGrpc) {
                        cleaned.grpc_settings = cleanedGrpc;
                        hasTransportSettings = true;
                    }
                }
                
                // Clean kcp_settings
                if (settings.kcp_settings) {
                    const cleanedKcp = cleanEmptyValues(settings.kcp_settings);
                    if (cleanedKcp) {
                        cleaned.kcp_settings = cleanedKcp;
                        hasTransportSettings = true;
                    }
                }
                
                // Clean tcp_settings
                if (settings.tcp_settings) {
                    const cleanedTcp = cleanEmptyValues(settings.tcp_settings);
                    if (cleanedTcp) {
                        cleaned.tcp_settings = cleanedTcp;
                        hasTransportSettings = true;
                    }
                }
                
                // Clean websocket_settings
                if (settings.websocket_settings) {
                    const cleanedWebsocket = cleanEmptyValues(settings.websocket_settings);
                    if (cleanedWebsocket) {
                        cleaned.websocket_settings = cleanedWebsocket;
                        hasTransportSettings = true;
                    }
                }
                
                return hasTransportSettings ? cleaned : undefined;
            };
            
            // Clean the entire form data
            const cleanedData = { 
                ...data,
                // Always include required fields even if they would be cleaned
                remark: data.remark,
                address: data.address,
                port: data.port, // Always include port
                inbound_tag: data.inbound_tag,
                priority: data.priority, // Always include priority
                
                // Clean nested objects
                http_headers: Object.keys(data.http_headers || {}).length > 0 ? data.http_headers : undefined,
                fragment_settings: cleanEmptyValues(data.fragment_settings),
                noise_settings: cleanEmptyValues(data.noise_settings),
                mux_settings: cleanEmptyValues(data.mux_settings),
                
                // Apply special cleaning to transport settings
                transport_settings: cleanTransportSettings(data.transport_settings),
            };
            
            if (editingHost?.id) {
                // Edit existing host
                await modifyHost(editingHost.id, {
                    ...cleanedData,
                    alpn: cleanedData.alpn === 'default' ? undefined : cleanedData.alpn,
                    fingerprint: cleanedData.fingerprint === 'default' ? undefined : cleanedData.fingerprint,
                } as CreateHost);
            } else {
                // Add new host
                const maxPriority = Math.min(...(hosts?.map((h) => h.priority ?? 0) ?? [0]), 0);
                const newHost = {
                    ...cleanedData,
                    priority: maxPriority - 1,
                    alpn: cleanedData.alpn === 'default' ? undefined : cleanedData.alpn,
                    fingerprint: cleanedData.fingerprint === 'default' ? undefined : cleanedData.fingerprint,
                };
            await addHost(newHost as CreateHost);
            }
            setEditingHost(null);
            form.reset(initialDefaultValues); // Reset to initial values after submit
            return { status: 200 };
        } catch (error) {
            console.error('Error managing host:', error);
            return { status: 500 };
        }
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
                                    <SortableHost 
                                        key={host.id ?? 'new'} 
                                        host={host}
                                        onEdit={handleEdit}
                                        onDuplicate={handleDuplicate}
                                    />
                                ))}
                            </div>
                        </div>
                    </SortableContext>
                </DndContext>
            </div>

            <HostModal
                isDialogOpen={isDialogOpen}
                onSubmit={onSubmit}
                onOpenChange={(open) => {
                    if (!open) {
                        setEditingHost(null);
                        form.reset(initialDefaultValues); // Reset to initial values when closing
                    } else if (!editingHost) {
                        // When opening for a new host, ensure form is reset to initial values
                        form.reset(initialDefaultValues);
                    }
                    onAddHost(open);
                }}
                form={form}
                editingHost={!!editingHost}
            />
        </div>
    )
}

