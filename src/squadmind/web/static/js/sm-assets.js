// Görsel seti (scripts/gorsel_uret.py SVG'leri + scripts/assets_yerlestir.py kapak / manifest.json) yer tutucuların yerine geçer.
// Manifestte olmayan dosya için görsel isteği yapılmaz; CSS/SVG yer tutucular kalır (hiçbir şey bozulmaz).
window.SMAssets = (function () {
  var files = null, waiting = [];
  function url(name) { return "/static/img/" + name; }
  function has(name) { return !!files && files.indexOf(name) >= 0; }
  // "rozet-7" gibi bir tabandan var olan ilk dosya adını döndürür (SVG önce, PNG yedek); yoksa null
  function pick(base) { var e = [".svg", ".png"]; for (var i = 0; i < e.length; i++) if (has(base + e[i])) return base + e[i]; return null; }
  function onReady(fn) { if (files) fn(); else waiting.push(fn); }
  function apply() {
    // Giriş kapağı: WebP + JPEG yedeği, mobilde 960 px sürüm; CSS değişkenleri .stage .cover tarafından okunur
    document.querySelectorAll("[data-asset-cover]").forEach(function (el) {
      function set(w) {
        var a = has("giris-kapak-" + w + ".webp"), b = has("giris-kapak-" + w + ".jpg");
        if (!a && !b) return null;
        var u = a && b ? "image-set(url(" + url("giris-kapak-" + w + ".webp") + ') type("image/webp"), url(' + url("giris-kapak-" + w + ".jpg") + ') type("image/jpeg"))'
          : "url(" + url("giris-kapak-" + w + (a ? ".webp" : ".jpg")) + ")";
        return u;
      }
      var big = set(1920), small = set(960) || big;
      if (big) { el.style.setProperty("--cover-img", big); el.style.setProperty("--cover-img-sm", small); el.classList.add("has-cover"); }
    });
  }
  fetch("/static/img/manifest.json").then(function (r) { return r.ok ? r.json() : { files: [] }; }).catch(function () { return { files: [] }; })
    .then(function (m) { files = m.files || []; apply(); waiting.splice(0).forEach(function (f) { f(); }); });
  return { has: has, pick: pick, url: url, onReady: onReady };
})();
