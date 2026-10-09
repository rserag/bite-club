"use strict";
const tg = window.Telegram?.WebApp;
function syncTheme() {
  document.documentElement.dataset.theme =
    tg?.colorScheme === "dark" ? "dark" : "light";
  const background = getComputedStyle(document.documentElement)
    .getPropertyValue("--background")
    .trim();
  if (tg?.isVersionAtLeast?.("6.1")) {
    tg.setHeaderColor?.(tg.isVersionAtLeast("6.9") ? background : "bg_color");
    tg.setBackgroundColor?.(background);
  }
  if (tg?.isVersionAtLeast?.("7.10")) tg.setBottomBarColor?.(background);
}
syncTheme();
tg?.onEvent?.("themeChanged", syncTheme);
const $ = (id) => document.getElementById(id);
const state = {
  dashboard: null,
  foods: [],
  meals: [],
  portions: [],
  editing: null,
  busy: false,
  submission: null,
  sourcePreview: null,
  sourceBusy: false,
  recipeMeal: null,
  detachRecipe: false,
  labelHandoff: false,
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
function handoffCommand() {
  return {action: "label_handoff", day: $("meal-day").value,
    label: $("meal-label").value, items: selectedPortions()};
}
function selectedPortions() {
  return state.portions.map((x) => x.source_id ?
    {provider: x.provider || "usda", source_id: x.source_id, hash: x.hash, preparation: x.preparation, grams: x.grams} :
    {version_id: x.version_id, grams: x.grams});
}
function forgetHandoff() {
  try { sessionStorage.removeItem("biteclub-label-handoff"); } catch { /* Keep the open form. */ }
}
function rememberHandoff(command, requestId) {
  try {
    sessionStorage.setItem("biteclub-label-handoff", JSON.stringify({
      created: Date.now(), request_id: requestId, day: command.day,
      label: command.label, items: command.items,
    }));
    return true;
  } catch { return false; }
}
function restoreHandoff() {
  try {
    const raw = sessionStorage.getItem("biteclub-label-handoff");
    if (!raw) return;
    if (raw.length > 6000) throw new Error("Invalid handoff draft.");
    const draft = JSON.parse(raw);
    if (Object.keys(draft).sort().join() !== "created,day,items,label,request_id" ||
        !Number.isSafeInteger(draft.created) || draft.created > Date.now() ||
        Date.now() - draft.created > 6 * 60 * 60 * 1000 ||
        typeof draft.request_id !== "string" ||
        !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(draft.request_id) ||
        typeof draft.day !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(draft.day) ||
        !["Breakfast", "Lunch", "Dinner", "Snack", "Meal"].includes(draft.label) ||
        !Array.isArray(draft.items) || draft.items.length > 9) throw new Error("Invalid handoff draft.");
    for (const item of draft.items) {
      if (typeof item.grams !== "string" || !/^[0-9]{1,5}(?:\.[0-9]{1,3})?$/.test(item.grams) || !(Number(item.grams) > 0 && Number(item.grams) <= 50000))
        throw new Error("Invalid handoff portion.");
      if (item.source_id) {
        if (Object.keys(item).sort().join() !== "grams,hash,preparation,provider,source_id" ||
            !["usda", "openfoodfacts"].includes(item.provider) || typeof item.source_id !== "string" || !/^[0-9]{1,14}$/.test(item.source_id) ||
            typeof item.hash !== "string" || !/^[0-9a-f]{64}$/.test(item.hash) || !["raw", "cooked", "as_sold", "as_prepared"].includes(item.preparation))
          throw new Error("Invalid handoff source.");
      } else if (Object.keys(item).sort().join() !== "grams,version_id" ||
          !Number.isSafeInteger(item.version_id) || item.version_id <= 0) throw new Error("Invalid handoff food.");
    }
    state.portions = draft.items.map((item) => ({...item, name: item.source_id ?
      `Reviewed ${item.provider === "usda" ? "USDA" : "Open Food Facts"} source ${item.source_id}` :
      state.foods.find((food) => food.version_id === item.version_id)?.name || `Saved source #${item.version_id}`}));
    state.editing = null;
    state.detachRecipe = false;
    state.labelHandoff = true;
    $("meal-day").value = draft.day;
    $("meal-label").value = draft.label;
    if (pendingRequest() === draft.request_id) state.submission = {
      request_id: draft.request_id, canonical: JSON.stringify(handoffCommand()), preserveMeal: true,
    };
    showLabelHandoff(!pendingRequest());
    notice("Your label handoff is restored with its original food versions, source hashes, portions and date.");
  } catch {
    forgetHandoff();
    notice("The saved label handoff expired or is invalid. Check your private chat before starting again.", true);
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
    return true;
  } catch (error) {
    notice(error.message, true);
    return false;
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
      "/api/foods?q=" + encodeURIComponent($("food-search").value) +
        "&preparation=" + encodeURIComponent($("food-preparation").value),
    );
    state.foods = data.foods;
    $("food-results").replaceChildren();
    if (!data.foods.length)
      $("food-results").append(
        node(
          "p",
          "No saved foods match. Find in USDA to review a new source, or enter a label in chat.",
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
function sourceStatus(status) {
  return {
    not_configured: "Source lookup is not configured. Your saved foods are still available.",
    timeout: "The food source took too long. Try again later; your meal has not been saved.",
    rate_limited: "The food source is busy. Wait briefly before trying again.",
    unsupported_food: "This source needs another reviewed food record or manual source entry.",
    not_found: "That source is unavailable. Search for another food record.",
    invalid_response: "That source could not be read reliably. Choose another record.",
  }[status] || "No usable source was returned. Try another name or preparation.";
}
async function findSources() {
  const query = $("food-search").value.trim();
  if (!query) return notice("Enter a food name before searching sources.", true);
  if (state.sourceBusy) return;
  state.sourceBusy = true;
  $("find-source").disabled = true;
  $("source-results").replaceChildren(node("p", "Searching food sources…", "subtle"));
  try {
    const data = await api("/api/food-sources?q=" + encodeURIComponent(query) +
      "&preparation=" + encodeURIComponent($("food-preparation").value));
    $("source-results").replaceChildren(node("h2", "Review a new source"));
    if (!data.remote.length) $("source-results").append(node("p", sourceStatus(data.source_status), "empty"));
    for (const food of data.remote) {
      const button = node("button", undefined, "list-card");
      button.append(node("strong", food.name), node("small",
        [food.data_type, food.preparation_hint.replaceAll("_", " ")].join(" · ")));
      button.onclick = () => reviewSource(food.source_id, food.provider || "usda");
      $("source-results").append(button);
    }
  } catch (error) {
    $("source-results").replaceChildren(node("p", error.message, "empty"));
  } finally {
    state.sourceBusy = false;
    $("find-source").disabled = false;
  }
}
async function reviewSource(sourceId, provider = "usda") {
  if (state.sourceBusy) return;
  state.sourceBusy = true;
  try {
    notice("Loading the full source for review…");
    const data = await api("/api/food-sources/" + encodeURIComponent(sourceId) +
      "?provider=" + encodeURIComponent(provider));
    if (!data.document || !data.content_sha256) throw new Error(sourceStatus(data.source_status));
    state.sourcePreview = data;
    const record = data.document.record;
    $("source-body").replaceChildren(node("h3", record.name),
      node("p", record.source_reference, "subtle"),
      node("p", "Nutrient values per 100 g of edible food. Original source basis: " + record.basis_grams + " g."));
    if (record.source_url?.startsWith("https://")) {
      const link = node("a", "Open the source record", "source-link");
      link.href = record.source_url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      $("source-body").append(link);
    }
    for (const item of data.nutrients) $("source-body").append(
      node("p", item.code.replaceAll("_", " ") + ": " +
        (item.per_100g === null ? "unknown" : fmt(item.per_100g, 3) + " " + item.unit)));
    $("source-body").append(node("p", "Unlisted nutrients remain unknown. Source averages can differ from the food you ate.", "subtle"));
    for (const warning of data.document.warnings) $("source-body").append(node("p", warning, "subtle"));
    const knownPreparation = record.preparation !== "unspecified";
    $("source-preparation").value = knownPreparation ? record.preparation : $("food-preparation").value;
    $("source-preparation").disabled = knownPreparation;
    $("source-reviewed").checked = false;
    $("source-dialog").showModal();
    notice("Review the full food and preparation before using this source.");
  } catch (error) {
    notice(error.message, true);
  } finally {
    state.sourceBusy = false;
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
          meal.recipes?.length ? meal.recipes.map((recipe) =>
            `${recipe.amount} ${recipe.unit === "g" ? "g cooked" : "serving(s)"} ${recipe.name}`
          ).join(" · ") : meal.items.map((x) =>
            `${x.grams}g ${x.name}${x.estimated ? " (estimate)" : ""}`
          ).join(" · "),
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
function openMeal(meal = null, detachRecipe = false) {
  if (meal?.recipes?.length && !detachRecipe) return openRecipe(meal);
  state.editing = meal;
  state.detachRecipe = detachRecipe;
  state.labelHandoff = false;
  $("label-handoff-help").hidden = true;
  $("save-meal").textContent = "Save measured meal";
  $("measured").required = true;
  $("measured").closest("label").hidden = false;
  state.portions = meal && !detachRecipe
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
function showLabelHandoff(open = true) {
  $("form-title").textContent = "Continue with a nutrition label";
  $("form-eyebrow").textContent = "CONTINUE IN CHAT";
  $("label-handoff-help").hidden = false;
  $("meal-label").disabled = false;
  $("meal-day").disabled = false;
  $("measured").required = state.portions.length > 0;
  $("measured").closest("label").hidden = !state.portions.length;
  $("save-meal").textContent = "Continue with a label in chat";
  renderPortions();
  if (open && !$("meal-dialog").open) $("meal-dialog").showModal();
}
function renderPortions() {
  $("portion-rows").replaceChildren();
  for (const [index, item] of state.portions.entries()) {
    const row = node("div", undefined, "portion-row");
    const name = node("div", item.name, "portion-name");
    name.append(node("small", item.source_id ? "Reviewed new source · " + item.preparation.replaceAll("_", " ") : "Saved source #" + item.version_id));
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
  $("save-meal").disabled = (!state.portions.length && !state.labelHandoff) || state.busy;
  if (state.labelHandoff) {
    $("measured").required = state.portions.length > 0;
    $("measured").closest("label").hidden = !state.portions.length;
  }
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
function openRecipe(meal) {
  if (!meal.recipe_editable) return notice("This meal contains multiple recipe groups. Edit it from its receipt in chat.", true);
  state.recipeMeal = meal;
  const recipe = meal.recipes[0];
  $("recipe-summary").replaceChildren(node("h3", recipe.name),
    node("p", recipe.reference + " · " + recipe.fraction + " of the original batch", "subtle"),
    node("p", "The original ingredients and recipe version stay attached to this meal."));
  $("recipe-amount-label").firstChild.textContent = recipe.unit === "g" ? "Cooked portion (g)" : "Equal servings";
  $("recipe-amount").value = recipe.amount;
  $("recipe-amount").max = recipe.maximum;
  $("recipe-estimated").checked = recipe.estimated;
  $("recipe-dialog").showModal();
}
function details(meal) {
  $("detail-title").textContent = meal.label;
  $("detail-body").replaceChildren(
    node("p", meal.date + " · " + meal.reference, "subtle"),
  );
  for (const recipe of meal.recipes || []) $("detail-body").append(node("p",
    recipe.name + " · " + recipe.amount + (recipe.unit === "g" ? " g cooked" : " serving(s)") +
    " · " + recipe.fraction + " of batch · " + recipe.reference, "recipe-summary"));
  if (meal.recipes?.length && !meal.recipe_editable) $("detail-body").append(node("p",
    "Use the current receipt in chat for this meal. This form edits one complete recipe portion.", "subtle"));
  for (const item of meal.items) {
    const row = node("div", undefined, "detail-item");
    const name = node("span", item.name);
    name.append(
      node(
        "small",
        item.estimated
          ? "Approved estimate · new repeats need approval"
          : item.recipe_ingredient ? "Calculated ingredient equivalent" : "Measured portion",
      ),
    );
    row.append(name, node("strong", item.quantity_text || item.grams + " g"));
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
    const edit = node("button", meal.recipes?.length ? "Edit recipe portion" : "Edit portions", "primary");
    edit.disabled = !!meal.recipes?.length && !meal.recipe_editable;
    edit.onclick = () => {
      $("detail-dialog").close();
      openMeal(meal);
    };
    actions.append(edit);
    if (meal.recipes?.length) {
      const detach = node("button", "Replace recipe with measured foods", "secondary");
      detach.onclick = () => {
        if (!confirm("Replace this recipe portion with foods you measured directly? Choose all replacement foods and amounts; the recipe portion will be removed from this meal revision.")) return;
        $("detail-dialog").close();
        openMeal(meal, true);
      };
      actions.append(detach);
    }
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
    state.submission = { canonical, request_id: crypto.randomUUID(), preserveMeal: command.action === "source_add" };
  const payload = { ...command, request_id: state.submission.request_id };
  if (command.action === "label_handoff") {
    state.submission.preserveMeal = true;
    if (!rememberHandoff(command, payload.request_id)) {
      state.busy = false;
      notice("This browser cannot preserve the label handoff. Your open meal is retained; keep this window open and retry.", true);
      showLabelHandoff();
      return;
    }
  }
  if (!rememberRequest(payload.request_id)) {
    state.busy = false;
    notice(
      "This browser cannot preserve a retry identifier. Open the diary in the Telegram app before saving.",
      true,
    );
    if (state.labelHandoff) showLabelHandoff();
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
      const handoffAccepted = data.replies.some((x) => x.continue_in_chat);
      if (handoffAccepted) {
        forgetHandoff();
        state.labelHandoff = false;
        $("review-chat").hidden = false;
        $("review-chat").textContent = "Continue the label in chat";
        $("receipt-state").textContent = "Your meal context is saved in the label guide. Continue in your private chat.";
      } else $("review-chat").textContent = "Review the draft in chat";
      forgetRequest();
      const preserveMeal = !handoffAccepted && (state.labelHandoff || state.submission?.preserveMeal);
      state.submission = null;
      if (!preserveMeal) {
        state.portions = [];
        state.editing = null;
        state.detachRecipe = false;
        state.recipeMeal = null;
      }
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
  if (await load()) restoreHandoff();
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
  if (state.labelHandoff && pendingRequest()) {
    notice("The label handoff is still pending. Check its receipt before starting another meal.", true);
    return;
  }
  forgetHandoff();
  state.labelHandoff = false;
  state.editing = null;
  state.portions = [];
  state.detachRecipe = false;
}
$("close-dialog").onclick = () => {
  abandonMeal();
  $("meal-dialog").close();
};
$("meal-dialog").addEventListener("cancel", abandonMeal);
$("close-detail").onclick = () => $("detail-dialog").close();
$("close-receipt").onclick = () => {
  $("receipt-dialog").close();
  if (state.labelHandoff) showLabelHandoff();
};
$("review-chat").onclick = () => tg?.close();
$("search-form").onsubmit = (event) => {
  event.preventDefault();
  loadFoods();
  $("source-results").replaceChildren();
};
$("food-preparation").onchange = () => { loadFoods(); $("source-results").replaceChildren(); };
$("find-source").onclick = findSources;
$("close-source").onclick = () => $("source-dialog").close();
$("source-preparation").onchange = () => { $("source-reviewed").checked = false; };
$("save-source").onclick = () => {
  if (!$("source-form").reportValidity() || !state.sourcePreview) return;
  const preview = state.sourcePreview;
  const source = {provider: preview.document.provider || "usda", source_id: preview.document.source_id,
    hash: preview.content_sha256, preparation: $("source-preparation").value};
  $("source-dialog").close();
  submit({action: "source_add", source});
};
$("source-form").onsubmit = (event) => {
  event.preventDefault();
  const preview = state.sourcePreview;
  const preparation = $("source-preparation").value;
  if (!preview || !preparation || !$("source-reviewed").checked) return;
  if (state.editing) return notice("Save or close your current edit before adding a new source.", true);
  if (state.portions.length >= 10) return notice("A meal can contain up to ten food entries.", true);
  $("source-dialog").close();
  if (!state.portions.length) openMeal();
  state.portions.push({provider: preview.document.provider || "usda", source_id: preview.document.source_id, hash: preview.content_sha256,
    preparation, name: preview.document.record.name, grams: ""});
  renderPortions();
  if (!$("meal-dialog").open) $("meal-dialog").showModal();
};
$("close-recipe").onclick = () => { state.recipeMeal = null; $("recipe-dialog").close(); };
$("barcode-form").onsubmit = (event) => {
  event.preventDefault();
  reviewSource($("food-barcode").value.trim(), "openfoodfacts");
};
$("label-chat").onclick = () => {
  if (pendingRequest()) return recoverPendingReceipt();
  if (state.editing) return notice("Finish or close your current edit before continuing a new meal in chat.", true);
  if (state.portions.length > 9) return notice("A meal can contain ten foods. Remove one entry to leave room for the label food.", true);
  if (!state.portions.length) openMeal();
  state.labelHandoff = true;
  $("measured").checked = false;
  showLabelHandoff();
};
$("recipe-dialog").addEventListener("cancel", () => { state.recipeMeal = null; });
$("recipe-form").onsubmit = (event) => {
  event.preventDefault();
  if (!state.recipeMeal) return;
  const recipe = state.recipeMeal.recipes[0];
  const command = { action: "recipe_portion", reference: state.recipeMeal.reference,
    portion: $("recipe-amount").value, unit: recipe.unit, estimated: $("recipe-estimated").checked };
  $("recipe-dialog").close();
  submit(command);
};
$("more-meals").onclick = () => loadMeals(true);
for (const button of document.querySelectorAll("[data-view]"))
  button.onclick = () => view(button.dataset.view);
$("meal-form").onsubmit = (event) => {
  event.preventDefault();
  if (state.labelHandoff) {
    if (state.portions.length && !$("measured").checked) return;
    const command = handoffCommand();
    $("meal-dialog").close();
    submit(command);
    return;
  }
  if (!$("measured").checked || !state.portions.length) return;
  const command = {
    action: state.editing ? "edit" : state.portions.some((x) => x.source_id) ? "source_meal" : "meal",
    items: selectedPortions(),
  };
  if (state.editing) { command.reference = state.editing.reference; command.detach_recipe = state.detachRecipe; }
  else {
    command.day = $("meal-day").value;
    command.label = $("meal-label").value;
  }
  $("meal-dialog").close();
  submit(command);
};
tg?.ready();
tg?.expand();
refresh();
