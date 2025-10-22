const MLDSA65_SEED_LENGTH = 32
const BASE64_CHUNK_SIZE = 0x8000

type MlDsaImplementation = {
  keygen?: (seed?: Uint8Array) => any
  keyGen?: (seed?: Uint8Array) => any
  keypair?: (seed?: Uint8Array) => any
  keyPair?: (seed?: Uint8Array) => any
  generateKeypair?: (seed?: Uint8Array) => any
  generateKeyPair?: (seed?: Uint8Array) => any
  keypairFromSeed?: (seed: Uint8Array) => any
  keyPairFromSeed?: (seed: Uint8Array) => any
  fromSeed?: (seed: Uint8Array) => any
  seedKeypair?: (seed: Uint8Array) => any
  seedKeyPair?: (seed: Uint8Array) => any
  [key: string]: any
}

let mlDsa65Promise: Promise<MlDsaImplementation> | null = null

const base64UrlEncode = (bytes: Uint8Array) => {
  if (typeof window === 'undefined') {
    throw new Error('ML-DSA-65 generation is only supported in the browser runtime')
  }
  let binary = ''
  const length = bytes.length
  for (let i = 0; i < length; i += BASE64_CHUNK_SIZE) {
    const chunk = bytes.subarray(i, i + BASE64_CHUNK_SIZE)
    binary += String.fromCharCode(...chunk)
  }
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '')
}

const base64UrlDecode = (value: string): Uint8Array => {
  if (typeof window === 'undefined') {
    throw new Error('ML-DSA-65 generation is only supported in the browser runtime')
  }
  const normalized = value.replace(/-/g, '+').replace(/_/g, '/')
  const padLength = normalized.length % 4
  const padded = padLength === 0 ? normalized : normalized + '='.repeat(4 - padLength)
  const binaryString = atob(padded)
  const bytes = new Uint8Array(binaryString.length)
  for (let i = 0; i < binaryString.length; i += 1) {
    bytes[i] = binaryString.charCodeAt(i)
  }
  return bytes
}

const loadMlDsa65 = async (): Promise<MlDsaImplementation> => {
  if (!mlDsa65Promise) {
    mlDsa65Promise = (async () => {
      const attemptLoaders: Array<() => Promise<any>> = [
        async () => (await import('@noble/post-quantum/ml-dsa.js')).ml_dsa65,
        async () => (await import('@noble/post-quantum/ml-dsa.js')).default,
        async () => (await import('@noble/post-quantum/ml-dsa.js')),
        async () => (await import('@noble/post-quantum')).ml_dsa65,
        async () => (await import('@noble/post-quantum')).ml_dsa?.ml_dsa65,
        async () => (await import('@noble/post-quantum')).ml_dsa,
      ]

      for (const loader of attemptLoaders) {
        try {
          const mod = await loader()
          if (!mod) continue
          if (mod.ml_dsa65) return mod.ml_dsa65 as MlDsaImplementation
          return mod as MlDsaImplementation
        } catch {
          // Continue to the next loader
        }
      }

      throw new Error('Unable to load @noble/post-quantum ML-DSA-65 implementation')
    })()
  }

  return mlDsa65Promise
}

const executeWithSeed = async (impl: MlDsaImplementation, seed: Uint8Array) => {
  const candidates: Array<(seed?: Uint8Array) => any> = [
    impl.keypairFromSeed,
    impl.keyPairFromSeed,
    impl.seedKeypair,
    impl.seedKeyPair,
    impl.fromSeed,
    impl.keypair,
    impl.keyPair,
    impl.keygen,
    impl.keyGen,
    impl.generateKeypair,
    impl.generateKeyPair,
  ].filter(Boolean) as Array<(seed?: Uint8Array) => any>

  for (const fn of candidates) {
    try {
      const result = fn.length > 0 ? fn(seed) : fn()
      if (result) {
        return await Promise.resolve(result)
      }
    } catch (error) {
      console.warn('[ML-DSA-65] Key generation attempt failed, trying fallback', error)
    }
  }

  throw new Error('ML-DSA-65 key generation failed: compatible function not found')
}

const unwrapPublicKey = (keypair: any): Uint8Array => {
  if (keypair instanceof Uint8Array) {
    return keypair
  }

  const candidateKeys = ['publicKey', 'public', 'verify', 'verificationKey', 'pk']
  if (keypair && typeof keypair === 'object') {
    for (const property of candidateKeys) {
      const value = keypair[property]
      if (value instanceof Uint8Array) {
        return value
      }
    }
    if (Array.isArray(keypair)) {
      const arrayCandidate = keypair.find((item: any) => item instanceof Uint8Array)
      if (arrayCandidate instanceof Uint8Array) {
        return arrayCandidate
      }
    }
    if (keypair.keypair) {
      return unwrapPublicKey(keypair.keypair)
    }
  }

  throw new Error('Unable to extract ML-DSA-65 public key from generated keypair')
}

const ensureSeed = (seed?: string): { bytes: Uint8Array; encoded: string } => {
  if (seed) {
    const decoded = base64UrlDecode(seed)
    if (decoded.length !== MLDSA65_SEED_LENGTH) {
      throw new Error(`Seed must be ${MLDSA65_SEED_LENGTH} bytes`)
    }
    return { bytes: decoded, encoded: seed }
  }

  const generated = new Uint8Array(MLDSA65_SEED_LENGTH)
  crypto.getRandomValues(generated)
  return { bytes: generated, encoded: base64UrlEncode(generated) }
}

export const generateMldsa65 = async (seed?: string): Promise<{ seed: string; verify: string }> => {
  if (typeof window === 'undefined') {
    throw new Error('ML-DSA-65 generation requires a browser environment')
  }

  const implementation = await loadMlDsa65()
  const { bytes: seedBytes, encoded } = ensureSeed(seed)

  const keypair = await executeWithSeed(implementation, seedBytes)
  const publicKey = unwrapPublicKey(keypair)

  return {
    seed: encoded,
    verify: base64UrlEncode(publicKey),
  }
}
