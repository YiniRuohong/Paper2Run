import { QueryClient, QueryCache, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';
import { App } from './app';
import { ApiClientError } from './api-client';
import './styles/global.css';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: false, refetchOnWindowFocus: false },
  },
  queryCache: new QueryCache({
    onError: (error) => {
      if (error instanceof ApiClientError && error.status === 401) {
        queryClient.clear();
        window.dispatchEvent(new CustomEvent('p2r:session-expired'));
      }
    },
  }),
});

export default function MockWorkspace({ basename }: { basename: '/' | '/mock' }) {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter basename={basename}>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  );
}
