import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const page = readFileSync(resolve("apps/product/app/episodes/[episode_id]/rehab/page.tsx"), "utf8");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

assert(page.includes("Recommended from AI Triage"), "AI rehab page should keep the recommendation section");
assert(!page.includes("Clinician review is recommended"), "AI rehab page should not show the clinician review warning");
assert(!page.includes("Keep today&apos;s list conservative"), "AI rehab page should not show the conservative-list warning");

console.log("AI daily rehab page contract ok");
