import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import { announcePwaUpdate } from './pwa';
import './ui/styles.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);

if (import.meta.env.PROD && 'serviceWorker' in navigator) {
  void import('virtual:pwa-register').then(({ registerSW }) => {
    const updateServiceWorker = registerSW({
      immediate: true,
      onNeedRefresh() {
        announcePwaUpdate(() => updateServiceWorker(true));
      },
      onRegisterError(error) {
        console.error('OpenCalc service worker registration failed:', error);
      },
    });
  });
}
