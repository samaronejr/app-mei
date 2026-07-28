// Copy the pinned browser builds of HTMX and Alpine into static/js/.
//
// Vendored rather than loaded from a CDN for two reasons. A fiscal product must not
// have its interface disappear because a third party had an outage on the day DAS is
// due; and every external script origin is an origin the Content-Security-Policy has
// to allow, which is exactly the allowance that makes a CSP stop being a control.
//
// The Alpine build is `@alpinejs/csp`, NOT `alpinejs`. The default distribution
// compiles `x-*` expressions with `new Function`, so shipping it would force
// `script-src 'unsafe-eval'` and hand any injected attribute a JavaScript evaluator.
//
// The result is committed to the repository, so building the image and running CI
// need no Node toolchain at all. `npm run build` regenerates it; check-vendored-assets
// (a pytest case) fails if a committed file drifts from this manifest.

import { createHash } from "node:crypto";
import { copyFileSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const outputDirectory = join(root, "static", "js");

const VENDORED = [
  {
    package: "htmx.org",
    // The classic (non-module) build: it assigns window.htmx, which is what a
    // `defer` script tag in a server-rendered page expects.
    source: "dist/htmx.min.js",
    target: "htmx.min.js",
  },
  {
    package: "@alpinejs/csp",
    // "cdn" names the build shape (self-starting browser global), not the delivery
    // channel. It is served from this origin.
    source: "dist/cdn.min.js",
    target: "alpine-csp.min.js",
  },
];

mkdirSync(outputDirectory, { recursive: true });

const manifest = VENDORED.map(({ package: name, source, target }) => {
  const packageJson = JSON.parse(
    readFileSync(join(root, "node_modules", name, "package.json"), "utf8"),
  );
  const from = join(root, "node_modules", name, source);
  const to = join(outputDirectory, target);
  copyFileSync(from, to);
  const sha256 = createHash("sha256").update(readFileSync(to)).digest("hex");
  console.log(`vendored ${name}@${packageJson.version} -> static/js/${target}`);
  return { file: target, package: name, version: packageJson.version, source, sha256 };
});

writeFileSync(
  join(outputDirectory, "VENDOR.json"),
  `${JSON.stringify({ vendored: manifest }, null, 2)}\n`,
);
