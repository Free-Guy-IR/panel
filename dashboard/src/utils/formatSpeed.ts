export function formatMbpsPair(bytesPerSecond: number, decimals = 1) {
  const mbps = (bytesPerSecond * 8) / (1024 * 1024)
  const mbpsText = mbps.toFixed(decimals).replace(/\.0$/, '')
  const mbPerSec = bytesPerSecond / (1024 * 1024)
  const mbPerSecText = mbPerSec.toFixed(decimals).replace(/\.0$/, '')

  return { mbpsText, mbPerSecText }
}
