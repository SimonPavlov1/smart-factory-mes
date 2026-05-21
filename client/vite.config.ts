import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import svgr from "vite-plugin-svgr";
import path from 'path';

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), svgr()],
  server: {
    port: 5175,
    strictPort: true,
    /*proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        secure: false
      }
    }*/
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, './src'),
      "@icons": path.resolve(__dirname, "./src/assets/icons"),
      "@ui": path.resolve(__dirname, './src/ui'),
      "@components": path.resolve(__dirname, './src/components'),
      "@utils": path.resolve(__dirname, './src/utils'),
      "@controllers": path.resolve(__dirname, './src/controllers'),
      "@models": path.resolve(__dirname, './src/models'),
      "@apis": path.resolve(__dirname, './src/apis'),
    }
  }
})
