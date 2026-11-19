// Runs before the page paints so dark mode never flashes light first.
let saved = null;
try {
  saved = localStorage.getItem("theme");
} catch {
  // Storage can be blocked; fall back to the system setting.
}
const dark = saved ? saved === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
document.documentElement.dataset.theme = dark ? "dark" : "light";
