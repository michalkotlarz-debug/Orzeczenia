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
      date: host.getAttribute("data-date") || "",
      src: host.getAttribute("data-src") || "",
      srcKey: id.split(":")[0]
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
        '" data-date="' + esc(it.date) + '" data-src="' + esc(it.src) + '">' +
        '<div class="card-top"><div class="ident">' +
          '<span class="badge">' + esc(it.type).toUpperCase() + '</span>' +
          '<a class="sig" href="' + esc(it.url) + '">' + esc(it.sig) + '</a>' +
          '<span class="src src-' + esc(it.srcKey) + '">' + esc(it.src) + '</span>' +
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

  render();
  bind();
  refreshCount();
  refreshDocCount();
  setInterval(refreshDocCount, 60000);
})();
