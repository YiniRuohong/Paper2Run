import { createLiveClient, type ApiClient } from './client';
import { createMockClient } from './mock/adapter';
import { dataMode } from './config';

export { ApiClientError } from './client';
export type { ApiClient } from './client';
export { dataMode } from './config';
export * from './types';

let cached: ApiClient | null = null;

export function getClient(): ApiClient {
  if (!cached) {
    cached = dataMode === 'mock' ? createMockClient() : createLiveClient();
  }
  return cached;
}
