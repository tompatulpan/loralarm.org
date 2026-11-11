// Hero-fade: hero-blocket på startsidan är sticky ("overlay") – det står
// still medan resten av sidan glider upp över det. Det här skriptet låter
// hero:n dessutom tona ut och halka efter något (parallax) under de första
// scrollpikslarna, så att innehållet nedanför snabbt får platsen.
// Progressivt: utan hero-element eller på små skärmar (där hero:n inte är
// sticky, se layout.css) görs ingenting. Vid prefers-reduced-motion sätts bara
// sidhuvudets höjd; ingen tonad eller parallax.
(function () {
  "use strict";
  var hero = document.querySelector("header.hero");
  if (!hero || !window.matchMedia) return;

  // Måste matcha media query för sticky-hero i layout.css
  var sticky = window.matchMedia("(min-width: 861px) and (min-height: 640px)");
  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
  var siteHeader = document.querySelector("header.site-header");

  var FADE_DISTANCE = 420; // px scroll innan hero är helt uttonad
  var LAG_FACTOR = 0.3;    // hur mycket hero:n halkar efter (0 = ingen parallax)
  var LAG_MAX = 96;        // tak för parallaxförflyttningen i px

  function reset() {
    hero.style.opacity = "";
    hero.style.transform = "";
    hero.style.visibility = "";
  }

  var ticking = false;
  function update() {
    ticking = false;
    if (siteHeader) {
      document.documentElement.style.setProperty("--header-h", siteHeader.offsetHeight + "px");
    }
    if (!sticky.matches || reduced.matches) { reset(); return; }
    var y = Math.max(window.scrollY, 0);
    var t = Math.min(y / FADE_DISTANCE, 1);
    hero.style.opacity = String(1 - t);
    // Dold när helt uttonad så att den inte fångar klick bakom sektionerna
    hero.style.visibility = t >= 1 ? "hidden" : "";
    var lag = Math.min(y * LAG_FACTOR, LAG_MAX);
    hero.style.transform = lag > 0 ? "translateY(" + lag.toFixed(1) + "px)" : "";
  }
  function schedule() {
    if (!ticking) {
      ticking = true;
      requestAnimationFrame(update);
    }
  }
  window.addEventListener("scroll", schedule, { passive: true });
  window.addEventListener("resize", schedule);
  if (sticky.addEventListener) {
    sticky.addEventListener("change", schedule);
    reduced.addEventListener("change", schedule);
  }
  update();
})();
