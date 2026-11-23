// The admin UI's only script: theme choice, confirm-before-delete, and seeking the player from a transcript line.
// No inline handlers anywhere (the Content-Security-Policy forbids them); everything hangs off data attributes.
(function () {
  "use strict";
  var root = document.documentElement;
  try {
    var saved = localStorage.getItem("icampus-theme");
    if (saved === "light" || saved === "dark") root.setAttribute("data-theme", saved);
  } catch (e) { /* private mode: follow the OS setting */ }

  document.addEventListener("click", function (event) {
    var toggle = event.target.closest("[data-theme-toggle]");
    if (toggle) {
      var dark = root.getAttribute("data-theme")
        ? root.getAttribute("data-theme") === "dark"
        : !window.matchMedia("(prefers-color-scheme: light)").matches;
      var next = dark ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("icampus-theme", next); } catch (e) { /* not kept */ }
      return;
    }
    var line = event.target.closest("[data-seek]");
    if (line) {
      var media = document.querySelector("[data-player]");
      if (media) {
        media.currentTime = parseFloat(line.getAttribute("data-seek")) || 0;
        media.play().catch(function () { /* the browser wants a gesture on the player itself */ });
      }
    }
  });

  document.addEventListener("submit", function (event) {
    var message = event.target.getAttribute("data-confirm");
    if (message && !window.confirm(message)) event.preventDefault();
  });

  // Mark the transcript line being spoken, and keep it in view.
  document.addEventListener("DOMContentLoaded", function () {
  var player = document.querySelector("[data-player]");
  var lines = Array.prototype.slice.call(document.querySelectorAll(".line[data-seek]"));
  if (player && lines.length) {
    var starts = lines.map(function (el) { return parseFloat(el.getAttribute("data-seek")) || 0; });
    var current = -1;
    player.addEventListener("timeupdate", function () {
      var t = player.currentTime, i = -1;
      for (var n = 0; n < starts.length && starts[n] <= t + 0.05; n++) i = n;
      if (i === current) return;
      if (current >= 0) lines[current].classList.remove("is-current");
      if (i >= 0) {
        lines[i].classList.add("is-current");
        lines[i].scrollIntoView({ block: "nearest" });
      }
      current = i;
    });
    var at = parseFloat((location.hash.match(/t=([\d.]+)/) || [])[1]);
    if (at) player.currentTime = at;
  }
  });
})();
