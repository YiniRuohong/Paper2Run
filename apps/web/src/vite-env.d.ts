/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_DATA_MODE?: 'mock' | 'live' | 'replay';
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
