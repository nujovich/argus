# Argus marketing / screencast site

Standalone Astro + GSAP landing page for the Argus hackathon submission.
**Not** part of the Hermes plugin — separate build, separate deploy target.

## Run

```bash
cd site
npm install
npm run dev      # http://localhost:4321
npm run build    # static output in site/dist/
```

## Layout

```
site/
├── astro.config.mjs
├── package.json
├── src/
│   ├── components/   # Hero, Problem, HowItWorks, DemoBeats, Sponsors, CTA
│   ├── layouts/Base.astro
│   ├── pages/index.astro
│   └── styles/global.css
└── README.md
```

Scroll animations use GSAP ScrollTrigger. Elements with class `reveal` fade
and translate in once they enter the viewport.

## Constraints

- The site has **no** dependency on the Hermes plugin SDK and ignores the
  dashboard's "no Next.js, no bundled React, theme vars only" rules — those
  apply only to `dashboard/`.
- Static output. Deploy via any static host (GitHub Pages, Vercel, Netlify,
  Cloudflare Pages).
