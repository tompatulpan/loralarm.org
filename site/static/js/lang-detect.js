/* First-visit language suggestion: if the visitor's browser does not prefer
   Swedish and the current (Swedish-default) page has an English translation,
   offer it by redirecting once. The choice is remembered in a cookie, and
   clicking the SE/EN switcher always overrides it. */
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
      ";path=/;max-age=" + YEAR + ";SameSite=Lax";
  }

  /* Remember the choice when the visitor uses the language switcher. */
  document.querySelectorAll('a[hreflang="sv"], a[hreflang="en"]').forEach(function (a) {
    a.addEventListener("click", function () {
      setCookie(COOKIE, a.getAttribute("hreflang"));
    });
  });

  /* Already decided (earlier visit or this redirect), already on English,
     or a crawler (their renderers run JS with English defaults)? */
  if (getCookie(COOKIE) || document.documentElement.lang.indexOf("en") === 0) return;
  if (/bot|crawl|spider|slurp|baidu|bingpreview/i.test(navigator.userAgent)) return;

  var langs = (navigator.languages && navigator.languages.length)
    ? navigator.languages
    : [navigator.language || ""];
  var wantsSwedish = langs.some(function (l) {
    return (l || "").toLowerCase().indexOf("sv") === 0;
  });
  if (wantsSwedish) return;

  /* Only redirect when this page actually has an English translation
     (the hreflang alternates are rendered by Hugo from .Translations). */
  var en = document.querySelector('link[rel="alternate"][hreflang="en"]');
  if (!en) return;

  setCookie(COOKIE, "en");
  window.location.replace(en.href);
})();
