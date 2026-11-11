/* First-visit language suggestion: if the visitor's most preferred supported
   language (sv or en) is English and the current (Swedish-default) page has an
   English translation, offer it by redirecting. Only an explicit click on the
   language switcher is remembered (cookie) and overrides the detection. */
(function () {
  "use strict";

  var COOKIE = "loralarm-lang";
  var YEAR = 60 * 60 * 24 * 365;

  function getCookie(name) {
    var m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : null;
  }

  function setCookie(name, value) {
    document.cookie =
      name + "=" + encodeURIComponent(value) +
      ";path=/;max-age=" + YEAR + ";SameSite=Lax" +
      (location.protocol === "https:" ? ";Secure" : "");
  }

  /* Remember the choice when the visitor uses the language switcher.
     This script runs in <head>, so wait for the DOM. auxclick covers
     middle-click, which opens the link in a new tab without a click event. */
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll('a[hreflang="sv"], a[hreflang="en"]').forEach(function (a) {
      var remember = function () { setCookie(COOKIE, a.getAttribute("hreflang")); };
      a.addEventListener("click", remember);
      a.addEventListener("auxclick", remember);
    });
  });

  /* Already decided, already on English, or a crawler/automated browser
     (their renderers run JS with English defaults)? */
  if (getCookie(COOKIE) || document.documentElement.lang.indexOf("en") === 0) return;
  if (navigator.webdriver) return;
  if (/bot|crawl|spider|slurp|baidu|bingpreview|lighthouse|inspectiontool/i.test(navigator.userAgent)) return;

  /* First supported language in the visitor's preference order wins;
     visitors preferring neither sv nor en get English. */
  var langs = (navigator.languages && navigator.languages.length)
    ? navigator.languages
    : [navigator.language || ""];
  var preferred = "en";
  for (var i = 0; i < langs.length; i++) {
    var code = (langs[i] || "").toLowerCase().slice(0, 2);
    if (code === "sv" || code === "en") { preferred = code; break; }
  }
  if (preferred === "sv") return;

  /* Only redirect when this page actually has an English translation
     (the hreflang alternates are rendered by Hugo from .AllTranslations). */
  var en = document.querySelector('link[rel="alternate"][hreflang="en"]');
  if (!en) return;

  /* Path only (keeps the current host, e.g. when testing a local build);
     query string and hash survive the redirect. */
  var target = new URL(en.href).pathname + location.search + location.hash;
  if (target === location.pathname + location.search + location.hash) return;
  window.location.replace(target);
})();
