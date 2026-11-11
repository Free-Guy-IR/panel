export function routerPathname(path: string): string {
  let decoded = path
  try {
    decoded = path
      .split('/')
      .map(segment => decodeURIComponent(segment).replace(/\//g, '%2F'))
      .join('/')
  } catch {
    decoded = path
  }
  return decoded.toLowerCase()
}
