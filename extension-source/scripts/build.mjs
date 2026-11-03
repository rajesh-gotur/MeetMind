// Builds the unpacked extension into dist/: compiles TypeScript, then copies static files.
import { execFileSync } from "node:child_process";
import { cpSync, existsSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const dist = join(root, "dist");

rmSync(dist, { recursive: true, force: true });
execFileSync(process.execPath, [join(root, "node_modules", "typescript", "bin", "tsc"), "-p", root], { stdio: "inherit" });
cpSync(join(root, "static"), dist, { recursive: true });

for (const required of ["manifest.json", "background/service-worker.js", "content/meet-scraper.js", "offscreen/recorder.js"]) {
  if (!existsSync(join(dist, required))) throw new Error(`Build is missing ${required}`);
}
console.log("MeetMind built to dist/");
