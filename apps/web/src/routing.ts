export type Surface = { kind: 'lab' | 'mock' | 'unavailable'; basename: '/' | '/mock' };

// Exact lab boundary: never mount product authentication or fixture providers here.
export function resolveSurface(pathname: string, mode: string | undefined): Surface {
  if (pathname === '/lab' || pathname === '/lab/') return { kind: 'lab', basename: '/' };
  if (pathname === '/mock' || pathname.startsWith('/mock/')) return { kind: 'mock', basename: '/mock' };
  if (mode === 'live' || mode === 'replay') return { kind: 'unavailable', basename: '/' };
  return { kind: 'mock', basename: '/' };
}
