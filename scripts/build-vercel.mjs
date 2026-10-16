import { mkdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';

// Flask owns templates, static assets, sessions and all API routes. Keep every
// request on the same public origin while its state stays on persistent storage.
export function buildConfig(value) {
  if (!value) throw new Error('Set MD57_BACKEND_ORIGIN to your persistent Flask server HTTPS origin.');
  const origin = new URL(value);
  if (origin.protocol !== 'https:' || origin.username || origin.password ||
      origin.pathname !== '/' || origin.search || origin.hash ||
      ['localhost', '127.0.0.1', '[::1]'].includes(origin.hostname)) {
    throw new Error('MD57_BACKEND_ORIGIN must be a public HTTPS origin without credentials, path, query or fragment.');
  }
  const deploymentHosts = [process.env.VERCEL_URL, process.env.VERCEL_PROJECT_PRODUCTION_URL];
  if (deploymentHosts.includes(origin.host)) {
    throw new Error('MD57_BACKEND_ORIGIN must point to Flask, not this Vercel deployment.');
  }
  return {
    version: 3,
    routes: [{ src: '/(.*)', dest: `${origin.origin}/$1`, headers: {
      'Cache-Control': 'private, no-store, max-age=0',
      'Vercel-CDN-Cache-Control': 'no-store',
      'CDN-Cache-Control': 'no-store',
    } }],
  };
}

if (process.argv[1] && resolve(process.argv[1]) === resolve(import.meta.filename)) {
  const config = buildConfig(process.env.MD57_BACKEND_ORIGIN);
  mkdirSync('.vercel/output', { recursive: true });
  writeFileSync('.vercel/output/config.json', `${JSON.stringify(config, null, 2)}\n`);
  console.log('Vercel proxy configuration built. Flask runs on the persistent backend.');
}
