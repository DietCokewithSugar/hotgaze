// AI focus group: brief → recruit → review and edit the plan → discussion, streamed
// turn by turn → structured report. The plan, transcript and report live here in the
// browser; each step sends them back to the server, which keeps nothing.

import { lang, onLangChange, t, tFor } from "./i18n.js";
import {
  $,
  $$,
  ApiError,
  canvasBlob,
  clearNotice,
  download,
  h,
  icon,
  isImageFile,
  postEvents,
  renameExtension,
  serverInfo,
  showNotice,
  statusLine,
  toast,
} from "./core.js";

const SAMPLE_URL = (language) => `/static/samples/focus-group-wealth.${language}.json`;
const FALLBACK = { participants: [3, 6], max_images: 20, max_images_ab: 10, plan_edge: 2048, talk_edge: 1280 };
const STANCES = ["supportive", "neutral", "skeptical"];
const MAX_QUESTIONS = 6;
// A screenshot this small that is already JPEG or PNG is sent as it is.
const PASS_BYTES = 600 * 1024;

let root;
let els;
let status;
let limits = FALLBACK;
let available = true;
let nextShotId = 1;
let follow = null;
const sampleCache = new Map();

const state = {
  mode: "text",
  shots: { a: [], b: [] }, // {id, file, url, name, prepared: Map(edge -> File)}
  count: 5,
  regions: [], // from the heatmap page
  phase: "brief", // brief | recruiting | plan | discussing | stopped | reporting | done
  session: null, // the brief as recruited: {lang, topic, context, audience, mode, participants}
  planKey: "",
  plan: null,
  attention: null, // {overlay, summary}
  turns: [],
  live: null, // {turn, node, text}
  report: null,
  reportFailed: false,
  usage: emptyUsage(),
  pending: emptyUsage(), // the request in flight reports cumulative usage
  models: new Set(),
  active: 0, // seconds spent discussing and reporting
  since: 0, // performance.now() when the current stretch began
  editing: null, // {kind: "person" | "question", id}
  sample: null, // {generator, topic} while showing the sample session
  abort: null,
  error: null,
  retry: null,
};

export function init(section) {
  root = section;
  els = {
    notices: $(".notice-slot", root),
    sheet: $("[data-sheet]", root),
    brief: $("[data-brief]", root),
    topic: $("textarea[name=topic]", root),
    audience: $("input[name=audience]", root),
    context: $("textarea[name=context]", root),
    modeHelp: $("[data-mode-help]", root),
    trays: $("[data-trays]", root),
    plan: $("[data-plan]", root),
    session: $("[data-session]", root),
    roster: $("[data-roster]", root),
    transcript: $("[data-transcript]", root),
    pill: $("[data-pill]", root),
    dockStop: $("[data-dock-stop]", root),
    steps: $$("[data-step]", root),
    meta: $("[data-meta]", root),
    countSection: $("[data-count-section]", root),
    actions: $("[data-actions]", root),
    results: $("[data-results]", root),
    report: $("[data-report]", root),
    reportMeta: $("[data-report-meta]", root),
    file: $("[data-file]", root),
  };
  status = statusLine($("[data-status]", root), els.pill);

  for (const field of [els.topic, els.audience, els.context]) field.addEventListener("input", renderPanel);
  els.topic.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && ready()) recruit();
  });

  const tabs = $$("[data-mode]", root);
  for (const tab of tabs) {
    tab.addEventListener("click", () => setMode(tab.dataset.mode));
    tab.addEventListener("keydown", (event) => {
      const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
      if (!step) return;
      event.preventDefault();
      const next = tabs[(tabs.indexOf(tab) + step + tabs.length) % tabs.length];
      next.focus();
      setMode(next.dataset.mode);
    });
  }
  for (const button of $$("[data-count]", root)) {
    button.addEventListener("click", () => {
      state.count = Number(button.dataset.count);
      renderCount();
    });
  }
  for (const chip of $$("[data-sample]", root)) chip.addEventListener("click", () => sample());
  els.dockStop.addEventListener("click", stop);

  els.file.addEventListener("change", () => {
    const files = [...(els.file.files || [])];
    els.file.value = "";
    addShots(els.file.dataset.version || "a", files);
  });

  window.addEventListener("beforeunload", (event) => {
    if (["recruiting", "discussing", "reporting"].includes(state.phase)) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  serverInfo().then((info) => {
    limits = { ...FALLBACK, ...(info.focus_group || {}) };
    available = info.tools?.focus_group !== false;
    for (const [field, key] of [
      [els.topic, "topic_max"],
      [els.context, "context_max"],
      [els.audience, "audience_max"],
    ]) {
      if (limits[key]) field.maxLength = limits[key];
    }
    if (!available) {
      els.notices.prepend(
        h(
          "div",
          { class: "alert alert-warning", role: "status" },
          icon("alert"),
          h("div", { class: "alert-body" }, h("p", { "data-i18n": "focus.unavailable", text: t("focus.unavailable") })),
        ),
      );
    }
    renderPanel();
  });

  onLangChange(() => {
    renderAll();
    status.refresh();
    if (state.error) showNotice(els.notices, state.error, state.retry);
  });
  renderAll();
}

/** A pasted or dropped image, or a hand-off from another tool: {mode, files: {a, b}, regions}. */
export function receive(file, options = {}) {
  const handoff = Boolean(options.mode || options.files);
  if (handoff && (state.phase !== "brief" || state.sample)) reset();
  if (!handoff && (state.phase !== "brief" || state.sample)) return;
  if (options.mode) setMode(options.mode);
  if (options.regions) state.regions = [...options.regions];
  if (options.files) {
    addShots("a", options.files.a || []);
    addShots("b", options.files.b || []);
  }
  if (file) {
    if (state.mode === "text") setMode("flow");
    addShots(state.mode === "ab" && state.shots.a.length && !state.shots.b.length ? "b" : "a", [file]);
  }
}

/** Show the sample session: a complete run, read-only. */
export async function sample() {
  if (busy()) return;
  try {
    const data = await loadSample(lang());
    showSession(data);
  } catch (error) {
    fail(error, sample);
  }
}

/** The sample session for a language (cached). Home uses it for its excerpt. */
export function loadSample(language) {
  if (!sampleCache.has(language)) {
    sampleCache.set(
      language,
      fetch(SAMPLE_URL(language)).then((response) => {
        if (!response.ok) throw new ApiError("network", {}, response.statusText);
        return response.json();
      }),
    );
  }
  const promise = sampleCache.get(language);
  promise.catch(() => sampleCache.delete(language));
  return promise;
}

/** A transcript turn as a list item; `plan` names the speakers. */
export function turnNode(turn, plan, { attention = null, total = plan.guide.length } = {}) {
  const moderator = turn.speaker === "moderator";
  const person = plan.participants.find((item) => item.id === turn.speaker);
  const text = h("p", { class: "turn-text", text: turn.text });
  const meta = moderator
    ? [h("b", { text: t("focus.moderator") }), h("span", { text: turnLabel(turn, plan, total) })]
    : [
        h("b", { text: person?.name || turn.speaker }),
        h("span", { text: [person?.summary, turnLabel(turn, plan, total)].filter(Boolean).join(" · ") }),
      ];
  const body = h("div", { class: "turn-body" }, h("p", { class: "turn-meta" }, meta), text);
  if (turn.image === "heatmap" && attention?.overlay) {
    body.append(
      h(
        "figure",
        { class: "turn-figure" },
        h("img", { src: attention.overlay, alt: t("focus.overlay_alt"), loading: "lazy" }),
        h("figcaption", { text: t("focus.heatmap_shown") }),
      ),
    );
  }
  const node = h(
    "li",
    { class: moderator ? `turn turn-mod turn-${turn.kind}` : "turn", "data-slot": turn.slot || "" },
    moderator ? null : avatar(person?.name || "?"),
    body,
  );
  return { node, text };
}

// ── Phases ────────────────────────────────────────────────────────────────────

function busy() {
  return ["recruiting", "discussing", "reporting"].includes(state.phase);
}

function setPhase(phase) {
  state.phase = phase;
  renderAll();
}

function reset() {
  state.abort?.abort();
  Object.assign(state, {
    phase: "brief",
    session: null,
    planKey: "",
    plan: null,
    attention: null,
    turns: [],
    live: null,
    report: null,
    reportFailed: false,
    usage: emptyUsage(),
    pending: emptyUsage(),
    models: new Set(),
    active: 0,
    editing: null,
    sample: null,
    abort: null,
  });
  clearError();
  els.transcript.replaceChildren();
  renderAll();
}

function ready() {
  if (!available || busy() || !els.topic.value.trim()) return false;
  if (state.mode === "flow" || state.mode === "heatmap") return state.shots.a.length > 0;
  if (state.mode === "ab") return state.shots.a.length > 0 && state.shots.b.length > 0;
  return true;
}

function materialKey() {
  const ids = (list) => list.map((shot) => shot.id).join(",");
  const a = state.mode === "heatmap" ? state.shots.a.slice(0, 1) : state.shots.a;
  return [state.mode, state.mode === "text" ? "" : ids(a), state.mode === "ab" ? ids(state.shots.b) : ""].join("|");
}

function startClock() {
  state.since = performance.now();
}

function stopClock() {
  if (state.since) state.active += (performance.now() - state.since) / 1000;
  state.since = 0;
}

/** For status lines that keep counting across stretches of discussion and report. */
function clockOrigin() {
  return (state.since || performance.now()) - state.active * 1000;
}

// ── Step 1: brief and material ────────────────────────────────────────────────

function setMode(mode) {
  if (busy() || mode === state.mode) return;
  state.mode = mode;
  if (mode === "heatmap" && state.shots.a.length > 1) toast(t("focus.heatmap_first_only"));
  renderBrief();
  renderPanel();
}

function addShots(version, files) {
  const images = files.filter(isImageFile);
  if (files.length && !images.length) {
    toast(t("errors.local_file"));
    return;
  }
  if (!images.length || state.phase !== "brief") return;
  images.sort((x, y) => (x.name || "").localeCompare(y.name || "", undefined, { numeric: true }));
  const list = state.shots[version];
  const cap = state.mode === "heatmap" ? 1 : state.mode === "ab" ? limits.max_images_ab : limits.max_images;
  if (state.mode === "heatmap") {
    for (const shot of list.splice(0)) URL.revokeObjectURL(shot.url);
  }
  const room = Math.max(0, cap - list.length);
  if (images.length > room) toast(state.mode === "heatmap" ? t("focus.heatmap_first_only") : t("focus.too_many_shots", { max: cap }));
  for (const file of images.slice(0, room)) {
    list.push({ id: nextShotId++, file, url: URL.createObjectURL(file), name: file.name || "screenshot", prepared: new Map() });
  }
  clearError();
  renderBrief();
  renderPanel();
}

function moveShot(version, id, step) {
  const list = state.shots[version];
  const index = list.findIndex((shot) => shot.id === id);
  const target = index + step;
  if (index < 0 || target < 0 || target >= list.length) return;
  [list[index], list[target]] = [list[target], list[index]];
  renderBrief();
  $(`[data-shot="${id}"] [data-move="${step}"]`, els.trays)?.focus();
}

function removeShot(version, id) {
  const list = state.shots[version];
  const index = list.findIndex((shot) => shot.id === id);
  if (index < 0) return;
  URL.revokeObjectURL(list[index].url);
  list.splice(index, 1);
  renderBrief();
  renderPanel();
}

function browse(version) {
  els.file.dataset.version = version;
  els.file.multiple = state.mode !== "heatmap";
  els.file.click();
}

function renderBrief() {
  for (const tab of $$("[data-mode]", root)) {
    const selected = tab.dataset.mode === state.mode;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
  }
  els.modeHelp.dataset.i18n = `focus.mode_${state.mode}_help`;
  els.modeHelp.textContent = t(els.modeHelp.dataset.i18n);
  const disabled = state.phase !== "brief";
  for (const field of [els.topic, els.audience, els.context]) field.disabled = disabled;

  if (state.mode === "text") {
    els.trays.hidden = true;
    els.trays.replaceChildren();
    return;
  }
  els.trays.hidden = false;
  const versions = state.mode === "ab" ? ["a", "b"] : ["a"];
  els.trays.classList.toggle("is-pair", versions.length === 2);
  els.trays.replaceChildren(...versions.map((version) => tray(version)));
}

function tray(version) {
  const heatmap = state.mode === "heatmap";
  const shots = heatmap ? state.shots.a.slice(0, 1) : state.shots[version];
  const label = state.mode === "ab" ? t(version === "a" ? "focus.version_a" : "focus.version_b") : null;
  const classes = ["tray", shots.length ? "" : "is-empty", state.phase === "recruiting" ? "is-locked" : ""];
  const zone = h("div", { class: classes.filter(Boolean).join(" "), "data-tray": version });
  bindTrayDrop(zone, version);

  const head =
    label || shots.length
      ? h(
          "div",
          { class: "tray-head" },
          label ? h("span", { class: "tray-label", text: label }) : null,
          shots.length && !heatmap ? h("span", { class: "meta num", text: t("focus.shots_count", { n: shots.length }) }) : null,
        )
      : null;

  if (!shots.length) {
    zone.append(
      head || "",
      h(
        "div",
        { class: "drop-prompt" },
        h(
          "button",
          { type: "button", class: "btn btn-primary", onclick: () => browse(version) },
          icon("upload"),
          h("span", { text: t("focus.upload") }),
        ),
        h("p", { class: "dropzone-hint", text: t(heatmap ? "focus.upload_one_hint" : "focus.upload_hint") }),
        h("p", { class: "dropzone-formats", text: t("home.formats") }),
      ),
    );
    return zone;
  }

  const list = h("ol", { class: "tiles" });
  shots.forEach((shot, index) => {
    const ref = state.mode === "ab" ? `${version.toUpperCase()}${index + 1}` : heatmap ? "" : String(index + 1);
    const tools = heatmap
      ? [toolButton("x", t("focus.remove_shot", { name: shot.name }), () => removeShot(version, shot.id))]
      : [
          toolButton("left", t("focus.move_left"), () => moveShot(version, shot.id, -1), { disabled: index === 0, move: -1 }),
          toolButton("right", t("focus.move_right"), () => moveShot(version, shot.id, 1), {
            disabled: index === shots.length - 1,
            move: 1,
          }),
          toolButton("x", t("focus.remove_shot", { name: shot.name }), () => removeShot(version, shot.id)),
        ];
    list.append(
      h(
        "li",
        { class: heatmap ? "tile tile-wide" : "tile", "data-shot": shot.id },
        h(
          "figure",
          { class: "tile-frame" },
          h("img", { src: shot.url, alt: "", loading: "lazy" }),
          ref ? h("span", { class: "tile-ref num", text: ref }) : null,
        ),
        h("span", { class: "tile-name", title: shot.name, text: shot.name }),
        h("div", { class: "tile-tools" }, tools),
      ),
    );
  });
  const cap = state.mode === "ab" ? limits.max_images_ab : limits.max_images;
  if (!heatmap && shots.length < cap) {
    list.append(
      h(
        "li",
        { class: "tile tile-add" },
        h(
          "button",
          { type: "button", class: "tile-add-button", onclick: () => browse(version) },
          icon("plus"),
          h("span", { text: t("focus.add_more") }),
        ),
      ),
    );
  }
  zone.append(head || "", list);
  if (state.mode === "heatmap" && state.shots.a.length > 1) zone.append(h("p", { class: "help", text: t("focus.heatmap_first_only") }));
  return zone;
}

function bindTrayDrop(zone, version) {
  let depth = 0;
  zone.addEventListener("dragenter", (event) => {
    event.preventDefault();
    depth += 1;
    zone.classList.add("is-over");
  });
  zone.addEventListener("dragover", (event) => event.preventDefault());
  zone.addEventListener("dragleave", () => {
    depth = Math.max(0, depth - 1);
    if (!depth) zone.classList.remove("is-over");
  });
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    depth = 0;
    zone.classList.remove("is-over");
    addShots(version, [...(event.dataTransfer?.files || [])]);
  });
}

function toolButton(name, label, onclick, { disabled = false, move = null } = {}) {
  return h(
    "button",
    {
      type: "button",
      class: "icon-btn icon-btn-sm",
      "aria-label": label,
      title: label,
      disabled,
      "data-move": move === null ? null : String(move),
      onclick,
    },
    icon(name),
  );
}

// ── Recruit ───────────────────────────────────────────────────────────────────

async function recruit() {
  if (!ready()) {
    if (!els.topic.value.trim()) els.topic.focus();
    return;
  }
  const session = {
    lang: lang(),
    topic: els.topic.value.trim(),
    context: els.context.value.trim(),
    audience: els.audience.value.trim(),
    mode: state.mode,
    participants: state.count,
  };
  Object.assign(state, {
    session,
    planKey: materialKey(),
    plan: null,
    attention: null,
    turns: [],
    report: null,
    reportFailed: false,
    usage: emptyUsage(),
    models: new Set(),
    active: 0,
    editing: null,
  });
  els.transcript.replaceChildren();
  clearError();
  setPhase("recruiting");
  const shotsHint = session.mode === "text" ? "status.focus_recruit_hint" : "status.focus_recruit_hint_shots";
  status.working("status.focus_recruit", { hintKey: shotsHint, slowAfter: session.mode === "text" ? 90 : 150, slowKey: "status.focus_slow" });

  const controller = new AbortController();
  state.abort = controller;
  try {
    const body = briefForm(session);
    body.append("participants", String(session.participants));
    await appendShots(body, session.mode, limits.plan_edge, 0.9);
    if (session.mode === "heatmap") for (const region of state.regions) body.append("region", region);
    await request("/api/focus-group/plan", body, controller, (name, data) => {
      if (name === "status" && data.phase === "heatmap") status.working("status.focus_heatmap", { slowAfter: 60, slowKey: "heatmap.slow" });
      else if (name === "status") status.working("status.focus_recruit", { hintKey: shotsHint, slowAfter: 150, slowKey: "status.focus_slow" });
      else if (name === "attention") state.attention = data;
      else if (name === "plan") state.plan = data.plan;
    });
    if (!state.plan) throw new ApiError("focus_bad_output");
    setPhase("plan");
    status.done("focus.planned", { count: state.plan.participants.length, questions: state.plan.guide.length });
    focusSheet();
  } catch (error) {
    if (error.name === "AbortError") {
      if (state.phase === "recruiting") setPhase("brief");
      return;
    }
    setPhase("brief");
    fail(error, recruit);
  } finally {
    if (state.abort === controller) state.abort = null;
  }
}

function briefForm(session) {
  const body = new FormData();
  body.append("lang", session.lang);
  body.append("topic", session.topic);
  body.append("context", session.context);
  body.append("audience", session.audience);
  body.append("mode", session.mode);
  return body;
}

async function appendShots(body, mode, edge, quality) {
  if (mode === "text") return;
  const a = mode === "heatmap" ? state.shots.a.slice(0, 1) : state.shots.a;
  for (const shot of a) body.append("images", await prepared(shot, edge, quality));
  if (mode === "ab") for (const shot of state.shots.b) body.append("images_b", await prepared(shot, edge, quality));
}

/** The screenshot as sent: upright JPEG at most `edge` px long (small JPEG/PNG go as they are). */
async function prepared(shot, edge, quality) {
  if (!shot.prepared.has(edge)) {
    let bitmap;
    try {
      bitmap = await createImageBitmap(shot.file, { imageOrientation: "from-image" });
    } catch {
      throw new ApiError("local_file");
    }
    const scale = Math.min(1, edge / Math.max(bitmap.width, bitmap.height));
    let file = shot.file;
    if (scale < 1 || shot.file.size > PASS_BYTES || !/^image\/(png|jpeg)$/.test(shot.file.type)) {
      const width = Math.max(1, Math.round(bitmap.width * scale));
      const height = Math.max(1, Math.round(bitmap.height * scale));
      const canvas = h("canvas", { width, height });
      const context = canvas.getContext("2d");
      context.fillStyle = "#fff"; // transparent areas read as white, not black
      context.fillRect(0, 0, width, height);
      context.imageSmoothingQuality = "high";
      context.drawImage(bitmap, 0, 0, width, height);
      const blob = await canvasBlob(canvas, "image/jpeg", quality);
      file = new File([blob], renameExtension(shot.name, "jpg"), { type: "image/jpeg" });
    }
    bitmap.close?.();
    shot.prepared.set(edge, file);
  }
  return shot.prepared.get(edge);
}

/** One streamed step. Usage arrives cumulative for the request and is banked at the end. */
async function request(path, body, controller, onEvent) {
  state.pending = emptyUsage();
  try {
    await postEvents(path, body, {
      signal: controller.signal,
      onEvent: (name, data) => {
        if (name === "usage") {
          state.pending = data;
          if (data.model) state.models.add(data.model);
        } else {
          onEvent(name, data);
        }
      },
    });
  } finally {
    for (const key of Object.keys(state.usage)) state.usage[key] += Number(state.pending[key] || 0);
    state.pending = emptyUsage();
  }
}

// ── Step 2: review and edit the plan ──────────────────────────────────────────

function renderPlan() {
  const plan = state.plan;
  if (!plan) {
    els.plan.replaceChildren();
    return;
  }
  const people = plan.participants;
  const guide = plan.guide;
  const peopleList = h("ul", { class: "roster" }, people.map((person) => personRow(person)));
  const guideList = h("ol", { class: "guide" }, guide.map((item, index) => guideRow(item, index)));
  const sections = [
    h(
      "div",
      { class: "plan-head" },
      h("h2", { class: "plan-title", text: plan.title || state.session?.topic || "" }),
      plan.objective ? h("p", { class: "plan-objective", text: plan.objective }) : null,
    ),
    h(
      "section",
      { class: "plan-section" },
      h("div", { class: "section-head" }, h("h3", {}, t("focus.participants"), " ", h("span", { class: "count num", text: people.length }))),
      peopleList,
    ),
    h(
      "section",
      { class: "plan-section" },
      h(
        "div",
        { class: "section-head" },
        h("h3", {}, t("focus.guide"), " ", h("span", { class: "count num", text: guide.length })),
        h(
          "button",
          {
            type: "button",
            class: "btn btn-sm btn-quiet",
            disabled: guide.length >= MAX_QUESTIONS,
            onclick: addQuestion,
          },
          icon("plus"),
          h("span", { text: t("focus.add_question") }),
        ),
      ),
      guideList,
    ),
  ];
  const heatmap = state.session?.mode === "heatmap";
  if (plan.screens?.length && !heatmap) sections.push(screensSection(plan));
  if (heatmap && state.attention) sections.push(attentionSection(plan));
  els.plan.replaceChildren(...sections);
}

function personRow(person) {
  const editing = state.editing?.kind === "person" && state.editing.id === person.id;
  if (editing) return personEditor(person);
  return h(
    "li",
    { class: "person", "data-person": person.id },
    avatar(person.name),
    h(
      "div",
      { class: "person-body" },
      h(
        "p",
        { class: "person-head" },
        h("b", { text: person.name }),
        person.summary ? h("span", { class: "person-summary", text: person.summary }) : null,
        stanceTag(person.stance),
      ),
      person.profile ? h("p", { class: "person-profile", text: person.profile }) : null,
      person.voice ? h("p", { class: "meta", text: t("focus.voice", { voice: person.voice }) }) : null,
    ),
    h(
      "div",
      { class: "row-tools" },
      toolButton("pencil", t("focus.edit_name", { name: person.name }), () => edit("person", person.id)),
      toolButton("trash", t("focus.remove_person", { name: person.name }), () => removePerson(person.id)),
    ),
  );
}

function personEditor(person) {
  const fields = {
    name: h("input", { class: "input", type: "text", value: person.name, maxlength: "20", autocomplete: "off" }),
    summary: h("input", { class: "input", type: "text", value: person.summary || "", maxlength: "80", autocomplete: "off" }),
    stance: h(
      "select",
      {},
      STANCES.map((stance) => h("option", { value: stance, selected: stance === person.stance, text: t(`focus.stance.${stance}`) })),
    ),
    profile: h("textarea", { class: "prose", rows: "4", maxlength: "700", text: person.profile || "" }),
    voice: h("input", { class: "input", type: "text", value: person.voice || "", maxlength: "140", autocomplete: "off" }),
  };
  fields.profile.value = person.profile || "";
  const save = () => {
    const name = fields.name.value.trim();
    if (name) person.name = name;
    person.summary = fields.summary.value.trim();
    person.stance = fields.stance.value;
    person.profile = fields.profile.value.trim();
    person.voice = fields.voice.value.trim();
    state.editing = null;
    renderPlan();
    $(`[data-person="${person.id}"] .icon-btn`, els.plan)?.focus();
  };
  const row = h(
    "li",
    { class: "person is-editing", "data-person": person.id },
    avatar(person.name),
    h(
      "div",
      { class: "editor-grid" },
      labelled(t("focus.field_name"), fields.name),
      labelled(t("focus.field_stance"), fields.stance),
      labelled(t("focus.field_summary"), fields.summary, "wide"),
      labelled(t("focus.field_profile"), fields.profile, "wide"),
      labelled(t("focus.field_voice"), fields.voice, "wide"),
      h("div", { class: "editor-actions" }, h("button", { type: "button", class: "btn btn-sm", onclick: save, text: t("focus.done_edit") })),
    ),
  );
  row.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      state.editing = null;
      renderPlan();
    } else if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      save();
    }
  });
  requestAnimationFrame(() => fields.name.focus());
  return row;
}

function guideRow(item, index) {
  const editing = state.editing?.kind === "question" && state.editing.id === item.id;
  if (editing) return guideEditor(item, index);
  const total = state.plan.guide.length;
  const refs = (item.screens || []).map((ref) => h("span", { class: "ref-chip num", text: t("focus.screen_n", { ref }) }));
  return h(
    "li",
    { class: "guide-item", "data-question": item.id },
    h("span", { class: "guide-n num", text: index + 1 }),
    h(
      "div",
      { class: "guide-body" },
      h("p", { class: "guide-q", text: item.question }),
      item.purpose ? h("p", { class: "meta", text: t("focus.purpose", { purpose: item.purpose }) }) : null,
      refs.length || item.reveal_heatmap
        ? h(
            "p",
            { class: "guide-refs" },
            refs.length ? h("span", { class: "meta", text: t("focus.covers") }) : null,
            refs,
            item.reveal_heatmap ? h("span", { class: "tag", text: t("focus.reveal_tag") }) : null,
          )
        : null,
    ),
    h(
      "div",
      { class: "row-tools" },
      toolButton("up", t("focus.move_up"), () => moveQuestion(index, -1), { disabled: index === 0, move: -1 }),
      toolButton("down", t("focus.move_down"), () => moveQuestion(index, 1), { disabled: index === total - 1, move: 1 }),
      toolButton("pencil", t("focus.edit"), () => edit("question", item.id)),
      toolButton("trash", t("focus.remove_question", { n: index + 1 }), () => removeQuestion(index)),
    ),
  );
}

function guideEditor(item, index) {
  const question = h("textarea", { class: "prose", rows: "2", maxlength: "300" });
  question.value = item.question;
  const purpose = h("input", { class: "input", type: "text", value: item.purpose || "", maxlength: "200", autocomplete: "off" });
  const heatmap = state.session?.mode === "heatmap";
  const reveal = heatmap ? h("input", { type: "checkbox", checked: Boolean(item.reveal_heatmap) }) : null;
  const save = () => {
    const text = question.value.trim();
    if (!text) {
      removeQuestion(index, { quiet: true });
      return;
    }
    item.question = text;
    item.purpose = purpose.value.trim();
    if (heatmap && reveal.checked) {
      for (const other of state.plan.guide) other.reveal_heatmap = other === item;
    } else if (heatmap && item.reveal_heatmap) {
      // Unticked: the reveal moves to the default question (the second), or the first.
      const guide = state.plan.guide;
      const usual = guide[Math.min(1, guide.length - 1)];
      const fallback = usual === item ? guide.find((q) => q !== item) : usual;
      item.reveal_heatmap = !fallback;
      if (fallback) fallback.reveal_heatmap = true;
    }
    state.editing = null;
    renderPlan();
    $(`[data-question="${item.id}"] .icon-btn`, els.plan)?.focus();
  };
  const row = h(
    "li",
    { class: "guide-item is-editing", "data-question": item.id },
    h("span", { class: "guide-n num", text: index + 1 }),
    h(
      "div",
      { class: "editor-grid" },
      labelled(t("focus.field_question"), question, "wide"),
      labelled(t("focus.field_purpose"), purpose, "wide"),
      heatmap ? h("label", { class: "check wide" }, reveal, h("span", { text: t("focus.reveal") })) : null,
      h("div", { class: "editor-actions" }, h("button", { type: "button", class: "btn btn-sm", onclick: save, text: t("focus.done_edit") })),
    ),
  );
  row.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (!item.question) removeQuestion(index, { quiet: true });
      else {
        state.editing = null;
        renderPlan();
      }
    } else if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      save();
    }
  });
  requestAnimationFrame(() => question.focus());
  return row;
}

function labelled(text, control, wide = "") {
  return h("label", { class: wide ? "field wide" : "field" }, h("span", { text }), control);
}

function edit(kind, id) {
  state.editing = { kind, id };
  renderPlan();
}

function removePerson(id) {
  const [min] = limits.participants;
  if (state.plan.participants.length <= min) {
    toast(t("focus.min_people", { min }));
    return;
  }
  state.plan.participants = state.plan.participants.filter((person) => person.id !== id);
  renumber();
  renderPlan();
  renderPanel();
}

function addQuestion() {
  if (state.plan.guide.length >= MAX_QUESTIONS) {
    toast(t("focus.max_questions", { max: MAX_QUESTIONS }));
    return;
  }
  const item = { id: `q${state.plan.guide.length + 1}`, question: "", purpose: "" };
  if (state.session?.mode === "flow" || state.session?.mode === "ab") item.screens = [];
  if (state.session?.mode === "heatmap") item.reveal_heatmap = false;
  state.plan.guide.push(item);
  state.editing = { kind: "question", id: item.id };
  renderPlan();
}

function removeQuestion(index, { quiet = false } = {}) {
  if (state.plan.guide.length <= 1) {
    if (!quiet) toast(t("focus.min_questions"));
    state.editing = null;
    renderPlan();
    return;
  }
  const [removed] = state.plan.guide.splice(index, 1);
  if (removed?.reveal_heatmap && state.plan.guide.length) state.plan.guide[Math.min(1, state.plan.guide.length - 1)].reveal_heatmap = true;
  state.editing = null;
  renumber();
  renderPlan();
  renderPanel();
}

function moveQuestion(index, step) {
  const guide = state.plan.guide;
  const target = index + step;
  if (target < 0 || target >= guide.length) return;
  [guide[index], guide[target]] = [guide[target], guide[index]];
  renumber();
  renderPlan();
  $(`[data-question="${guide[target].id}"] [data-move="${step}"]`, els.plan)?.focus();
}

/** The server numbers participants and questions by position; keep the same ids here. */
function renumber() {
  state.plan.participants.forEach((person, index) => {
    person.id = `p${index + 1}`;
  });
  state.plan.guide.forEach((item, index) => {
    item.id = `q${index + 1}`;
  });
}

function screensSection(plan) {
  const shots = shotsByRef();
  return h(
    "section",
    { class: "plan-section" },
    h("div", { class: "section-head" }, h("h3", { text: t("focus.screens") })),
    h(
      "ol",
      { class: "screens" },
      plan.screens.map((screen) =>
        h(
          "li",
          { class: "screen" },
          shots[screen.ref]
            ? h("img", { src: shots[screen.ref].url, alt: "", loading: "lazy" })
            : h("span", { class: "screen-missing", "aria-hidden": "true" }, icon("image")),
          h(
            "div",
            {},
            h("p", { class: "screen-title" }, h("span", { class: "ref-chip num", text: screen.ref === "S" ? t("focus.mode_heatmap") : t("focus.screen_n", { ref: screen.ref }) }), " ", screen.title || ""),
            screen.description ? h("p", { class: "meta", text: screen.description }) : null,
          ),
        ),
      ),
    ),
  );
}

function shotsByRef() {
  const out = {};
  const mode = state.session?.mode;
  state.shots.a.forEach((shot, index) => {
    out[mode === "ab" ? `A${index + 1}` : mode === "heatmap" ? "S" : String(index + 1)] = shot;
  });
  if (mode === "ab") state.shots.b.forEach((shot, index) => (out[`B${index + 1}`] = shot));
  return out;
}

function attentionSection(plan) {
  const page = plan.screens?.[0];
  return h(
    "section",
    { class: "plan-section" },
    h("div", { class: "section-head" }, h("h3", { text: t("focus.attention_notes") })),
    h(
      "div",
      { class: "attention" },
      h("figure", { class: "attention-figure" }, h("img", { src: state.attention.overlay, alt: t("focus.overlay_alt") })),
      h(
        "div",
        { class: "attention-side" },
        page?.title || page?.description
          ? h("p", { class: "attention-page" }, h("b", { text: page.title || "" }), page.title && page.description ? colon() : "", page.description || "")
          : null,
        plan.attention?.length
          ? h(
              "ul",
              { class: "attention-notes" },
              plan.attention.map((note) =>
                h(
                  "li",
                  {},
                  h("p", {}, h("b", { text: note.element }), " ", h("span", { class: `tag tag-level-${note.level}`, text: t(`focus.level.${note.level}`) })),
                  note.note ? h("p", { class: "meta", text: note.note }) : null,
                ),
              ),
            )
          : null,
      ),
    ),
  );
}

// ── Step 3: the discussion ────────────────────────────────────────────────────

function planProblem() {
  const [min, max] = limits.participants;
  const people = state.plan.participants;
  if (people.length < min || people.length > max || people.some((person) => !person.name.trim())) return true;
  return !state.plan.guide.length || state.plan.guide.some((item) => !item.question.trim());
}

async function discuss() {
  if (!state.plan || busy()) return;
  if (state.editing) {
    state.editing = null;
    renderPlan();
  }
  if (planProblem()) {
    fail(new ApiError("focus_invalid_plan"));
    return;
  }
  clearError();
  dropLive();
  setPhase("discussing");
  startClock();
  progress();
  focusSheet();

  const controller = new AbortController();
  state.abort = controller;
  try {
    const body = briefForm(state.session);
    body.append("plan", JSON.stringify(state.plan));
    body.append("transcript", JSON.stringify(state.turns));
    await appendShots(body, state.session.mode, limits.talk_edge, 0.82);
    if (state.session.mode === "heatmap" && state.attention?.overlay) body.append("overlay", dataUrlFile(state.attention.overlay, "overlay.jpg"));
    await request("/api/focus-group/discuss", body, controller, (name, data) => {
      if (name === "turn") startTurn(data);
      else if (name === "delta") appendDelta(data);
      else if (name === "turn_end") endTurn(data);
    });
    stopClock();
    dropLive();
    writeReport();
  } catch (error) {
    stopClock();
    dropLive();
    if (error.name === "AbortError") {
      if (state.phase === "discussing") setPhase("stopped");
      return;
    }
    state.phase = "stopped";
    fail(error, discuss);
    renderAll();
  } finally {
    if (state.abort === controller) state.abort = null;
  }
}

function stop() {
  state.abort?.abort();
}

function progress() {
  const total = state.plan.guide.length;
  const last = state.live?.turn || state.turns.at(-1);
  const index = last ? last.question : 0;
  if (index >= total) status.working("status.focus_closing", { since: clockOrigin(), hintKey: "status.focus_discuss_hint" });
  else status.working("status.focus_discuss", { params: { n: index + 1, total }, since: clockOrigin(), hintKey: "status.focus_discuss_hint" });
}

function startTurn(turn) {
  const following = isFollowing();
  const { node, text } = turnNode({ ...turn, text: "" }, state.plan, { attention: state.attention });
  node.classList.add("is-live");
  const caret = h("span", { class: "caret", "aria-hidden": "true" });
  text.append(caret);
  const written = document.createTextNode("");
  text.prepend(written);
  els.transcript.append(node);
  state.live = { turn, node, text, written };
  if (following) keepInView();
  const previous = state.turns.at(-1);
  if (!previous || previous.question !== turn.question) progress();
}

function appendDelta(delta) {
  if (!state.live || state.live.turn.slot !== delta.slot) return;
  const following = isFollowing();
  state.live.written.data += delta.text;
  if (following) keepInView();
}

function endTurn(turn) {
  const live = state.live;
  if (live && live.turn.slot === turn.slot) {
    live.text.textContent = turn.text;
    live.node.classList.remove("is-live");
  } else {
    els.transcript.append(turnNode(turn, state.plan, { attention: state.attention }).node);
  }
  state.live = null;
  state.turns.push(turn);
}

/** A turn cut off by a stop or an error is dropped; resuming starts it again. */
function dropLive() {
  state.live?.node.remove();
  state.live = null;
}

/** Follow the stream only while the transcript's end is on screen; never pull back a reader who scrolled away. */
function isFollowing() {
  const box = els.transcript.getBoundingClientRect();
  return box.bottom >= 0 && box.bottom <= window.innerHeight + 160;
}

function keepInView() {
  if (follow) return;
  follow = requestAnimationFrame(() => {
    follow = null;
    const node = state.live?.node || els.transcript.lastElementChild;
    node?.scrollIntoView({ block: "end" });
  });
}

function renderTranscript() {
  els.transcript.replaceChildren(
    ...state.turns.map((turn) => turnNode(turn, state.plan, { attention: state.attention }).node),
  );
  if (state.live) els.transcript.append(state.live.node);
  renderRoster();
}

function renderRoster() {
  const plan = state.plan;
  if (!plan) {
    els.roster.replaceChildren();
    return;
  }
  els.roster.replaceChildren(
    ...plan.participants.map((person) =>
      h(
        "li",
        { class: "roster-chip", title: [person.summary, person.profile].filter(Boolean).join("\n") },
        avatar(person.name, "avatar avatar-sm"),
        h("span", { text: person.name }),
        stanceTag(person.stance),
      ),
    ),
  );
}

// ── Step 4: the report ────────────────────────────────────────────────────────

async function writeReport() {
  if (!state.turns.some((turn) => turn.speaker !== "moderator")) {
    fail(new ApiError("focus_transcript_empty"));
    return;
  }
  clearError();
  state.reportFailed = false;
  setPhase("reporting");
  startClock();
  status.working("status.focus_report", { since: clockOrigin(), hintKey: "status.focus_report_hint" });

  const controller = new AbortController();
  state.abort = controller;
  try {
    const body = briefForm(state.session);
    body.append("plan", JSON.stringify(state.plan));
    body.append("transcript", JSON.stringify(state.turns));
    let report = null;
    await request("/api/focus-group/report", body, controller, (name, data) => {
      if (name === "report") report = data.report;
    });
    if (!report) throw new ApiError("focus_bad_output");
    state.report = report;
    stopClock();
    setPhase("done");
    status.done("focus.done", { time: duration(state.active, lang()) });
    revealReport();
  } catch (error) {
    stopClock();
    if (error.name === "AbortError") return;
    state.reportFailed = true;
    state.phase = state.report ? "done" : "stopped";
    fail(error, writeReport);
    renderAll();
  } finally {
    if (state.abort === controller) state.abort = null;
  }
}

function revealReport() {
  els.results.hidden = false;
  const heading = $("h2", els.results);
  heading.tabIndex = -1;
  heading.focus({ preventScroll: true });
  els.results.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
}

function renderReport() {
  const report = state.report;
  els.results.hidden = !report;
  if (!report) {
    els.report.replaceChildren();
    return;
  }
  const plan = state.plan;
  const people = Object.fromEntries(plan.participants.map((person) => [person.id, person]));
  const speakers = state.turns.filter((turn) => turn.speaker !== "moderator").length;
  els.reportMeta.textContent = t("focus.report_meta", {
    people: plan.participants.length,
    questions: plan.guide.length,
    turns: speakers,
  });

  const blocks = [
    h(
      "div",
      { class: "report-lead" },
      report.headline ? h("p", { class: "report-headline", text: report.headline }) : null,
      report.summary ? h("p", { class: "report-summary", text: report.summary }) : null,
    ),
  ];
  if (report.findings?.length) {
    blocks.push(
      reportSection(
        t("focus.findings"),
        h(
          "ol",
          { class: "findings" },
          report.findings.map((finding, index) =>
            h(
              "li",
              { class: "finding" },
              h("span", { class: "finding-n num", text: index + 1 }),
              h(
                "div",
                { class: "finding-body" },
                h("h4", {}, finding.title, " ", h("span", { class: "tag", text: t(`focus.evidence.${finding.evidence}`) })),
                finding.detail ? h("p", { text: finding.detail }) : null,
                finding.quotes?.length
                  ? h(
                      "ul",
                      { class: "quotes" },
                      finding.quotes.map((quote) =>
                        h(
                          "li",
                          { class: "quote" },
                          h("p", { class: "quote-text", text: `“${quote.text}”` }),
                          h("p", { class: "quote-by" }, avatar(people[quote.speaker]?.name || "?", "avatar avatar-xs"), h("span", { text: people[quote.speaker]?.name || quote.speaker })),
                        ),
                      ),
                    )
                  : null,
                whoLine(finding.participants, people),
              ),
            ),
          ),
        ),
      ),
    );
  }
  if (report.stances?.length) blocks.push(stanceSection(report.stances, people));
  const columns = [];
  if (report.pain_points?.length) columns.push(pointList(t("focus.pain_points"), report.pain_points, people));
  if (report.needs?.length) columns.push(pointList(t("focus.needs"), report.needs, people));
  if (columns.length) blocks.push(h("div", { class: "report-columns" }, columns));
  if (report.ab) blocks.push(abSection(report.ab, people));
  if (report.flow_issues?.length) {
    blocks.push(
      reportSection(
        t("focus.flow_issues"),
        dataTable(
          [t("focus.col_screens"), t("focus.col_issue"), t("focus.col_severity"), t("focus.col_suggestion")],
          report.flow_issues.map((issue) => [
            issue.screens.map((ref) => t("focus.screen_n", { ref })).join(t("focus.list_sep")) || "—",
            issue.issue,
            h("span", { class: `tag tag-severity-${issue.severity}`, text: t(`focus.severity.${issue.severity}`) }),
            issue.suggestion || "—",
          ]),
        ),
      ),
    );
  }
  if (report.attention?.length) {
    blocks.push(
      reportSection(
        t("focus.attention"),
        dataTable(
          [t("focus.col_element"), t("focus.col_predicted"), t("focus.col_feedback"), t("focus.col_match")],
          report.attention.map((row) => [
            row.element,
            row.predicted || "—",
            row.feedback || "—",
            h("span", { class: `tag tag-match-${row.match}`, text: t(`focus.match.${row.match}`) }),
          ]),
        ),
      ),
    );
  }
  if (report.disagreements?.length) {
    blocks.push(
      reportSection(
        t("focus.disagreements"),
        h(
          "ul",
          { class: "points" },
          report.disagreements.map((item) =>
            h(
              "li",
              {},
              h("p", { class: "point-title", text: item.topic }),
              h(
                "ul",
                { class: "sides" },
                item.sides.map((side) => h("li", {}, faces(side.participants, people), h("p", { text: side.view }))),
              ),
            ),
          ),
        ),
      ),
    );
  }
  if (report.opportunities?.length) {
    blocks.push(
      reportSection(
        t("focus.opportunities"),
        h(
          "ol",
          { class: "points numbered" },
          report.opportunities.map((item) =>
            h(
              "li",
              {},
              h("p", { class: "point-title" }, item.title, " ", h("span", { class: `tag tag-priority-${item.priority}`, text: t(`focus.priority.${item.priority}`) })),
              item.detail ? h("p", { text: item.detail }) : null,
              item.related?.length ? h("p", { class: "meta", text: t("focus.related", { list: item.related.join(t("focus.list_sep")) }) }) : null,
            ),
          ),
        ),
      ),
    );
  }
  if (report.validate?.length) {
    blocks.push(reportSection(t("focus.validate"), h("ol", { class: "plain-list" }, report.validate.map((line) => h("li", { text: line })))));
  }
  if (report.limitations) {
    blocks.push(
      h(
        "div",
        { class: "alert alert-info report-limits", role: "note" },
        icon("info"),
        h("div", { class: "alert-body" }, h("p", {}, h("b", { text: t("focus.limitations") + colon() }), report.limitations)),
      ),
    );
  }
  if (state.usage.calls) {
    blocks.push(
      h("p", {
        class: "meta num report-usage",
        text: t("focus.usage", {
          calls: state.usage.calls,
          input: tokens(state.usage.prompt_tokens),
          hit: percent(state.usage.cache_hit_tokens, state.usage.prompt_tokens),
          output: tokens(state.usage.completion_tokens),
        }),
      }),
    );
  }
  els.report.replaceChildren(...blocks);
}

function reportSection(title, content) {
  return h("section", { class: "report-section" }, h("h3", { text: title }), content);
}

function pointList(title, points, people) {
  return reportSection(
    title,
    h(
      "ul",
      { class: "points" },
      points.map((point) =>
        h(
          "li",
          {},
          h("p", { class: "point-title", text: point.title }),
          point.detail ? h("p", { text: point.detail }) : null,
          whoLine(point.participants, people),
        ),
      ),
    ),
  );
}

function stanceSection(stances, people) {
  const order = ["positive", "mixed", "neutral", "negative"];
  const counts = Object.fromEntries(order.map((key) => [key, stances.filter((s) => s.stance === key).length]));
  const total = stances.length;
  const bar = h(
    "div",
    { class: "split-bar", role: "img", "aria-label": order.filter((k) => counts[k]).map((k) => `${t(`focus.final.${k}`)} ${counts[k]}`).join(", ") },
    order.filter((key) => counts[key]).map((key) => h("span", { class: `split-${key}`, style: { flexGrow: String(counts[key]) } })),
  );
  const legend = h(
    "ul",
    { class: "split-legend num" },
    order
      .filter((key) => counts[key])
      .map((key) => h("li", {}, h("span", { class: `swatch-dot split-${key}`, "aria-hidden": "true" }), `${t(`focus.final.${key}`)} ${t("focus.people_n", { n: counts[key] })}`)),
  );
  const rows = h(
    "ul",
    { class: "stance-rows" },
    stances.map((item) =>
      h(
        "li",
        {},
        avatar(people[item.participant]?.name || "?", "avatar avatar-sm"),
        h(
          "div",
          {},
          h("p", {}, h("b", { text: people[item.participant]?.name || item.participant }), " ", h("span", { class: `tag tag-final-${item.stance}`, text: t(`focus.final.${item.stance}`) })),
          item.summary ? h("p", { class: "meta", text: item.summary }) : null,
        ),
      ),
    ),
  );
  return reportSection(t("focus.stances"), h("div", { class: "stances", "data-total": total }, bar, legend, rows));
}

function abSection(ab, people) {
  const groups = [
    ["a", t("focus.ab_a")],
    ["undecided", t("focus.ab_undecided")],
    ["b", t("focus.ab_b")],
  ].filter(([key]) => ab[key]?.length);
  const bar = h(
    "div",
    { class: "split-bar", role: "img", "aria-label": groups.map(([key, label]) => `${label} ${ab[key].length}`).join(", ") },
    groups.map(([key]) => h("span", { class: `split-ab-${key}`, style: { flexGrow: String(ab[key].length) } })),
  );
  const legend = h(
    "ul",
    { class: "split-legend num" },
    groups.map(([key, label]) =>
      h("li", {}, h("span", { class: `swatch-dot split-ab-${key}`, "aria-hidden": "true" }), `${label} ${t("focus.people_n", { n: ab[key].length })}`, " ", faces(ab[key], people)),
    ),
  );
  const list = (title, items) =>
    items?.length ? h("div", {}, h("h4", { text: title }), h("ul", { class: "plain-list" }, items.map((line) => h("li", { text: line })))) : null;
  return reportSection(
    t("focus.ab"),
    h(
      "div",
      { class: "ab" },
      bar,
      legend,
      h(
        "div",
        { class: "report-columns" },
        list(t("focus.ab_strengths", { version: t("focus.version_a") }), ab.a_strengths),
        list(t("focus.ab_strengths", { version: t("focus.version_b") }), ab.b_strengths),
        list(t("focus.ab_issues", { version: t("focus.version_a") }), ab.a_issues),
        list(t("focus.ab_issues", { version: t("focus.version_b") }), ab.b_issues),
      ),
      ab.verdict ? h("p", { class: "ab-verdict" }, h("b", { text: t("focus.ab_verdict") + colon() }), ab.verdict) : null,
    ),
  );
}

function dataTable(headers, rows) {
  return h(
    "div",
    { class: "table-wrap" },
    h(
      "table",
      { class: "report-table" },
      h("thead", {}, h("tr", {}, headers.map((label) => h("th", { scope: "col", text: label })))),
      h("tbody", {}, rows.map((cells) => h("tr", {}, cells.map((cell) => h("td", {}, cell))))),
    ),
  );
}

function whoLine(ids, people) {
  return ids?.length ? h("p", { class: "who" }, faces(ids, people)) : null;
}

function faces(ids, people) {
  return h(
    "span",
    { class: "faces" },
    (ids || []).map((id) => h("span", { class: "face", title: people[id]?.name || id }, avatar(people[id]?.name || "?", "avatar avatar-xs"), h("span", { text: people[id]?.name || id }))),
  );
}

// ── Sample and exports ────────────────────────────────────────────────────────

function showSession(data) {
  reset();
  Object.assign(state, {
    session: { lang: data.lang, ...data.brief },
    plan: data.plan,
    attention: data.attention || null,
    turns: data.transcript || [],
    report: data.report || null,
    usage: { ...emptyUsage(), ...(data.usage || {}) },
    active: data.elapsed_seconds || 0,
    sample: { generator: data.generator || "", topic: data.brief?.topic || "" },
    phase: "done",
  });
  for (const model of data.models || []) state.models.add(model);
  renderAll();
  status.info("focus.sample_done");
  focusSheet();
}

function sampleBanner() {
  if (!state.sample) return null;
  const claude = state.sample.generator.startsWith("claude");
  return h(
    "div",
    { class: "alert alert-info sample-banner", role: "note" },
    icon("info"),
    h("div", { class: "alert-body" }, h("p", { text: t(claude ? "focus.sample_by_claude" : "focus.sample_by_tool", { topic: state.sample.topic }) })),
  );
}

function exportData() {
  return {
    format: "focal-focus-group",
    version: 1,
    generator: state.sample?.generator || `deepseek${state.models.size ? `:${[...state.models].join("+")}` : ""}`,
    lang: state.session.lang,
    created: new Date().toISOString().slice(0, 10),
    brief: {
      topic: state.session.topic,
      context: state.session.context,
      audience: state.session.audience,
      mode: state.session.mode,
      participants: state.plan.participants.length,
    },
    plan: state.plan,
    attention: state.attention,
    transcript: state.turns,
    report: state.report,
    usage: state.usage,
    models: [...state.models],
    elapsed_seconds: Math.round(state.active),
  };
}

/** ASCII only: browsers drop a download name with Chinese in it and save "download". */
function fileStem() {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const stamp = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}`;
  return state.sample ? "focus-group-sample" : `focus-group-${stamp}`;
}

function downloadMarkdown() {
  download(new Blob([markdown()], { type: "text/markdown;charset=utf-8" }), `${fileStem()}.md`);
}

function downloadJson() {
  download(new Blob([JSON.stringify(exportData(), null, 2)], { type: "application/json" }), `${fileStem()}.json`);
}

async function copyMarkdown() {
  try {
    await navigator.clipboard.writeText(markdown());
    toast(t("focus.copied"));
  } catch {
    toast(t("tool.copy_failed"));
  }
}

/** The report, brief and transcript as Markdown, in the session's language. */
function markdown() {
  const L = state.session.lang;
  const tt = (key, params) => tFor(L, key, params);
  const sep = tt("focus.list_sep");
  const report = state.report || {};
  const plan = state.plan;
  const people = Object.fromEntries(plan.participants.map((person) => [person.id, person]));
  const names = (ids) => (ids || []).map((id) => people[id]?.name || id).join(sep);
  const colon = L === "zh" ? "：" : ": ";
  const paren = (text) => (L === "zh" ? `（${text}）` : ` (${text})`);
  const out = [];
  const add = (...lines) => out.push(...lines);
  const cell = (text) => String(text ?? "").replace(/\|/g, "\\|").replace(/\n/g, " ");

  add(`# ${plan.title || state.session.topic}`, "");
  const by = state.sample?.generator.startsWith("claude") ? tt("focus.md_by_claude") : tt("focus.md_by_tool");
  add(`> ${by} · ${new Date().toISOString().slice(0, 10)}`, `> ${tt("focus.md_note")}`, "");
  if (report.headline) add(`**${report.headline}**`, "");
  if (report.summary) add(report.summary, "");
  if (report.findings?.length) {
    add(`## ${tt("focus.findings")}`, "");
    report.findings.forEach((finding, index) => {
      add(`${index + 1}. **${finding.title}**${paren(tt(`focus.evidence.${finding.evidence}`))}`);
      if (finding.detail) add(`   ${finding.detail}`);
      for (const quote of finding.quotes || []) add(`   > “${quote.text}” —— ${people[quote.speaker]?.name || quote.speaker}`);
      if (finding.participants?.length) add(`   ${names(finding.participants)}`);
      add("");
    });
  }
  if (report.stances?.length) {
    add(`## ${tt("focus.stances")}`, "");
    for (const item of report.stances) add(`- **${people[item.participant]?.name || item.participant}**${paren(tt(`focus.final.${item.stance}`))}${item.summary ? colon + item.summary : ""}`);
    add("");
  }
  for (const [key, title] of [
    ["pain_points", "focus.pain_points"],
    ["needs", "focus.needs"],
  ]) {
    if (!report[key]?.length) continue;
    add(`## ${tt(title)}`, "");
    for (const point of report[key]) add(`- **${point.title}**${point.detail ? colon + point.detail : ""}${point.participants?.length ? paren(names(point.participants)) : ""}`);
    add("");
  }
  if (report.ab) {
    const ab = report.ab;
    add(`## ${tt("focus.ab")}`, "");
    add(`- ${tt("focus.ab_a")}${colon}${names(ab.a) || "—"}`, `- ${tt("focus.ab_b")}${colon}${names(ab.b) || "—"}`, `- ${tt("focus.ab_undecided")}${colon}${names(ab.undecided) || "—"}`, "");
    for (const [key, label, version] of [
      ["a_strengths", "focus.ab_strengths", "focus.version_a"],
      ["b_strengths", "focus.ab_strengths", "focus.version_b"],
      ["a_issues", "focus.ab_issues", "focus.version_a"],
      ["b_issues", "focus.ab_issues", "focus.version_b"],
    ]) {
      if (!ab[key]?.length) continue;
      add(`**${tt(label, { version: tt(version) })}**`, ...ab[key].map((line) => `- ${line}`), "");
    }
    if (ab.verdict) add(`**${tt("focus.ab_verdict")}**${colon}${ab.verdict}`, "");
  }
  if (report.flow_issues?.length) {
    add(`## ${tt("focus.flow_issues")}`, "");
    add(`| ${tt("focus.col_screens")} | ${tt("focus.col_issue")} | ${tt("focus.col_severity")} | ${tt("focus.col_suggestion")} |`, "| --- | --- | --- | --- |");
    for (const issue of report.flow_issues) {
      const refs = issue.screens.map((ref) => tt("focus.screen_n", { ref })).join(sep) || "—";
      add(`| ${cell(refs)} | ${cell(issue.issue)} | ${tt(`focus.severity.${issue.severity}`)} | ${cell(issue.suggestion || "—")} |`);
    }
    add("");
  }
  if (report.attention?.length) {
    add(`## ${tt("focus.attention")}`, "");
    add(`| ${tt("focus.col_element")} | ${tt("focus.col_predicted")} | ${tt("focus.col_feedback")} | ${tt("focus.col_match")} |`, "| --- | --- | --- | --- |");
    for (const row of report.attention) add(`| ${cell(row.element)} | ${cell(row.predicted || "—")} | ${cell(row.feedback || "—")} | ${tt(`focus.match.${row.match}`)} |`);
    add("");
  }
  if (report.disagreements?.length) {
    add(`## ${tt("focus.disagreements")}`, "");
    for (const item of report.disagreements) {
      add(`- **${item.topic}**`);
      for (const side of item.sides) add(`  - ${names(side.participants) || "—"}${colon}${side.view}`);
    }
    add("");
  }
  if (report.opportunities?.length) {
    add(`## ${tt("focus.opportunities")}`, "");
    report.opportunities.forEach((item, index) => {
      const related = item.related?.length ? paren(tt("focus.related", { list: item.related.join(sep) })) : "";
      add(`${index + 1}. **${item.title}**${paren(tt(`focus.priority.${item.priority}`))}${item.detail ? colon + item.detail : ""}${related}`);
    });
    add("");
  }
  if (report.validate?.length) add(`## ${tt("focus.validate")}`, "", ...report.validate.map((line) => `- ${line}`), "");
  if (report.limitations) add(`## ${tt("focus.limitations")}`, "", report.limitations, "");

  add(`## ${tt("focus.md_brief")}`, "");
  add(`- **${tt("focus.md_topic")}**${colon}${state.session.topic}`);
  if (plan.objective) add(`- **${tt("focus.md_objective")}**${colon}${plan.objective}`);
  if (state.session.audience) add(`- **${tt("focus.md_audience")}**${colon}${state.session.audience}`);
  if (state.session.context) add(`- **${tt("focus.md_context")}**${colon}${state.session.context}`);
  add("", `### ${tt("focus.participants")}`, "");
  for (const person of plan.participants) {
    add(`- **${person.name}**${person.summary ? paren(person.summary) : ""} · ${tt(`focus.stance.${person.stance}`)}${person.profile ? colon + person.profile : ""}`);
  }
  add("", `### ${tt("focus.guide")}`, "");
  plan.guide.forEach((item, index) => add(`${index + 1}. ${item.question}`));
  add("", `## ${tt("focus.md_appendix")}`, "");
  for (const turn of state.turns) {
    const name = turn.speaker === "moderator" ? tt("focus.moderator") : people[turn.speaker]?.name || turn.speaker;
    add(`**${name}**${colon}${turn.text}`, "");
  }
  return out.join("\n").replace(/\n{3,}/g, "\n\n").trim() + "\n";
}

// ── Rendering ─────────────────────────────────────────────────────────────────

function renderAll() {
  const phase = state.phase;
  const briefStage = phase === "brief" || phase === "recruiting";
  els.brief.hidden = !briefStage;
  els.plan.hidden = phase !== "plan";
  els.session.hidden = briefStage || phase === "plan";
  els.countSection.hidden = !briefStage;
  els.sheet.classList.toggle("is-busy", busy());
  els.sheet.setAttribute("aria-busy", String(busy()));

  $(".sample-banner", els.sheet)?.remove();
  const banner = sampleBanner();
  if (banner) els.sheet.prepend(banner);

  renderBrief();
  renderCount();
  if (phase === "plan") renderPlan();
  if (!els.session.hidden) renderTranscript();
  renderReport();
  renderSteps();
  renderPanel();
  renderStatus();
}

function renderCount() {
  for (const button of $$("[data-count]", root)) button.setAttribute("aria-pressed", String(Number(button.dataset.count) === state.count));
}

function renderSteps() {
  const current = { brief: "recruit", recruiting: "recruit", plan: "discuss", discussing: "discuss", stopped: "discuss", reporting: "report", done: "report" }[state.phase];
  const order = ["recruit", "discuss", "report"];
  const finished = state.phase === "done" && Boolean(state.report);
  const reached = order.indexOf(current);
  for (const step of els.steps) {
    const index = order.indexOf(step.dataset.step);
    step.classList.toggle("is-done", finished || index < reached);
    if (index === reached && !finished) step.setAttribute("aria-current", "step");
    else step.removeAttribute("aria-current");
  }
}

function renderStatus() {
  if (state.error && !busy()) {
    status.clear();
    return;
  }
  const phase = state.phase;
  if (phase === "brief") {
    if (!available) status.info("focus.unavailable");
    else if (state.mode === "ab" && (!state.shots.a.length || !state.shots.b.length)) status.info("focus.need_ab");
    else if (state.mode !== "text" && !state.shots.a.length) status.info("focus.need_shots");
    else if (!els.topic.value.trim()) status.info("focus.idle");
    else status.info("focus.ready");
  } else if (phase === "plan") {
    status.done("focus.planned", { count: state.plan.participants.length, questions: state.plan.guide.length });
  } else if (phase === "stopped") {
    const last = state.turns.at(-1);
    const total = state.plan.guide.length;
    status.info("focus.stopped", { n: Math.min((last?.question ?? 0) + 1, total), total });
  } else if (phase === "done" && state.sample) {
    status.info("focus.sample_done");
  } else if (phase === "done" && state.report) {
    status.done("focus.done", { time: duration(state.active, lang()) });
  }
  // Working phases keep the status their step set, counting as it goes.
}

function renderPanel() {
  if (!els) return;
  const phase = state.phase;
  const actions = [];
  const primary = (label, iconName, onclick, { disabled = false, busy: working = false } = {}) =>
    h(
      "button",
      { type: "button", class: "btn btn-primary btn-lg btn-block", disabled: disabled || working, "aria-busy": working ? "true" : null, onclick },
      icon(iconName),
      h("span", { text: label }),
    );
  const secondary = (label, iconName, onclick, { quiet = false, disabled = false } = {}) =>
    h("button", { type: "button", class: quiet ? "btn btn-quiet" : "btn", disabled, onclick }, iconName ? icon(iconName) : null, h("span", { text: label }));
  const row = (...buttons) => h("div", { class: "panel-row" }, buttons.filter(Boolean));

  if (phase === "brief") {
    const canReturn = state.plan && state.planKey === materialKey();
    actions.push(primary(t(state.plan ? "focus.rerecruit" : "focus.recruit"), state.plan ? "refresh" : "group", recruit, { disabled: !ready() }));
    if (canReturn) actions.push(secondary(t("focus.back_to_plan"), "left", () => setPhase("plan")));
  } else if (phase === "recruiting") {
    actions.push(primary(t("focus.recruiting"), "group", null, { busy: true }));
    actions.push(secondary(t("focus.cancel"), "x", stop));
  } else if (phase === "plan") {
    actions.push(primary(t("focus.start"), "chat", discuss, { disabled: !available }));
    actions.push(row(secondary(t("focus.rerecruit"), "refresh", recruit, { disabled: !available }), secondary(t("focus.edit_brief"), "pencil", () => setPhase("brief"))));
  } else if (phase === "discussing") {
    actions.push(primary(t("focus.running"), "chat", null, { busy: true }));
    actions.push(secondary(t("focus.stop"), "stop", stop));
  } else if (phase === "stopped") {
    const complete = closingDone();
    if (complete) actions.push(primary(t("focus.rewrite_report"), "report", writeReport, { disabled: !available }));
    else actions.push(primary(t("focus.resume"), "chat", discuss, { disabled: !available }));
    const canReport = state.turns.some((turn) => turn.speaker !== "moderator");
    if (!complete) actions.push(secondary(t("focus.report_now"), "report", writeReport, { disabled: !canReport || !available }));
    actions.push(secondary(t("focus.new"), "plus", reset, { quiet: true }));
  } else if (phase === "reporting") {
    actions.push(primary(t("focus.writing"), "report", null, { busy: true }));
  } else if (phase === "done") {
    if (state.sample) {
      actions.push(primary(t("focus.sample_own"), "plus", reset));
      if (state.report) actions.push(row(secondary(t("focus.download_md"), "download", downloadMarkdown), secondary(t("focus.download_json"), "code", downloadJson)));
    } else {
      actions.push(primary(t("focus.download_md"), "download", downloadMarkdown, { disabled: !state.report }));
      actions.push(row(secondary(t("focus.copy_md"), "copy", copyMarkdown, { disabled: !state.report }), secondary(t("focus.download_json"), "code", downloadJson)));
      actions.push(row(secondary(t("focus.rewrite_report"), "refresh", writeReport, { quiet: true, disabled: !available }), secondary(t("focus.new"), "plus", reset, { quiet: true })));
    }
  }
  els.actions.replaceChildren(...actions);
  // On narrow screens the panel sits below the transcript; the dock keeps Stop in reach.
  els.dockStop.hidden = phase !== "discussing";
}

function closingDone() {
  if (!state.plan) return false;
  const finals = new Set(state.turns.filter((turn) => turn.kind === "final").map((turn) => turn.speaker));
  return state.plan.participants.every((person) => finals.has(person.id));
}

function focusSheet() {
  els.sheet.scrollIntoView({ block: "nearest" });
}

// ── Errors ────────────────────────────────────────────────────────────────────

function fail(error, retry = null) {
  state.error = error;
  state.retry = retry;
  showNotice(els.notices, error, retry ? () => retry() : null);
  renderStatus();
  renderPanel();
}

function clearError() {
  state.error = null;
  state.retry = null;
  clearNotice(els.notices);
}

// ── Small helpers ─────────────────────────────────────────────────────────────

function colon() {
  return lang() === "zh" ? "：" : ": ";
}

function emptyUsage() {
  return { calls: 0, prompt_tokens: 0, cache_hit_tokens: 0, completion_tokens: 0, reasoning_tokens: 0 };
}

function turnLabel(turn, plan, total) {
  if (turn.kind === "ask") return t("focus.turn_question", { n: turn.question + 1, total });
  if (turn.kind === "probe") {
    const names = (turn.targets || []).map((id) => plan.participants.find((p) => p.id === id)?.name || id);
    return t("focus.turn_probe", { names: names.join(t("focus.list_sep")) });
  }
  if (turn.kind === "close") return t("focus.turn_close");
  if (turn.kind === "final") return t("focus.turn_final");
  if (turn.probe) return t("focus.turn_reply");
  return "";
}

function monogram(name) {
  const text = (name || "").trim();
  if (!text) return "?";
  if (/^[㐀-鿿]/.test(text)) return text[0];
  const words = text.split(/\s+/).filter(Boolean);
  return ((words[0]?.[0] || "") + (words.length > 1 ? words.at(-1)[0] : "")).toUpperCase();
}

function avatar(name, className = "avatar") {
  return h("span", { class: className, "aria-hidden": "true", text: monogram(name) });
}

function stanceTag(stance) {
  return h("span", { class: `tag tag-stance-${stance}`, text: t(`focus.stance.${stance}`) });
}

function duration(seconds, language) {
  const total = Math.max(1, Math.round(seconds));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return m ? tFor(language, "focus.time", { m, s }) : tFor(language, "focus.time_s", { s });
}

function tokens(value) {
  const n = Number(value) || 0;
  if (lang() === "zh") return n >= 10000 ? `${(n / 10000).toFixed(1)} 万` : String(n);
  return n >= 1000 ? `${(n / 1000).toFixed(1)}K` : String(n);
}

function percent(part, whole) {
  return whole ? `${Math.round((part / whole) * 100)}%` : "0%";
}

function dataUrlFile(url, name) {
  const [head, data] = url.split(",", 2);
  const type = /data:([^;]+)/.exec(head)?.[1] || "image/jpeg";
  const bytes = Uint8Array.from(atob(data), (char) => char.charCodeAt(0));
  return new File([bytes], name, { type });
}
