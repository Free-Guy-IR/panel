import { L2TPCoreEditor } from '@/features/core-editor/components/l2tp/l2tp-core-editor'
import { OpenVPNCoreEditor } from '@/features/core-editor/components/openvpn/openvpn-core-editor'
import { SingBoxCoreEditor } from '@/fork/singbox'
import { registerCoreEditor } from './registry'
import './pages/routes'
import './pages/tabs'

registerCoreEditor('singbox', SingBoxCoreEditor)
registerCoreEditor('openvpn', OpenVPNCoreEditor)
registerCoreEditor('l2tp', L2TPCoreEditor)
