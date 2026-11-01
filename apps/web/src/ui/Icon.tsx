// Minimal inline line-icon set (24-unit grid, round joins) so the UI needs no
// icon dependency. Usage: <Icon name="copy" size={14} />
const PATHS = {
  grid: 'M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z',
  list: 'M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01',
  plus: 'M12 5v14M5 12h14',
  refresh: 'M20 11.5a8 8 0 1 1-2.4-5.7M20 4.5v5h-5',
  alert: 'M12 9.5v4M12 17h.01M10.3 4.2 2.9 17.2A2 2 0 0 0 4.6 20.2h14.8a2 2 0 0 0 1.7-3L13.7 4.2a2 2 0 0 0-3.4 0z',
  bell: 'M18 9a6 6 0 1 0-12 0c0 6.5-2.5 8.5-2.5 8.5h17S18 15.5 18 9M13.7 21a2 2 0 0 1-3.4 0',
  doc: 'M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8zM14 3v5h5M9 13h6M9 17h4',
  check: 'M19.5 6.5 9 17l-4.5-4.5',
  home: 'M4 10.5 12 4l8 6.5V19a1 1 0 0 1-1 1h-4.5v-6h-5v6H5a1 1 0 0 1-1-1z',
  play: 'M7 4.5v15l12.5-7.5z',
  'chevron-right': 'M9.5 6l6 6-6 6',
  'chevron-down': 'M6 9.5l6 6 6-6',
  'chevron-left': 'M14.5 6l-6 6 6 6',
  settings: 'M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2zM15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0z',
  sidebar: 'M5 4.5h14a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-11a2 2 0 0 1 2-2zM9.5 4.5v15',
  copy: 'M10 9h9a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1h-9a1 1 0 0 1-1-1v-9a1 1 0 0 1 1-1zM15 9V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v9a1 1 0 0 0 1 1h4',
  folder: 'M3.5 7.5a2 2 0 0 1 2-2h3.6l2 2h7.4a2 2 0 0 1 2 2v7.5a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z',
  server: 'M5 3.5h14a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2zM5 13.5h14a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2zM7 7h.01M7 17h.01',
  search: 'M11 4.5a6.5 6.5 0 1 1 0 13 6.5 6.5 0 0 1 0-13zM20 20l-4.4-4.4',
  'check-circle': 'M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18zM8.3 12.3l2.6 2.6 4.8-5.3',
  x: 'M6.5 6.5l11 11M17.5 6.5l-11 11',
  info: 'M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18zM12 11v5M12 7.8h.01',
  monitor: 'M4.5 4.5h15a1.5 1.5 0 0 1 1.5 1.5v9a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 15V6a1.5 1.5 0 0 1 1.5-1.5zM8.5 20h7M12 16.5V20',
  chart: 'M5 20v-6M10 20V8M15 20v-9M20 20V5',
  shield: 'M12 3.2 19.5 6v5.6c0 4.4-3.1 8.1-7.5 9.2-4.4-1.1-7.5-4.8-7.5-9.2V6z',
  key: 'M8 15.5a3.5 3.5 0 1 1 0-7 3.5 3.5 0 0 1 0 7zM11.5 12H21M18 12v3M21 12v2.5',
  spark: 'M12 3.5l1.9 5.1 5.1 1.9-5.1 1.9-1.9 5.1-1.9-5.1-5.1-1.9 5.1-1.9z',
  flask: 'M9 3.5h6M10 3.5v5.8L5.2 17.7A2 2 0 0 0 7 20.5h10a2 2 0 0 0 1.8-2.8L14 9.3V3.5M7.5 14.5h9',
  cpu: 'M7.5 7.5h9v9h-9zM10 3.5v4M14 3.5v4M10 16.5v4M14 16.5v4M3.5 10h4M3.5 14h4M16.5 10h4M16.5 14h4',
  code: 'M8.5 8.5 5 12l3.5 3.5M15.5 8.5 19 12l-3.5 3.5M13.3 5.5l-2.6 13',
  download: 'M12 4v11M7.5 10.5 12 15l4.5-4.5M5 19.5h14',
  upload: 'M12 19V8M7.5 12.5 12 8l4.5 4.5M5 4.5h14',
  clock: 'M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18zM12 7.5V12l3 2',
  layers: 'M12 3.5 21 8l-9 4.5L3 8zM3 12.5l9 4.5 9-4.5M3 16.5 12 21l9-4.5',
} as const;

export type IconName = keyof typeof PATHS;

export function Icon({ name, size = 16, spin = false, className }: { name: IconName; size?: number; spin?: boolean; className?: string }) {
  return <svg className={`ico${spin ? ' spin' : ''}${className ? ` ${className}` : ''}`} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false"><path d={PATHS[name]} /></svg>;
}

// App mark derived from apps/desktop/resources/icon.svg: paper sheet with a
// folded corner and an indigo "run" triangle, simplified for 18-20px.
export function AppMark({ size = 18 }: { size?: number }) {
  return <svg className="app-mark" width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true" focusable="false">
    <path d="M8 2.75h7.25l4 4V19a2.25 2.25 0 0 1-2.25 2.25H8A2.25 2.25 0 0 1 5.75 19V5A2.25 2.25 0 0 1 8 2.75z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
    <path d="M15.25 2.75v2.75a1.25 1.25 0 0 0 1.25 1.25h2.75" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" strokeLinecap="round" />
    <path d="M10.4 10.6v5.8l5-2.9z" fill="var(--accent)" stroke="var(--accent)" strokeWidth="1.2" strokeLinejoin="round" />
  </svg>;
}
