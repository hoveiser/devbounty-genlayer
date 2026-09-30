// Build script: bundles the app and bakes the live studionet contract address
// into the output via an esbuild --define, then stages the static site that
// GitHub Pages serves (index.html + style.css + dist/app.js + icon assets).
//
//   CONTRACT_ADDRESS=0x... node build.mjs
//
// Falls back to the current live deployment when unset (local dev).
import { build } from 'esbuild';
import { cpSync, mkdirSync, rmSync } from 'node:fs';

const CONTRACT_ADDRESS =
  process.env.CONTRACT_ADDRESS ?? '0x0C1A6EB4288440d356a03393FE6b5F999bF075f9';
if (!/^0x[0-9a-fA-F]{40}$/.test(CONTRACT_ADDRESS)) {
  console.error(`CONTRACT_ADDRESS is not an EVM address: ${CONTRACT_ADDRESS}`);
  process.exit(2);
}

await build({
  entryPoints: ['src/main.js'],
  bundle: true,
  format: 'esm',
  target: 'es2022',
  outfile: 'dist/app.js',
  define: { __CONTRACT_ADDRESS__: JSON.stringify(CONTRACT_ADDRESS) },
});

// stage the Pages artifact: keep index.html's ./style.css and ./dist/app.js paths intact
rmSync('site', { recursive: true, force: true });
mkdirSync('site/dist', { recursive: true });
cpSync('index.html', 'site/index.html');
cpSync('style.css', 'site/style.css');
cpSync('dist/app.js', 'site/dist/app.js');
// icon assets: ./assets/* (apple-touch-icon, png sizes) + favicon at root so
// the browser's automatic /devbounty-genlayer/favicon.ico request also hits
// the new mark instead of GitHub's default star
mkdirSync('site/assets', { recursive: true });
cpSync('assets', 'site/assets', { recursive: true });
cpSync('assets/favicon.ico', 'site/favicon.ico');
cpSync('assets/favicon.svg', 'site/favicon.svg');

console.log(`built dist/app.js + site/ with CONTRACT_ADDRESS = ${CONTRACT_ADDRESS}`);
