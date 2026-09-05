/* Ulubione i rozwijanie filtrów. Wszystko po stronie przeglądarki —
   serwis nie ma bazy, więc lista ulubionych żyje wyłącznie w localStorage. */
(function () {
  "use strict";
  var KEY = "orzecznik:ulubione";

  function read() {
    try {
      var v = JSON.parse(localStorage.getItem(KEY));
      return Array.isArray(v) ? v : [];
    } catch (e) { return []; }
  }
  function write(items) {
    try { localStorage.setItem(KEY, JSON.stringify(items)); } catch (e) { /* tryb prywatny */ }
  }
  function indexOf(items, id) {
    for (var i = 0; i < items.length; i++) { if (items[i].id === id) return i; }
    return -1;
  }
  function refreshCount() {
    var n = read().length;
    document.querySelectorAll("[data-fav-count]").forEach(function (el) {
      el.textContent = n;
      el.hidden = n === 0;
    });
  }
  function paint(btn, on) {
    btn.classList.toggle("on", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    btn.title = on ? "Usuń z ulubionych" : "Dodaj do ulubionych";
    var lbl = btn.querySelector(".lbl");
    if (lbl) lbl.textContent = on ? "W ulubionych" : "Do ulubionych";
  }
  /* Zapisujemy garść danych z karty, żeby strona "Ulubione" mogła je pokazać
     bez odpytywania portali źródłowych. */
  function snapshot(host) {
    var id = host.getAttribute("data-doc");
    return {
      id: id,
      url: "/orzeczenie/" + id.replace(":", "/"),
      sig: host.getAttribute("data-sig") || "(bez sygnatury)",
      type: host.getAttribute("data-type") || "orzeczenie",
      court: host.getAttribute("data-court") || "",
      date: host.getAttribute("data-date") || ""
    };
  }

  function bind(root) {
    var items = read();
    (root || document).querySelectorAll("[data-fav]").forEach(function (btn) {
      var host = btn.closest("[data-doc]");
      if (!host) return;
      var id = host.getAttribute("data-doc");
      paint(btn, indexOf(items, id) !== -1);
      if (btn.dataset.bound) return;
      btn.dataset.bound = "1";
      btn.addEventListener("click", function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        var cur = read();
        var i = indexOf(cur, id);
        if (i === -1) { cur.push(snapshot(host)); } else { cur.splice(i, 1); }
        write(cur);
        paint(btn, i === -1);
        refreshCount();
        if (i !== -1 && document.querySelector("[data-fav-list]")) render();
      });
    });
  }

  var MONTHS = ["stycznia","lutego","marca","kwietnia","maja","czerwca",
                "lipca","sierpnia","września","października","listopada","grudnia"];
  function datePl(v) {
    if (!v) return "—";
    var p = v.split("-");
    if (p.length !== 3) return v;
    return parseInt(p[2], 10) + " " + MONTHS[parseInt(p[1], 10) - 1] + " " + p[0];
  }
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function render() {
    var list = document.querySelector("[data-fav-list]");
    if (!list) return;
    var items = read();
    if (!items.length) {
      list.innerHTML = '<div class="empty"><p><b>Nic tu jeszcze nie ma</b></p>' +
        '<p>Kliknij serduszko przy orzeczeniu, żeby zapisać je na później.</p></div>';
      return;
    }
    list.innerHTML = items.map(function (it) {
      return '' +
      '<article class="card" data-doc="' + esc(it.id) + '" data-sig="' + esc(it.sig) +
        '" data-type="' + esc(it.type) + '" data-court="' + esc(it.court) +
        '" data-date="' + esc(it.date) + '">' +
        '<div class="card-top"><div class="ident">' +
          '<span class="badge">' + esc(it.type).toUpperCase() + '</span>' +
          '<a class="sig" href="' + esc(it.url) + '">' + esc(it.sig) + '</a>' +
        '</div><div class="tools">' +
          '<a class="tool" href="' + esc(it.url) + '/pobierz.txt" title="Pobierz treść">&#10515;</a>' +
          '<button class="tool fav" type="button" data-fav aria-pressed="true">&#9829;</button>' +
        '</div></div>' +
        '<a class="card-link" href="' + esc(it.url) + '"><div class="meta">' +
          '<span>' + esc(it.court || "—") + '</span>' +
          '<span>' + esc(datePl(it.date)) + '</span>' +
        '</div></a>' +
      '</article>';
    }).join("");
    bind(list);
  }

  /* Autouzupełnianie pól filtra (sędzia/hasło tematyczne/podstawa prawna) na
     podstawie wartości już obecnych w bazie - użytkownik trafia w istniejącą
     pisownię zamiast zgadywać. Debounce, żeby nie odpytywać przy każdym znaku. */
  var suggestTimers = {};
  document.querySelectorAll("[data-suggest]").forEach(function (input) {
    var field = input.getAttribute("data-suggest");
    var list = document.getElementById("dl-" + field);
    if (!list) return;
    input.addEventListener("input", function () {
      var q = input.value.trim();
      clearTimeout(suggestTimers[field]);
      if (q.length < 2) { list.innerHTML = ""; return; }
      suggestTimers[field] = setTimeout(function () {
        fetch("/api/podpowiedzi?pole=" + encodeURIComponent(field) + "&q=" + encodeURIComponent(q))
          .then(function (r) { return r.json(); })
          .then(function (d) {
            list.innerHTML = (d.results || []).map(function (v) {
              return '<option value="' + v.replace(/"/g, "&quot;") + '">';
            }).join("");
          }).catch(function () { /* brak podpowiedzi to nie błąd */ });
      }, 250);
    });
  });

  /* Przełącznik jasny/ciemny - domyślnie idziemy za motywem systemu
     (prefers-color-scheme w CSS), ten przycisk pozwala wymusić wybór. */
  var THEME_KEY = "orzecznik:motyw";
  function systemPrefersDark() {
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  }
  function applyTheme(mode) {
    if (mode === "light" || mode === "dark") {
      document.documentElement.setAttribute("data-theme", mode);
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
  }
  document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var current = document.documentElement.getAttribute("data-theme") ||
        (systemPrefersDark() ? "dark" : "light");
      var next = current === "dark" ? "light" : "dark";
      applyTheme(next);
      try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* tryb prywatny */ }
    });
  });

  document.querySelectorAll("[data-filters-toggle]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var panel = btn.parentElement.querySelector("[data-filters]");
      if (!panel) return;
      var open = panel.hasAttribute("hidden");
      if (open) { panel.removeAttribute("hidden"); } else { panel.setAttribute("hidden", ""); }
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    });
  });

  /* Licznik pobranych orzeczeń na stronie głównej - dogrywany z /api/health,
     żeby rósł bez przeładowania strony w miarę jak obserwator dokłada nowe. */
  function refreshDocCount() {
    var els = document.querySelectorAll("[data-doc-count]");
    if (!els.length) return;
    fetch("/api/health").then(function (r) { return r.json(); }).then(function (d) {
      var baza = d && d.baza;
      if (!baza) return;
      var total = 0;
      for (var k in baza) { if (Object.prototype.hasOwnProperty.call(baza, k)) total += baza[k]; }
      var formatted = total.toLocaleString("pl-PL").replace(/ /g, " ");
      els.forEach(function (el) {
        var b = el.querySelector("b");
        if (b) b.textContent = formatted;
      });
    }).catch(function () { /* strona ma już wartość wyrenderowaną na serwerze */ });
  }

  /* Ostatnie wyszukiwania - podpowiedzi we wciaz pustym polu frazy, osobno
     dla orzeczen i aktow prawnych (nie mieszamy dwoch roznych domen). */
  var RECENT_MAX = 5;
  function recentKey(scope) { return "orzecznik:ostatnie:" + scope; }
  function readRecent(scope) {
    try {
      var v = JSON.parse(localStorage.getItem(recentKey(scope)));
      return Array.isArray(v) ? v : [];
    } catch (e) { return []; }
  }
  function saveRecent(scope, q) {
    q = (q || "").trim();
    if (!q) return;
    try {
      var items = readRecent(scope).filter(function (x) { return x !== q; });
      items.unshift(q);
      localStorage.setItem(recentKey(scope), JSON.stringify(items.slice(0, RECENT_MAX)));
    } catch (e) { /* tryb prywatny */ }
  }
  document.querySelectorAll("input[data-recent]").forEach(function (input) {
    var scope = input.getAttribute("data-recent");
    var list = document.getElementById(input.getAttribute("list"));
    if (list) {
      list.innerHTML = readRecent(scope).map(function (v) {
        return '<option value="' + v.replace(/"/g, "&quot;") + '">';
      }).join("");
    }
    var form = input.closest("form");
    if (form) {
      form.addEventListener("submit", function () { saveRecent(scope, input.value); });
    }
  });

  render();
  bind();
  refreshCount();
  refreshDocCount();
  setInterval(refreshDocCount, 60000);
})();
