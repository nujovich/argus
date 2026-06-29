import { defineConfig } from 'astro/config';

export default defineConfig({
  site: 'https://argus.example.com',
  output: 'static',
  build: {
    inlineStylesheets: 'auto',
  },
});
