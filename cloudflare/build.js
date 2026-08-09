import { copyFileSync, mkdirSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(fileURLToPath(import.meta.url));
const source = join(root, "..", "src", "job_monitor", "web_static");
const output = join(root, "public");

rmSync(output, { force: true, recursive: true });
mkdirSync(join(output, "assets"), { recursive: true });

copyFileSync(join(source, "index.html"), join(output, "index.html"));
copyFileSync(join(source, "styles.css"), join(output, "assets", "styles.css"));
copyFileSync(join(source, "app.js"), join(output, "assets", "app.js"));
copyFileSync(join(root, "_headers"), join(output, "_headers"));

console.log("Built Cloudflare Pages assets.");
