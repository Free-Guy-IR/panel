/**
 * Formats a byte-per-second rate the way the statistics cards show it: the
 * headline figure in MB/s and the same rate in Mb/s beside it.
 *
 * Shared so the per-node live view and the single-server section cannot drift
 * apart - they sit next to each other on the page, and two slightly different
 * roundings of the same number would read as a bug.
 */
export function formatMbpsPair(bytesPerSecond: number, decimals = 1) {
  const mbps = (bytesPerSecond * 8) / (1024 * 1024)
  const mbpsText = mbps.toFixed(decimals).replace(/\.0$/, '')
  const mbPerSec = bytesPerSecond / (1024 * 1024)
  const mbPerSecText = mbPerSec.toFixed(decimals).replace(/\.0$/, '')

  return { mbpsText, mbPerSecText }
}
