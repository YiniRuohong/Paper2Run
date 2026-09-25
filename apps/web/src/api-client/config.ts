import type { DataMode } from './types';

// Four product views are fixture-only until their backend is implemented.
// main.tsx handles VITE_DATA_MODE and /lab before this module is loaded.
export const dataMode: DataMode = 'mock';
