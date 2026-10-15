import fs from "node:fs";
import path from "node:path";

const publicRoot = path.resolve(process.argv[2] ?? path.resolve(import.meta.dirname, "../dist/public"));
const indexPath = path.join(publicRoot, "index.html");
const assetsPath = path.join(publicRoot, "assets");
const MAX_INDEX_BYTES = 32 * 1024;
const MAX_JAVASCRIPT_BYTES = 512 * 1024;

function fail(message) {
  process.stderr.write(`FAB production build verification failed: ${message}\n`);
  process.exitCode = 1;
}

if (!fs.existsSync(indexPath)) {
  fail(`missing ${indexPath}`);
} else {
  const index = fs.readFileSync(indexPath, "utf8");
  const indexBytes = Buffer.byteLength(index, "utf8");
  if (indexBytes > MAX_INDEX_BYTES) {
    fail(`index.html is ${indexBytes} bytes; budget is ${MAX_INDEX_BYTES}`);
  }
  for (const marker of ["id=\"manus-runtime\"", "__MANUS_HOST_DEV__", "/__manus__/logs"]) {
    if (index.includes(marker)) fail(`development marker ${marker} is present in index.html`);
  }

  const javascriptAssets = fs.existsSync(assetsPath)
    ? fs.readdirSync(assetsPath)
      .filter((name) => name.endsWith(".js"))
      .map((name) => ({ name, bytes: fs.statSync(path.join(assetsPath, name)).size }))
    : [];
  const oversized = javascriptAssets.filter(({ bytes }) => bytes > MAX_JAVASCRIPT_BYTES);
  for (const asset of oversized) {
    fail(`${asset.name} is ${asset.bytes} bytes; budget is ${MAX_JAVASCRIPT_BYTES}`);
  }

  const largest = javascriptAssets.sort((left, right) => right.bytes - left.bytes)[0];
  if (!process.exitCode) {
    process.stdout.write(
      `FAB production budgets passed: index ${indexBytes} bytes, largest JavaScript ${largest?.bytes ?? 0} bytes.\n`,
    );
  }
}
