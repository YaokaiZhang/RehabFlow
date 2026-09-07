#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

check_workspaces() {
  node -e '
const fs = require("fs");
const [file, ...requiredWorkspaces] = process.argv.slice(1);
const pkg = JSON.parse(fs.readFileSync(file, "utf8"));

if (!Array.isArray(pkg.workspaces)) {
  process.exit(1);
}

for (const workspace of requiredWorkspaces) {
  if (!pkg.workspaces.includes(workspace)) {
    process.exit(1);
  }
}
' "$@"
}

check_name() {
  node -e '
const fs = require("fs");
const [file, expectedName] = process.argv.slice(1);
const pkg = JSON.parse(fs.readFileSync(file, "utf8"));

process.exit(pkg.name === expectedName ? 0 : 1);
' "$@"
}

check_text_present() {
  node -e '
const fs = require("fs");
const [file, expectedText] = process.argv.slice(1);
const text = fs.readFileSync(file, "utf8");
process.exit(text.includes(expectedText) ? 0 : 1);
' "$@"
}

check_no_source_matches() {
  local pattern=$1
  local message=$2
  shift 2

  if grep -RInE \
    --include='*.js' \
    --include='*.jsx' \
    --include='*.ts' \
    --include='*.tsx' \
    --include='*.md' \
    --exclude='*.tsbuildinfo' \
    --exclude-dir='.next' \
    --exclude-dir='node_modules' \
    "$pattern" "$@"; then
    fail "$message"
  fi
}

[ -f package.json ] || fail "root package.json is missing"
[ -f apps/product/package.json ] || fail "apps/product/package.json is missing"
[ -f apps/console/package.json ] || fail "apps/console/package.json is missing"
[ -f packages/shared/package.json ] || fail "packages/shared/package.json is missing"
[ -f packages/shared/src/api.ts ] || fail "packages/shared/src/api.ts is missing"
[ -f packages/shared/src/auth.ts ] || fail "packages/shared/src/auth.ts is missing"
[ -f packages/shared/src/runtime.ts ] || fail "packages/shared/src/runtime.ts is missing"
[ -f packages/shared/src/ws.ts ] || fail "packages/shared/src/ws.ts is missing"
[ -f packages/shared/src/index.ts ] || fail "packages/shared/src/index.ts is missing"
[ -f apps/product/app/page.tsx ] || fail "product home page is missing"
[ -f apps/product/app/rehab/session/page.tsx ] || fail "product rehab session page is missing"
[ -f apps/product/app/episodes/[episode_id]/professional-care/live-monitor/page.tsx ] || fail "product episode-scoped professional care live monitor page is missing"
[ ! -e apps/product/app/dashboard/monitor/[patient_id]/page.tsx ] || fail "product must not contain patient-global doctor monitor route"
[ -f apps/console/app/page.tsx ] || fail "console home page is missing"
[ ! -e apps/product/app/test-console ] || fail "product app must not contain test-console route"

check_workspaces package.json apps/product apps/console packages/shared || fail "root package.json must declare required npm workspaces"
check_name apps/product/package.json @rehab/product || fail "product package name is incorrect"
check_name apps/console/package.json @rehab/console || fail "console package name is incorrect"
check_name packages/shared/package.json @rehab/shared || fail "shared package name is incorrect"

check_no_source_matches '(^|[^[:alnum:]_-])@/lib([^[:alnum:]_-]|$)' "frontend workspaces must import shared code through @rehab/shared, not @/lib" apps/product apps/console packages/shared
check_no_source_matches '/test-console' "product app must not link to /test-console" apps/product/app apps/product/components
check_no_source_matches 'Test Console|Internal QA|synthetic stream|diagnostics' "product app must not expose console/test wording" apps/product/app apps/product/components
check_text_present apps/console/app/layout.tsx 'Internal QA' || fail "console layout must identify itself as Internal QA"

printf 'Frontend workspace separation OK\n'
