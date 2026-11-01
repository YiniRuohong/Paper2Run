import { useSyncExternalStore } from 'react';
import { getThemeState, setThemePref, subscribeTheme, type ThemeState } from '../theme';

// React view of the theme store in src/theme.ts.
export function useTheme(): ThemeState & { setPref: typeof setThemePref } {
  const state = useSyncExternalStore(subscribeTheme, getThemeState, getThemeState);
  return { ...state, setPref: setThemePref };
}
