export type Surface = { kind: 'guided' | 'lab' | 'mock' | 'unavailable'; basename: '/' | '/mock' };

export function resolveSurface(_pathname: string, _mode: string | undefined): Surface {
  return { kind: 'guided', basename: '/' };
}
