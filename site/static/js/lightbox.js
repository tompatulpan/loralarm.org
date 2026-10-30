// Klicka på en bild → öppna den förstorad i ett overlay-lager.
// Stäng med klick utanför bilden eller Esc.
(function () {
  var overlay = document.createElement("div");
  overlay.className = "lightbox-overlay";
  overlay.hidden = true;
  overlay.innerHTML = '<img alt="">';

  var big = overlay.querySelector("img");
  overlay.addEventListener("click", function () {
    overlay.hidden = true;
    document.body.style.overflow = "";
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !overlay.hidden) {
      overlay.hidden = true;
      document.body.style.overflow = "";
    }
  });
  document.body.appendChild(overlay);

  document.addEventListener("click", function (e) {
    var link = e.target.closest("a.lightbox");
    if (!link) return;
    e.preventDefault();
    big.src = link.getAttribute("href");
    big.alt = (link.querySelector("img") || {}).alt || "";
    overlay.hidden = false;
    document.body.style.overflow = "hidden";
  });
})();
