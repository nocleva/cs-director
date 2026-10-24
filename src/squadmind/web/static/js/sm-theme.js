// Tema: kayıtlı seçim, yoksa sistem tercihi, yoksa koyu. Başlıkta eşzamanlı yüklenir (yanıp sönme olmasın).
(function () {
  var KEY = "sm-theme", root = document.documentElement;
  function saved() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  function apply(t) { root.setAttribute("data-theme", t); }
  var q = (location.search.match(/[?&]tema=(light|dark)/) || [])[1];   // ?tema=light: kaydetmeden önizleme
  var s = q || saved();
  apply(s === "light" || s === "dark" ? s : (window.matchMedia && matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"));
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-theme-toggle]");
    if (!b) return;
    var next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
    apply(next); try { localStorage.setItem(KEY, next); } catch (err) {}
  });
})();
