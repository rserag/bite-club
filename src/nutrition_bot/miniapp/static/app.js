"use strict";
const tg = window.Telegram?.WebApp;
const $ = (id) => document.getElementById(id);
const state = {
  dashboard: null,
  foods: [],
  meals: [],
  portions: [],
  editing: null,
  busy: false,
  submission: null,
};
const node = (tag, text, className) => {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
};
const fmt = (value, digits = 0) =>
  value === null || value === undefined
    ? "—"
    : Number(value).toLocaleString(undefined, {
        maximumFractionDigits: digits,
      });
function notice(text, error = false) {
  $("notice").textContent = text;
  $("notice").classList.toggle("error", error);
}
function pendingRequest() {
  try {
    return sessionStorage.getItem("biteclub-pending");
  } catch {
    return null;
  }
}
function rememberRequest(id) {
  try {
    sessionStorage.setItem("biteclub-pending", id);
    return true;
  } catch {
    return false;
  }
}
function forgetRequest() {
  try {
    sessionStorage.removeItem("biteclub-pending");
  } catch {
    // No additional mutation can start if its retry identifier cannot be saved.
  }
}
async function api(path, options = {}) {
  if (!tg?.initData)
    throw new Error(
      "Open Bite Club from your private Telegram chat to see your diary.",
    );
  const response = await fetch(path, {
    ...options,
    headers: { "X-Telegram-Init-Data": tg.initData, ...options.headers },
    cache: "no-store",
    credentials: "omit",
    referrerPolicy: "no-referrer",
  });
  const body = await response.json();
  if (!response.ok) {
    const error = new Error(body.error || "Please try again.");
    error.status = response.status;
    throw error;
  }
  return body;
}
async function load() {
  try {
    notice("Refreshing your diary…");
    const day = $("day").value;
    state.dashboard = await api(
      "/api/dashboard" + (day ? "?day=" + encodeURIComponent(day) : ""),
    );
    renderDashboard();
    notice("Your private diary · " + state.dashboard.timezone);
    await Promise.all([loadFoods(), loadMeals()]);
  } catch (error) {
    notice(error.message, true);
  }
}
function renderDashboard() {
  const data = state.dashboard;
  $("day").value = data.date;
  $("day").max = data.today || data.date;
  $("date-heading").textContent = new Date(data.date + "T12:00:00")
    .toLocaleDateString(undefined, {
      weekday: "long",
      month: "long",
      day: "numeric",
    })
    .toUpperCase();
  const energy = data.nutrients.find((x) => x.code === "energy");
  $("energy").textContent = fmt(energy?.known);
  $("day-status").textContent =
    data.status === "complete"
      ? "All food logged"
      : data.status === "incomplete"
        ? "Incomplete day"
        : "Completeness unknown";
  $("energy-target").textContent = data.target
    ? "Reviewed target · " + fmt(data.target.energy_kcal) + " kcal"
    : "No reviewed target set";
  $("energy-meter").style.width =
    data.target && energy?.known !== null
      ? Math.min(100, (Number(energy?.known) / data.target.energy_kcal) * 100) +
        "%"
      : "0%";
  $("energy-coverage").textContent = data.item_count
    ? `${data.meal_count} meals · ${energy?.known_items || 0}/${data.item_count} entries with energy data${energy?.missing_items ? " · partial known sum" : ""}`
    : data.status === "complete"
      ? "Explicitly marked complete with no food logged: recorded intake is zero."
      : "No food logged. Intake is unknown, not zero.";
  $("macros").replaceChildren();
  for (const code of ["protein", "carbohydrate", "fat"]) {
    const value = data.nutrients.find((x) => x.code === code);
    const card = node("div", undefined, "macro");
    card.append(
      node(
        "span",
        code === "carbohydrate"
          ? "Carbs"
          : code[0].toUpperCase() + code.slice(1),
      ),
      node(
        "strong",
        fmt(value?.known, 1) + (value?.known !== null ? " g" : ""),
      ),
      node(
        "small",
        value?.total_items
          ? `${value.known_items}/${value.total_items} known${value.missing_items ? " · partial" : ""}`
          : "No entries",
      ),
    );
    $("macros").append(card);
  }
  $("favorites").replaceChildren();
  if (!data.favorites.length)
    $("favorites").append(
      node("p", "Save a meal as a favorite from History.", "empty"),
    );
  for (const favorite of data.favorites) {
    const button = node("button", favorite.name, "favorite");
    button.append(node("small", "＋ Log saved portions"));
    button.onclick = () => {
      if (
        confirm(
          "Log the saved portions of “" +
            favorite.name +
            "”? Estimated portions will need fresh approval in chat.",
        )
      )
        submit({ action: "favorite", reference: favorite.reference });
    };
    $("favorites").append(button);
  }
  renderEnergy(data.energy_history);
  $("weight-chart").replaceChildren();
  if (!data.weight_history.length)
    $("weight-chart").append(
      node(
        "p",
        "No weight measurements recorded yet. Log one in chat.",
        "empty",
      ),
    );
  for (const item of data.weight_history) {
    const dot = node("div", undefined, "weight-dot");
    dot.append(
      node("strong", fmt(item.kg, 1) + " kg"),
      node("small", item.date.slice(5)),
    );
    $("weight-chart").append(dot);
  }
  $("nutrients").replaceChildren();
  for (const value of data.nutrients) {
    const row = node("div", undefined, "nutrient-row");
    const name = node("span", value.name);
    name.append(
      node(
        "small",
        `${value.known_items}/${value.total_items} logged entries with data`,
      ),
    );
    row.append(
      name,
      node(
        "span",
        fmt(value.known, 2) +
          " " +
          value.unit +
          (value.missing_items ? " · partial" : ""),
      ),
    );
    $("nutrients").append(row);
  }
}
function renderEnergy(items) {
  $("energy-chart").replaceChildren();
  const maximum = Math.max(
    1,
    ...items.filter((x) => x.energy !== null).map((x) => Number(x.energy)),
  );
  for (const item of items) {
    const column = node("div", undefined, "chart-column");
    column.title = `${item.date}: ${item.energy === null ? "unknown" : fmt(item.energy) + " recorded kcal"}; ${item.status}${item.partial ? "; partial nutrient data" : ""}`;
    column.append(
      node(
        "small",
        item.energy === null ? "?" : fmt(item.energy),
        "chart-value",
      ),
    );
    const bar = node(
      "div",
      undefined,
      "chart-bar" +
        (item.energy === null ? " unknown" : item.partial ? " partial" : ""),
    );
    if (item.energy !== null)
      bar.style.height =
        Math.max(3, (Number(item.energy) / maximum) * 86) + "px";
    column.append(
      bar,
      node(
        "small",
        new Date(item.date + "T12:00:00").toLocaleDateString(undefined, {
          weekday: "narrow",
        }),
      ),
    );
    $("energy-chart").append(column);
  }
}
function view(id) {
  for (const element of document.querySelectorAll(".view"))
    element.hidden = element.id !== id;
  for (const button of document.querySelectorAll("[data-view]")) {
    if (button.dataset.view === id) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  window.scrollTo({ top: 0, behavior: "instant" });
}
async function loadFoods() {
  try {
    const data = await api(
      "/api/foods?q=" + encodeURIComponent($("food-search").value),
    );
    state.foods = data.foods;
    $("food-results").replaceChildren();
    if (!data.foods.length)
      $("food-results").append(
        node(
          "p",
          "No saved foods match. Import or review foods in chat first.",
          "empty",
        ),
      );
    for (const food of data.foods) {
      const button = node("button", undefined, "list-card");
      button.append(
        node("strong", food.name),
        node(
          "small",
          [food.brand, food.preparation.replaceAll("_", " "), food.source]
            .filter(Boolean)
            .join(" · "),
        ),
      );
      const macros = food.nutrients.filter((x) =>
        ["energy", "protein"].includes(x.code),
      );
      const line = node("div", undefined, "meta");
      line.append(
        node(
          "span",
          macros
            .map((x) => `${fmt(x.per_100g, 1)} ${x.unit} ${x.code}`)
            .join(" · ") + " / 100g",
        ),
        node("span", "＋", "chevron"),
      );
      button.append(line);
      button.onclick = () => selectFood(food);
      $("food-results").append(button);
    }
  } catch (error) {
    notice(error.message, true);
  }
}
async function loadMeals(append = false) {
  try {
    const last = state.meals.at(-1);
    const data = await api(
      "/api/meals" + (append && last ? "?before=" + last.id : ""),
    );
    state.meals = append ? state.meals.concat(data.meals) : data.meals;
    $("meal-results").replaceChildren();
    if (!state.meals.length)
      $("meal-results").append(
        node(
          "p",
          "Your first meal will appear here after you log it.",
          "empty",
        ),
      );
    for (const meal of state.meals) {
      const button = node("button", undefined, "list-card");
      button.append(
        node("strong", meal.label + (meal.deleted ? " · deleted" : "")),
        node(
          "small",
          meal.items
            .map(
              (x) => `${x.grams}g ${x.name}${x.estimated ? " (estimate)" : ""}`,
            )
            .join(" · "),
        ),
      );
      const line = node("div", undefined, "meta");
      line.append(
        node("span", meal.date + " · " + meal.reference),
        node("span", "↗", "chevron"),
      );
      button.append(line);
      button.onclick = () => details(meal);
      $("meal-results").append(button);
    }
    $("more-meals").hidden = data.meals.length < 20;
  } catch (error) {
    notice(error.message, true);
  }
}
function openMeal(meal = null) {
  state.editing = meal;
  state.portions = meal
    ? meal.items.map((x) => ({
        version_id: x.version_id,
        name: x.name,
        grams: x.grams,
      }))
    : [];
  $("form-title").textContent = meal ? "Edit measured portions" : "Log a meal";
  $("form-eyebrow").textContent = meal ? meal.reference : "MEASURED MEAL";
  $("meal-label").value = meal?.label || "Lunch";
  $("meal-day").value = meal?.date || state.dashboard?.date || "";
  $("meal-day").max = state.dashboard?.today || state.dashboard?.date || "";
  $("meal-label").disabled = !!meal;
  $("meal-day").disabled = !!meal;
  $("measured").checked = false;
  renderPortions();
  $("meal-dialog").showModal();
}
function renderPortions() {
  $("portion-rows").replaceChildren();
  for (const [index, item] of state.portions.entries()) {
    const row = node("div", undefined, "portion-row");
    const name = node("div", item.name, "portion-name");
    name.append(node("small", "Saved source #" + item.version_id));
    const label = node("label", "Grams");
    const input = node("input");
    input.type = "number";
    input.min = "0.001";
    input.max = "50000";
    input.step = "0.001";
    input.inputMode = "decimal";
    input.required = true;
    input.value = item.grams;
    input.setAttribute("aria-label", "Measured grams of " + item.name);
    input.oninput = () => (item.grams = input.value);
    label.append(input);
    const remove = node("button", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", "Remove " + item.name);
    remove.onclick = () => {
      state.portions.splice(index, 1);
      renderPortions();
    };
    row.append(name, label, remove);
    $("portion-rows").append(row);
  }
  $("add-food").disabled = state.portions.length >= 10;
  $("save-meal").disabled = !state.portions.length || state.busy;
}
function selectFood(food) {
  if (!state.portions.length && !state.editing) openMeal();
  if (state.portions.length >= 10) {
    notice("A meal can contain up to ten food entries.", true);
    return;
  }
  state.portions.push({
    version_id: food.version_id,
    name: food.name,
    grams: "",
  });
  renderPortions();
  if (!$("meal-dialog").open) $("meal-dialog").showModal();
}
function details(meal) {
  $("detail-title").textContent = meal.label;
  $("detail-body").replaceChildren(
    node("p", meal.date + " · " + meal.reference, "subtle"),
  );
  for (const item of meal.items) {
    const row = node("div", undefined, "detail-item");
    const name = node("span", item.name);
    name.append(
      node(
        "small",
        item.estimated
          ? "Approved estimate · new repeats need approval"
          : "Measured portion",
      ),
    );
    row.append(name, node("strong", item.grams + " g"));
    $("detail-body").append(row);
  }
  const actions = node("div", undefined, "detail-actions");
  function action(label, command, confirmText) {
    const button = node("button", label, "secondary");
    button.onclick = () => {
      if (!confirmText || confirm(confirmText)) {
        $("detail-dialog").close();
        submit(command);
      }
    };
    actions.append(button);
  }
  if (!meal.deleted) {
    const edit = node("button", "Edit portions", "primary");
    edit.onclick = () => {
      $("detail-dialog").close();
      openMeal(meal);
    };
    actions.append(edit);
    action(
      "Repeat today",
      { action: "repeat", reference: meal.reference },
      "Log this meal again today? Estimates need fresh approval in chat.",
    );
    const favorite = node("button", "Save favorite", "secondary");
    favorite.onclick = () => {
      const name = prompt("Name this favorite:");
      if (name) {
        $("detail-dialog").close();
        submit({ action: "save_favorite", reference: meal.reference, name });
      }
    };
    actions.append(favorite);
    action(
      "Delete meal",
      { action: "delete", reference: meal.reference },
      "Delete this meal from current totals? Its revision history is preserved.",
    );
  } else
    action(
      "Undo deletion",
      { action: "undo", reference: meal.reference },
      "Restore the previous revision of this meal?",
    );
  $("detail-body").append(actions);
  $("detail-dialog").showModal();
}
async function submit(command) {
  if (state.busy) return;
  state.busy = true;
  const canonical = JSON.stringify(command);
  const pending = pendingRequest();
  if (pending && state.submission?.canonical !== canonical) {
    state.busy = false;
    notice(
      "A previous request is still unresolved. Refresh and check its receipt before logging again.",
      true,
    );
    return;
  }
  const retrying = state.submission?.canonical === canonical;
  if (!retrying)
    state.submission = { canonical, request_id: crypto.randomUUID() };
  const payload = { ...command, request_id: state.submission.request_id };
  if (!rememberRequest(payload.request_id)) {
    state.busy = false;
    notice(
      "This browser cannot preserve a retry identifier. Open the diary in the Telegram app before saving.",
      true,
    );
    return;
  }
  let accepted = false;
  try {
    $("receipt-text").textContent = "";
    $("review-chat").hidden = true;
    $("receipt-state").textContent = "Saving through your diary…";
    if (!$("receipt-dialog").open) $("receipt-dialog").showModal();
    await api("/api/commands", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    accepted = true;
    await receipt(payload.request_id);
  } catch (error) {
    if (
      !accepted &&
      !retrying &&
      error.status &&
      error.status < 500 &&
      ![401, 403, 429].includes(error.status)
    )
      forgetRequest();
    $("receipt-state").textContent =
      error.message + " Retry the same action to reuse its request identifier.";
    notice(error.message, true);
  } finally {
    state.busy = false;
    renderPortions();
  }
}
async function receipt(id) {
  for (let attempt = 0; attempt < 20; attempt++) {
    const data = await api("/api/requests/" + encodeURIComponent(id));
    if (data.status !== "pending") {
      $("receipt-text").textContent = data.replies
        .map((x) => x.text)
        .join("\n\n");
      $("receipt-state").textContent =
        data.status === "done"
          ? "Processed. The same receipt is delivered to your private chat."
          : "This request could not be applied. Check the receipt and refresh.";
      $("review-chat").hidden = !data.replies.some((x) => x.needs_approval);
      forgetRequest();
      state.submission = null;
      state.portions = [];
      state.editing = null;
      await load();
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  $("receipt-state").textContent =
    "Still queued. Tap Check receipt or Refresh to resume. Check your private chat before repeating this action.";
}
async function recoverPendingReceipt() {
  const pending = pendingRequest();
  if (!pending || !tg?.initData || state.busy) return;
  state.busy = true;
  if (!$("receipt-dialog").open) $("receipt-dialog").showModal();
  $("receipt-state").textContent = "Checking your saved request…";
  try {
    await receipt(pending);
  } catch (error) {
    if (error.status === 404 || error.status === 410) {
      forgetRequest();
      state.submission = null;
    }
    $("receipt-state").textContent = error.message;
    notice(error.message, true);
  } finally {
    state.busy = false;
    renderPortions();
  }
}
async function refresh() {
  await load();
  await recoverPendingReceipt();
}
$("refresh").onclick = refresh;
$("check-receipt").onclick = recoverPendingReceipt;
$("day").onchange = load;
$("log-meal").onclick = () => openMeal();
$("add-food").onclick = () => {
  $("meal-dialog").close();
  view("foods");
};
function abandonMeal() {
  state.editing = null;
  state.portions = [];
}
$("close-dialog").onclick = () => {
  abandonMeal();
  $("meal-dialog").close();
};
$("meal-dialog").addEventListener("cancel", abandonMeal);
$("close-detail").onclick = () => $("detail-dialog").close();
$("close-receipt").onclick = () => $("receipt-dialog").close();
$("review-chat").onclick = () => tg?.close();
$("search-form").onsubmit = (event) => {
  event.preventDefault();
  loadFoods();
};
$("more-meals").onclick = () => loadMeals(true);
for (const button of document.querySelectorAll("[data-view]"))
  button.onclick = () => view(button.dataset.view);
$("meal-form").onsubmit = (event) => {
  event.preventDefault();
  if (!$("measured").checked || !state.portions.length) return;
  const command = {
    action: state.editing ? "edit" : "meal",
    items: state.portions.map((x) => ({
      version_id: x.version_id,
      grams: x.grams,
    })),
  };
  if (state.editing) command.reference = state.editing.reference;
  else {
    command.day = $("meal-day").value;
    command.label = $("meal-label").value;
  }
  $("meal-dialog").close();
  submit(command);
};
tg?.ready();
tg?.expand();
tg?.setHeaderColor?.("#f6f5ee");
tg?.setBackgroundColor?.("#f6f5ee");
refresh();
