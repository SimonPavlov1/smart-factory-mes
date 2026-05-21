import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { setupWorker } from 'msw/browser';
import './index.css';
import { handlers } from './mocks/index.ts';
import App from './App.tsx';

const worker = setupWorker(...handlers);

const prepareMsw = async () => {
    if (import.meta.env.DEV) {
        await worker.start();
    }
}

prepareMsw().then(() => {
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  )
})
