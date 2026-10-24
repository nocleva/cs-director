// Gemini görselleri geldiyse (scripts/assets_yerlestir.py img/manifest.json yazar) yer tutucuların yerine koyar.
// Görsel yoksa görsel isteği yapılmaz; CSS/SVG yer tutucular kalır.
window.SMAssets = (function () {
  var files = null, waiting = [];
  function url(name) { return "/static/img/" + name; }
  function has(name) { return !!files && files.indexOf(name) >= 0; }
  function onReady(fn) { if (files) fn(); else waiting.push(fn); }
  function apply() {
    if (has("logo.png")) document.querySelectorAll("[data-asset-logo]").forEach(function (el) { var i = new Image(); i.alt = ""; i.src = url("logo.png"); el.replaceChildren(i); });
    document.querySelectorAll("[data-asset-cover]").forEach(function (el) {
      var f = has("giris-kapak.jpg") ? "giris-kapak.jpg" : has("giris-kapak.png") ? "giris-kapak.png" : null;
      if (f) el.style.setProperty("--cover-img", "url(" + url(f) + ")");
    });
  }
  fetch("/static/img/manifest.json").then(function (r) { return r.ok ? r.json() : { files: [] }; }).catch(function () { return { files: [] }; })
    .then(function (m) { files = m.files || []; apply(); waiting.splice(0).forEach(function (f) { f(); }); });
  return { has: has, url: url, onReady: onReady };
})();
