// The admin UI's only script. No inline handlers anywhere (the Content-Security-Policy forbids them);
// everything hangs off data attributes:
//   data-theme-set   the system / light / dark control
//   data-confirm     ask before a form is sent
//   data-seek        jump the player to a transcript line or a slide
//   data-reveal      show or hide what was typed in a password field
//   data-art         the picture on the sign-in page
// plus the "more" menus of table rows (details.menu).
(function () {
  "use strict";
  var root = document.documentElement;
  var KEY = "icampus-theme";

  // --- theme: follows the system until one is chosen ---------------------------------------------------------

  function chosen() {
    try {
      var saved = localStorage.getItem(KEY);
      return saved === "light" || saved === "dark" ? saved : "system";
    } catch (e) { return "system"; }  // private mode: follow the system
  }

  function applyTheme(choice) {
    if (choice === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", choice);
    each("[data-theme-set]", function (button) {
      var on = button.getAttribute("data-theme-set") === choice;
      button.classList.toggle("on", on);
      button.setAttribute("aria-pressed", on ? "true" : "false");
    });
    each("[data-art]", drawArt);
  }

  function each(selector, fn) {
    Array.prototype.forEach.call(document.querySelectorAll(selector), fn);
  }

  applyTheme(chosen());  // runs in <head>, before anything is painted

  // --- clicks -------------------------------------------------------------------------------------------------

  document.addEventListener("click", function (event) {
    var target = event.target;
    var theme = target.closest("[data-theme-set]");
    if (theme) {
      var choice = theme.getAttribute("data-theme-set");
      try {
        if (choice === "system") localStorage.removeItem(KEY);
        else localStorage.setItem(KEY, choice);
      } catch (e) { /* not kept */ }
      applyTheme(choice);
      return;
    }
    var reveal = target.closest("[data-reveal]");
    if (reveal) {
      var input = document.getElementById(reveal.getAttribute("data-reveal"));
      if (input) {
        var show = input.type === "password";
        input.type = show ? "text" : "password";
        reveal.setAttribute("aria-pressed", show ? "true" : "false");
      }
      return;
    }
    var seek = target.closest("[data-seek]");
    if (seek) {
      var media = document.querySelector("[data-player]");
      if (media) {
        media.currentTime = parseFloat(seek.getAttribute("data-seek")) || 0;
        media.play().catch(function () { /* the browser wants a gesture on the player itself */ });
      }
    }
    if (!target.closest("details.menu")) closeMenus(null);
  });

  document.addEventListener("submit", function (event) {
    var message = event.target.getAttribute("data-confirm");
    if (message && !window.confirm(message)) event.preventDefault();
  });

  // --- row menus: one open at a time, placed so a scrolling table can't clip them ---------------------------

  function closeMenus(except) {
    each("details.menu[open]", function (menu) { if (menu !== except) menu.removeAttribute("open"); });
  }

  document.addEventListener("toggle", function (event) {
    var menu = event.target;
    if (!menu.matches || !menu.matches("details.menu")) return;
    var list = menu.querySelector(".menu-list");
    if (!menu.open || !list) return;
    closeMenus(menu);
    var at = menu.querySelector("summary").getBoundingClientRect();
    list.classList.add("is-placed");
    var size = list.getBoundingClientRect();
    var below = at.bottom + 6 + size.height <= window.innerHeight - 8;
    list.style.top = Math.max(8, below ? at.bottom + 6 : at.top - 6 - size.height) + "px";
    list.style.left = Math.max(8, Math.min(at.right - size.width, window.innerWidth - size.width - 8)) + "px";
  }, true);

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    each("details.menu[open]", function (menu) {
      menu.removeAttribute("open");
      menu.querySelector("summary").focus();
    });
  });
  window.addEventListener("scroll", function () { closeMenus(null); }, true);
  window.addEventListener("resize", function () {
    closeMenus(null);
    each("[data-art]", drawArt);
  });

  // --- the sign-in picture ------------------------------------------------------------------------------------
  // Ordered dithering on a coarse grid, in the theme's own colours: a voice (the disc), the sound leaving it
  // (the rings), and the lines of text it turns into. Nothing is loaded; the same picture is drawn every time.

  var BAYER = [0, 32, 8, 40, 2, 34, 10, 42, 48, 16, 56, 24, 50, 18, 58, 26, 12, 44, 4, 36, 14, 46, 6, 38,
    60, 28, 52, 20, 62, 30, 54, 22, 3, 35, 11, 43, 1, 33, 9, 41, 51, 19, 59, 27, 49, 17, 57, 25,
    15, 47, 7, 39, 13, 45, 5, 37, 63, 31, 55, 23, 61, 29, 53, 21];

  function token(name) {  // a token's colour as the browser resolves it for the current theme
    var probe = document.createElement("span");
    probe.style.color = "var(" + name + ")";
    document.body.appendChild(probe);
    var colour = getComputedStyle(probe).color;
    document.body.removeChild(probe);
    return colour;
  }

  function dice(seed) {
    return function () {
      seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
      return seed / 4294967296;
    };
  }

  function smooth(a, b, x) {
    var t = Math.min(1, Math.max(0, (x - a) / (b - a)));
    return t * t * (3 - 2 * t);
  }

  function drawArt(canvas) {
    if (!document.body) return;
    var box = canvas.getBoundingClientRect();
    if (box.width < 80 || box.height < 80) return;
    var cell = 4;  // one picture dot is four CSS pixels
    var w = Math.round(box.width / cell), h = Math.round(box.height / cell);
    canvas.width = w;
    canvas.height = h;
    var ctx = canvas.getContext("2d");
    var ink = token("--text"), accent = token("--accent");
    var cx = 0.66 * w, cy = 0.31 * h, R = 0.17 * w;         // the voice, where the page's hairlines cross
    var tx = -(cx / Math.hypot(cx, h - cy)), ty = (h - cy) / Math.hypot(cx, h - cy);  // toward the lower left
    var reach = Math.hypot(cx, h - cy);
    var wave = R * 0.62;
    var random = dice(20240917);

    // Words: runs of dots on text lines, fixed by the seed.
    var lineGap = 7, words = [];
    for (var row = 0; row * lineGap < h; row++) {
      var x = Math.floor(random() * 6), spans = [];
      while (x < w) {
        var len = 3 + Math.floor(random() * 11);
        spans.push([x, x + len]);
        x += len + 2 + Math.floor(random() * 3);
      }
      words.push(spans);
    }

    function inWord(px, py) {
      var r = Math.floor(py / lineGap);
      if (py - r * lineGap > 1 || !words[r]) return false;  // each line is two dots tall
      for (var i = 0; i < words[r].length; i++) if (px >= words[r][i][0] && px < words[r][i][1]) return true;
      return false;
    }

    for (var y = 0; y < h; y++) {
      for (var x2 = 0; x2 < w; x2++) {
        var dx = x2 - cx, dy = y - cy, d = Math.hypot(dx, dy);
        var limit = (BAYER[(y & 7) * 8 + (x2 & 7)] + 0.5) / 64;
        var noise = random();
        if (d <= R) {
          // A ball lit from the upper left: the dither thickens toward the lower right.
          var nx = dx / R, ny = dy / R, nz = Math.sqrt(Math.max(0, 1 - nx * nx - ny * ny));
          var light = Math.max(0, -0.52 * nx - 0.62 * ny + 0.58 * nz);
          if (1.04 - Math.pow(light, 0.82) > limit) { ctx.fillStyle = accent; ctx.fillRect(x2, y, 1, 1); }
          continue;
        }
        var out = d - R;
        var along = (dx * tx + dy * ty) / d;          // 1 straight down the path, -1 straight away from it
        var cone = smooth(0.5, 0.93, along);
        var far = out / (reach - R);                  // 0 at the disc, 1 in the far corner
        var ring = Math.pow(0.5 + 0.5 * Math.cos(out / wave * 2 * Math.PI), 3);
        var sound = cone * ring * (1 - smooth(0.12, 0.6, far)) * 0.95;
        var text = inWord(x2, y) ? smooth(0.7, 0.97, along) * smooth(0.42, 0.7, far) * 1.05 : 0;
        var spray = Math.exp(-out / (R * 0.2)) * 0.09 + cone * Math.exp(-out / (R * 1.2)) * 0.02;
        if (Math.max(sound, text) > limit || noise < spray) { ctx.fillStyle = ink; ctx.fillRect(x2, y, 1, 1); }
      }
    }
  }

  // --- once the page is there ---------------------------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", function () {
    applyTheme(chosen());
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () { each("[data-art]", drawArt); });

    // Mark the transcript line being spoken, and keep it in view inside its own scrolling box.
    var player = document.querySelector("[data-player]");
    var lines = Array.prototype.slice.call(document.querySelectorAll(".line[data-seek]"));
    if (!player || !lines.length) return;
    var starts = lines.map(function (el) { return parseFloat(el.getAttribute("data-seek")) || 0; });
    var current = -1;
    player.addEventListener("timeupdate", function () {
      var t = player.currentTime, i = -1;
      for (var n = 0; n < starts.length && starts[n] <= t + 0.05; n++) i = n;
      if (i === current) return;
      if (current >= 0) lines[current].classList.remove("is-current");
      if (i >= 0) {
        var line = lines[i], pane = line.parentNode;
        line.classList.add("is-current");
        var top = line.offsetTop - pane.offsetTop;
        if (top < pane.scrollTop || top + line.offsetHeight > pane.scrollTop + pane.clientHeight) {
          pane.scrollTop = Math.max(0, top - pane.clientHeight / 3);
        }
      }
      current = i;
    });
    var at = parseFloat((location.hash.match(/t=([\d.]+)/) || [])[1]);
    if (at) player.currentTime = at;
  });
})();
