export type ToastTone = 'ok' | 'bad' | 'idle';

/** Success and info toasts leave on their own; an error stays until it is closed. */
export const TOAST_MS = 6000;
export const toastLifetime = (tone: ToastTone): number | null => tone === 'bad' ? null : TOAST_MS;
