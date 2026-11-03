// Entry point: language, routing between the five views, global paste and drop.

import { applyI18n, lang, onLangChange, setLang, t } from "./i18n.js";
import { $, $$, h, icon, imageFromTransfer, serverInfo } from "./core.js";
import * as home from "./home.js";
import * as cutout from "./cutout.js";
import * as erase from "./erase.js";
import * as heatmap from "./heatmap.js";
import * as focus from "./focus.js";

const views = { home, cutout, erase, heatmap, focus };
const routes = { "/": "home", "/cutout": "cutout", "/erase": "erase", "/heatmap": "heatmap", "/focus-group": "focus" };
const paths = Object.fromEntries(Object.entries(routes).map(([path, name]) => [name, path]));
const started = new Set();
let current = null;

function routeOf(pathname) {
  return routes[pathname.replace(/\/+$/, "") || "/"] ?? "home";
}

function pathOf(name) {
  return paths[name] ?? "/";
}

/**
 * Show a view. `file` hands an image to a tool, `options` hands over more (the focus
 * group takes {mode, files, regions}); `sample` asks the view to load its sample.
 */
export function navigate(name, { file = null, options = null, sample = false, replace = false } = {}) {
  if (location.pathname !== pathOf(name)) {
    history[replace ? "replaceState" : "pushState"]({}, "", pathOf(name));
  }
  show(name, { focus: true });
  if (options) views[name].receive(file, options);
  else if (file) views[name].receive(file);
  else if (sample) views[name].sample?.();
}

function show(name, { focus = false } = {}) {
  const section = $(`[data-view="${name}"]`);
  if (!started.has(name)) {
    started.add(name);
    views[name].init(section, { navigate });
  }
  for (const view of $$("[data-view]")) view.hidden = view !== section;
  for (const link of $$(".site-nav a")) {
    if (link.dataset.route === name) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  revealNavLink(name);
  current = name;
  document.title = t(`meta.title.${name}`);
  if (focus) {
    window.scrollTo(0, 0);
    $("h1", section)?.focus({ preventScroll: true });
  }
}

/**
 * On phones the nav scrolls sideways. Keep the current tool in sight, scrolled to a
 * link boundary so the first visible label is never cut in half.
 */
function revealNavLink(name) {
  const nav = $(".site-nav");
  const links = $$(".site-nav a");
  const current = links.find((link) => link.dataset.route === name);
  if (!current || nav.scrollWidth <= nav.clientWidth) {
    syncNavFade();
    return;
  }
  const inset = parseFloat(getComputedStyle(nav).paddingLeft) || 0;
  const start = (link) => link.offsetLeft - nav.offsetLeft - nav.clientLeft;
  const end = start(current) + current.offsetWidth;
  if (start(current) >= nav.scrollLeft && end <= nav.scrollLeft + nav.clientWidth) {
    syncNavFade();
    return;
  }
  const first = links.find((link) => end - start(link) + inset <= nav.clientWidth) ?? current;
  nav.scrollLeft = Math.max(0, start(first) - inset);
  syncNavFade();
}

/** Fade whichever edge of the scrolling nav has more tools beyond it. */
function syncNavFade() {
  const nav = $(".site-nav");
  const max = nav.scrollWidth - nav.clientWidth;
  const sides = [];
  if (max > 1 && nav.scrollLeft > 1) sides.push("left");
  if (max > 1 && nav.scrollLeft < max - 1) sides.push("right");
  nav.dataset.fade = sides.join(" ");
}

function syncLanguageToggle() {
  const toggle = $("#lang-toggle");
  toggle.lang = lang() === "zh" ? "en" : "zh-CN";
}

function showToolNotices(info) {
  if (info.tools.cutout && info.tools.erase) return;
  for (const name of ["cutout", "erase"]) {
    const slot = $(`[data-view="${name}"] .notice-slot`);
    slot.replaceChildren(
      h(
        "div",
        { class: "alert alert-warning", role: "status" },
        icon("alert"),
        h("div", { class: "alert-body" }, h("p", { "data-i18n": "tool.unavailable", text: t("tool.unavailable") })),
      ),
    );
  }
}

// ── Wiring ────────────────────────────────────────────────────────────────────

document.addEventListener("click", (event) => {
  const link = event.target.closest("a[data-link]");
  if (!link || event.defaultPrevented || event.button !== 0) return;
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  navigate(routeOf(new URL(link.href).pathname));
});

window.addEventListener("popstate", () => show(routeOf(location.pathname), { focus: true }));

// Paste an image anywhere: the home page offers the tools, a tool page takes it directly.
document.addEventListener("paste", (event) => {
  if (event.target.closest?.("textarea, input:not([type=file]):not([type=range]):not([type=checkbox])")) return;
  const file = imageFromTransfer(event.clipboardData);
  if (!file || !current) return;
  event.preventDefault();
  views[current].receive(file);
});

// A file dropped outside a dropzone would make the browser navigate away to it.
window.addEventListener("dragover", (event) => event.preventDefault());
window.addEventListener("drop", (event) => {
  if (event.defaultPrevented) return;
  event.preventDefault();
  const file = imageFromTransfer(event.dataTransfer);
  if (file && current) views[current].receive(file);
});

$("#lang-toggle").addEventListener("click", () => setLang(lang() === "zh" ? "en" : "zh"));

$(".site-nav").addEventListener("scroll", syncNavFade, { passive: true });
window.addEventListener("resize", () => current && revealNavLink(current));

onLangChange(() => {
  syncLanguageToggle();
  if (current) {
    document.title = t(`meta.title.${current}`);
    revealNavLink(current);
  }
  syncNavFade();
});

applyI18n();
syncLanguageToggle();
show(routeOf(location.pathname));
serverInfo().then(showToolNotices);
