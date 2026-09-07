import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

const scriptDir = dirname(fileURLToPath(import.meta.url));
const productRoot = resolve(scriptDir, "..");
const loginPage = readFileSync(resolve(productRoot, "app/login/page.tsx"), "utf8");
const doctorPage = readFileSync(resolve(productRoot, "app/doctor/page.tsx"), "utf8");

assert(loginPage.includes('const LOGIN_ROLE_KEY = "rehab_login_role"'), "login role should be persisted across remounts");
assert(loginPage.includes('safeInitialRole(params.get("role"))'), "login should accept an explicit role query parameter");
assert(loginPage.includes('window.sessionStorage.setItem(LOGIN_ROLE_KEY, requestedRole)'), "role query should update sticky role state");
assert(loginPage.includes('const updateRole = (nextRole: LoginRole)'), "role select changes should use a dedicated updater");
assert(loginPage.includes('window.sessionStorage.setItem(LOGIN_ROLE_KEY, nextRole)'), "manual role changes should persist while typing");
assert(!loginPage.includes('const [name, setName] = useState("")'), "login name should not be controlled by empty React state that can erase browser autofill");
assert(!loginPage.includes('const [password, setPassword] = useState("")'), "login password should not be controlled by empty React state that can erase browser autofill");
assert(!loginPage.includes('value={name}'), "login name field should leave browser-filled values in the DOM while typing");
assert(!loginPage.includes('value={password}'), "login password field should leave browser-filled values in the DOM while typing");
assert(loginPage.includes("new FormData(e.currentTarget)"), "login submit should read current DOM field values, including browser autofill");
assert(loginPage.includes('name="role"'), "login role select should participate in form submission");
assert(loginPage.includes('const submittedRole = safeInitialRole(String(formData.get("role") ?? "")) || role;'), "login submit should read the selected role from the form");
assert(loginPage.includes('login({ role: submittedRole, username, password: userPassword })'), "login should send the role selected at submit time");
assert(loginPage.includes('String(formData.get("name") ?? "")'), "login submit should read the current name field from FormData");
assert(loginPage.includes('String(formData.get("password") ?? "")'), "login submit should read the current password field from FormData");
assert(!loginPage.includes('onChange={(e) => setRole(e.target.value as "patient" | "doctor")}'), "role select should not bypass sticky role updater");
assert(loginPage.includes('doctorRedirectPath(nextPath)'), "doctor login should use the Care Worklist redirect helper");
assert(loginPage.includes('return nextPath.startsWith("/doctor") ? nextPath : "/doctor"'), "doctor redirect should default to the Care Worklist route");
assert(doctorPage.includes('href="/login?role=doctor&next=/doctor"'), "doctor sign-in link should open login with doctor role and Care Worklist return path");

console.log("login role contract ok");
