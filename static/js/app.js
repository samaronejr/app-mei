/*
 * Firm-side interface behaviour. Loaded by templates/base.html and by nothing else:
 * the RLS-isolated client portal ships no JavaScript at all.
 *
 * Two jobs, both of which exist because an HTMX swap is not a navigation. The
 * browser moves focus and announces a page on navigation; it does neither when a
 * fragment is replaced in place, so a keyboard user is left with focus on an element
 * that no longer exists and a screen reader user is told nothing happened.
 *
 * Everything here is written against a Content-Security-Policy of `script-src 'self'`
 * with no 'unsafe-eval' (apps/security/csp.py), and against `allowEval: false` in the
 * htmx-config meta. So: no eval, no Function constructor, no attribute-compiled HTMX
 * handlers, no inline script. Behaviour is attached by listening on document.body,
 * which is where HTMX events bubble to.
 */
(function () {
  "use strict";

  var ANNOUNCER_ID = "htmx-announce";
  var FALLBACK_ID = "htmx-error-fallback";

  var HEADINGS = "h1, h2, h3, h4, h5, h6";

  /*
   * Portuguese, because every string a user of this product reads is. Kept next to
   * the code rather than fetched from a data attribute: this is the one message that
   * has to survive the request that would have carried it failing.
   */
  var SWAP_FALLBACK = "Conteúdo atualizado.";
  var FAILURE =
    "Não foi possível carregar. Verifique sua conexão e tente novamente.";

  /*
   * The delay is load-bearing, not a workaround. A live region is announced when its
   * contents *change*, so writing the same string twice is silent, and writing into
   * a region in the same frame it was cleared is frequently missed. Clearing, then
   * setting on a later tick, is what makes two consecutive failures announce twice.
   */
  var ANNOUNCE_DELAY_MS = 100;

  function announce(text) {
    var region = document.getElementById(ANNOUNCER_ID);
    if (!region || !text) {
      return;
    }
    region.textContent = "";
    window.setTimeout(function () {
      region.textContent = text;
    }, ANNOUNCE_DELAY_MS);
  }

  function trimmed(value) {
    return value ? value.replace(/\s+/g, " ").trim() : "";
  }

  /*
   * What the swapped region calls itself, in the order assistive technology would
   * resolve it: an explicit label first, then the element a label points at, then
   * the heading the region opens with. The generic sentence is last because a swap
   * nobody can name still has to be announced as having happened.
   */
  function nameOf(element) {
    var label = trimmed(element.getAttribute("aria-label"));
    if (label) {
      return label;
    }

    var labelledBy = trimmed(element.getAttribute("aria-labelledby"));
    if (labelledBy) {
      var owner = document.getElementById(labelledBy.split(" ")[0]);
      var owned = owner ? trimmed(owner.textContent) : "";
      if (owned) {
        return owned;
      }
    }

    var heading = element.querySelector(HEADINGS);
    var headed = heading ? trimmed(heading.textContent) : "";
    if (headed) {
      return headed;
    }

    return SWAP_FALLBACK;
  }

  function fallback() {
    return document.getElementById(FALLBACK_ID);
  }

  function hideFallback() {
    var block = fallback();
    if (block) {
      block.setAttribute("hidden", "hidden");
    }
  }

  function showFallback() {
    var block = fallback();
    if (block) {
      block.removeAttribute("hidden");
    }
  }

  /*
   * tabindex is added here rather than written into the template because a permanent
   * tabindex="-1" is a permanent lie about the element: it reports as programmatically
   * focusable to assistive technology for the whole life of the page, when it is only
   * meant to receive focus for the instant after a swap. It is removed again on blur,
   * so the attribute exists exactly as long as the focus does.
   */
  function focusSwapped(element) {
    element.setAttribute("tabindex", "-1");
    element.addEventListener(
      "blur",
      function () {
        element.removeAttribute("tabindex");
      },
      { once: true }
    );
    element.focus({ preventScroll: true });
  }

  function onAfterSwap(event) {
    var target = event.target;
    if (!target || target.nodeType !== 1) {
      return;
    }
    hideFallback();
    focusSwapped(target);
    announce(nameOf(target));
  }

  function onFailure() {
    showFallback();
    announce(FAILURE);
  }

  document.body.addEventListener("htmx:afterSwap", onAfterSwap);

  /*
   * Three events, one handler. HTMX reports a 5xx, a network failure and an expired
   * timeout separately, and a user experiences all three identically: the thing they
   * asked for did not arrive.
   */
  var FAILURES = ["htmx:responseError", "htmx:timeout", "htmx:sendError"];
  for (var index = 0; index < FAILURES.length; index += 1) {
    document.body.addEventListener(FAILURES[index], onFailure);
  }
})();
