import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { VitePWA } from 'vite-plugin-pwa';

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: 'prompt',
      injectRegister: null,
      manifest: {
        name: 'OpenCalc Studio',
        short_name: 'OpenCalc',
        description:
          'A focused, accessible standard and scientific calculator.',
        start_url: '.',
        scope: '.',
        display: 'standalone',
        theme_color: '#111512',
        background_color: '#111512',
        icons: [
          {
            src: 'icons/opencalc-192.png',
            sizes: '192x192',
            type: 'image/png',
          },
          {
            src: 'icons/opencalc-512.png',
            sizes: '512x512',
            type: 'image/png',
          },
          {
            src: 'icons/opencalc-maskable-512.png',
            sizes: '512x512',
            type: 'image/png',
            purpose: 'maskable',
          },
        ],
      },
      workbox: {
        cacheId: 'opencalc-studio-v1',
        cleanupOutdatedCaches: true,
        globPatterns: ['**/*.{html,js,css,png,svg,webmanifest}'],
        navigateFallback: 'index.html',
      },
    }),
  ],
});
