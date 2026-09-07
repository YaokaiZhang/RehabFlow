import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const root = process.cwd();
const read = (path) => readFileSync(join(root, path), "utf8");
const failures = [];

function expect(condition, message) {
  if (!condition) failures.push(message);
}

function walk(dir, acc = []) {
  for (const entry of readdirSync(join(root, dir))) {
    if (entry === ".next" || entry === "node_modules") continue;
    const rel = join(dir, entry);
    const full = join(root, rel);
    const stat = statSync(full);
    if (stat.isDirectory()) walk(rel, acc);
    if (stat.isFile() && /\.(tsx|ts)$/.test(entry)) acc.push(rel);
  }
  return acc;
}

const navbar = read("apps/product/components/Navbar.tsx");
expect(navbar.includes("Episodes"), "Navbar should expose Episodes for patients.");
expect(navbar.includes("Sign out"), "Navbar should use Sign out as the signed-in auth action.");
expect(!navbar.includes("Care Paths"), "Navbar should not expose the old Care Paths product surface.");
expect(!navbar.includes("href=\"/menu\""), "Navbar should not link to /menu.");
expect(!navbar.includes("Logout"), "Navbar should not use old Logout label.");

const episodesPage = read("apps/product/app/episodes/page.tsx");
expect(episodesPage.includes("Create Episode With AI Triage"), "Episodes page should clearly offer AI-triage episode creation.");
expect(episodesPage.includes("Manual Episode"), "Episodes page should keep manual episode creation as the secondary path.");
expect(episodesPage.includes('href="/"'), "Episodes page should link AI-triage episode creation to the AI Triage Intake entry.");
expect(!episodesPage.includes("save a summary in a later slice"), "Episodes page should not describe AI-triage episode creation as a later slice.");

const register = read("apps/product/app/register/page.tsx");
expect(!register.includes("router.push(\"/menu\")"), "Register should not send new users to /menu.");
expect(register.includes("auth.role === \"doctor\" ? \"/doctor\" : \"/episodes\""), "Register should route by role after saving auth.");

expect(!existsSync(join(root, "apps/product/app/menu/page.tsx")), "Old /menu route should be deleted.");

const productFiles = walk("apps/product");
for (const file of productFiles) {
  const source = read(file);
  expect(!source.includes("href=\"/menu\""), file + " should not link to /menu.");
  expect(!source.includes("Professional Monitoring"), file + " should not use old Professional Monitoring language.");
  expect(!source.includes("Care Paths"), file + " should not use old Care Paths language.");
  expect(!source.includes("Bind Patient"), file + " should not use old manual binding language.");
}

if (failures.length) {
  console.error(failures.map((failure) => "- " + failure).join("\n"));
  process.exit(1);
}
console.log("product shell contract ok");
