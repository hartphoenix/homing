const STORAGE_KEY = "homingLocalState";
const listings = window.SUBLET_LISTINGS;

function loadState() {
  try {
    return { interested: {}, trashed: {}, ...JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}") };
  } catch (_) {
    return { interested: {}, trashed: {} };
  }
}

let state = loadState();
let showingTrash = false;

function saveState() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
}

function fact(text) {
  const span = document.createElement("span");
  span.className = "fact";
  span.textContent = text;
  return span;
}

function makeCard(listing, inTrash = false) {
  const card = document.querySelector("#listing-template").content.firstElementChild.cloneNode(true);
  card.dataset.id = listing.id;
  card.querySelector(".listing-title").textContent = listing.title;
  card.querySelector(".location").textContent = listing.location;
  card.querySelector(".summary-text").textContent = listing.summary;

  const badge = card.querySelector(".fit-badge");
  badge.className = `fit-badge ${listing.dateFit}`;
  badge.textContent = listing.dateFit === "strong" ? "STRONG DATE FIT" : "DATES TO VERIFY";

  const facts = card.querySelector(".facts");
  [listing.price, listing.dates, listing.type === "entire" ? "Entire place" : "Shared", listing.source, `Park: ${listing.parks}`]
    .forEach(item => facts.appendChild(fact(item)));

  const callouts = card.querySelector(".callouts");
  listing.unknowns.forEach(text => {
    const p = document.createElement("p");
    p.className = "callout";
    p.textContent = `Verify: ${text}`;
    callouts.appendChild(p);
  });

  const link = card.querySelector(".listing-link");
  link.href = listing.url;
  link.textContent = `Open on ${listing.source} ↗`;

  const checkbox = card.querySelector(".interest-checkbox");
  checkbox.checked = Boolean(state.interested[listing.id]);
  card.classList.toggle("interested-card", checkbox.checked);
  checkbox.addEventListener("change", () => {
    state.interested[listing.id] = checkbox.checked;
    card.classList.toggle("interested-card", checkbox.checked);
    saveState();
    updateCounts();
  });

  const deleteButton = card.querySelector(".delete-button");
  if (inTrash) {
    deleteButton.textContent = "Restore";
    deleteButton.setAttribute("aria-label", "Restore listing from trash");
    deleteButton.addEventListener("click", () => {
      delete state.trashed[listing.id];
      saveState();
      render();
    });
  } else {
    deleteButton.addEventListener("click", () => {
      state.trashed[listing.id] = true;
      saveState();
      render();
    });
  }
  return card;
}

function matchesFilters(listing) {
  const query = document.querySelector("#search").value.trim().toLowerCase();
  const fit = document.querySelector("#fit-filter").value;
  const type = document.querySelector("#type-filter").value;
  const interestedOnly = document.querySelector("#interested-only").checked;
  const haystack = [listing.title, listing.location, listing.summary, listing.source, listing.price, ...listing.unknowns].join(" ").toLowerCase();
  return (!query || haystack.includes(query)) && (fit === "all" || listing.dateFit === fit) &&
    (type === "all" || listing.type === type) && (!interestedOnly || state.interested[listing.id]);
}

function updateCounts() {
  const active = listings.filter(item => !state.trashed[item.id]).length;
  const interested = listings.filter(item => !state.trashed[item.id] && state.interested[item.id]).length;
  const trash = listings.length - active;
  document.querySelector("#active-count").textContent = active;
  document.querySelector("#interested-count").textContent = interested;
  document.querySelector("#trash-count").textContent = trash;
}

function render() {
  const grid = document.querySelector("#listing-grid");
  const trashGrid = document.querySelector("#trash-grid");
  grid.innerHTML = "";
  trashGrid.innerHTML = "";

  const visible = listings.filter(item => !state.trashed[item.id] && matchesFilters(item));
  visible.forEach(item => grid.appendChild(makeCard(item)));
  listings.filter(item => state.trashed[item.id]).forEach(item => trashGrid.appendChild(makeCard(item, true)));

  document.querySelector("#result-note").textContent = `${visible.length} lead${visible.length === 1 ? "" : "s"} shown`;
  document.querySelector("#empty-trash").hidden = trashGrid.children.length > 0;
  document.querySelector("#listing-grid").hidden = showingTrash;
  document.querySelector("#result-note").hidden = showingTrash;
  document.querySelector("#trash-panel").hidden = !showingTrash;
  document.querySelector("#trash-toggle").textContent = `View trash (${trashGrid.children.length})`;
  updateCounts();
}

["#search", "#fit-filter", "#type-filter", "#interested-only"].forEach(selector => {
  document.querySelector(selector).addEventListener("input", render);
});
document.querySelector("#trash-toggle").addEventListener("click", () => { showingTrash = true; render(); });
document.querySelector("#close-trash").addEventListener("click", () => { showingTrash = false; render(); });

render();
