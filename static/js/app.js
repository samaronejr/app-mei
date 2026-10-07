/*
 * Firm-side interface behaviour. Loaded by templates/base.html and by nothing else:
 * the RLS-isolated client portal ships no JavaScript at all.
 *
 * Four jobs, plus the review page's keyboard shortcuts. The first two exist because an HTMX swap is not a navigation. The
 * browser moves focus and announces a page on navigation; it does neither when a
 * fragment is replaced in place, so a keyboard user is left with focus on an element
 * that no longer exists and a screen reader user is told nothing happened.
 *
 * The third is the mirror image: an ordinary form POST *is* a navigation, and the
 * gap between the click and the new document is exactly where a second click lands.
 * It is presentation only — server-side Post/Redirect/Get is what makes the second
 * request harmless, and it holds with this file deleted.
 *
 * The fourth is the product shell's Alpine components (`dialog`, `palette`,
 * `uploadList`, `unsavedGuard`, `dismissable`), registered on `alpine:init`. That event fires once,
 * when Alpine starts, so under the product shell `templates/base.html` loads Alpine
 * after this file; see the note there.
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
   *
   * An element that is focusable already -- a button, a link, anything carrying its
   * own tabindex -- is focused as it is. Borrowing its tabindex would take it out of
   * the tab order, and removing it on blur would erase a value the template set.
   */
  function focusSwapped(element) {
    if (element.tabIndex < 0 && !element.hasAttribute("tabindex")) {
      element.setAttribute("tabindex", "-1");
      element.addEventListener(
        "blur",
        function () {
          element.removeAttribute("tabindex");
        },
        { once: true }
      );
    }
    element.focus({ preventScroll: true });
  }

  /*
   * A swap may name where focus belongs next: the first field of a form that came
   * back with errors, the row that was just updated, the control that continues the
   * task. The server marks it with data-focus-after-swap, because only the server
   * knows which one it is. Unmarked, focus lands on the swapped region itself, as
   * it always has.
   *
   * The marker is consumed when it is honoured. Left in place it would pull focus
   * back to the same element on the next, unrelated swap anywhere on the page.
   *
   * Looked up in the document as a last resort because an outerHTML swap replaces
   * the target: the element the event names is detached, and the marker is in its
   * replacement.
   */
  var FOCUS_AFTER_SWAP = "[data-focus-after-swap]";

  function focusDestination(target) {
    if (target.matches(FOCUS_AFTER_SWAP)) {
      return target;
    }
    var inside = target.querySelector(FOCUS_AFTER_SWAP);
    if (inside) {
      return inside;
    }
    if (!target.isConnected) {
      return document.querySelector(FOCUS_AFTER_SWAP);
    }
    return null;
  }

  /*
   * A region marked data-swap-keeps-focus is refreshed while the user is typing
   * somewhere else -- the command palette's results, under its combobox. Moving
   * focus there would pull the caret out of the field mid-word, so such a swap
   * only clears the failure notice; the component that owns the region says what
   * changed in its own polite status line.
   */
  var KEEPS_FOCUS = "[data-swap-keeps-focus]";

  function onAfterSwap(event) {
    var target = event.target;
    if (!target || target.nodeType !== 1) {
      return;
    }
    hideFallback();
    // Passive progress already has a polite live region. Its timer must not
    // move keyboard focus or announce an unrelated page heading every two seconds.
    if (target.hasAttribute("data-passive-poll") || target.closest(KEEPS_FOCUS)) {
      return;
    }
    var marked = focusDestination(target);
    if (marked) {
      marked.removeAttribute("data-focus-after-swap");
      focusSwapped(marked);
    } else {
      focusSwapped(target);
    }
    announce(nameOf(target));
  }

  function onFailure(event) {
    var source = event.target;
    var briefing = source && source.nodeType === 1
      ? source.closest("#briefing-content")
      : null;
    var xhr = event.detail && event.detail.xhr;
    var stale = briefing && briefing.querySelector("#briefing-stale");
    if (briefing) {
      if (xhr && xhr.status === 403) {
        briefing.textContent = "";
        window.location.reload();
        return;
      }
      if (stale) {
        stale.removeAttribute("hidden");
        stale.scrollIntoView({ block: "nearest" });
        return;
      }
    }
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

  /*
   * Double submit, and what it is not. The guarantee that a filing is recorded once
   * is server-side Post/Redirect/Get, asserted in the view tests and unaffected by
   * anything below: a browser with no script, a stale tab, or a request forged past
   * this listener meets the same redirect and is answered the same way.
   *
   * What this buys is that the accountant is not left watching two identical requests
   * with no way to tell which one was answered. It is a statement about the interface,
   * not about the database, and it must never be read as the protection.
   */
  var SUBMITTERS = 'button[type="submit"], input[type="submit"]';

  /*
   * Deferred by a tick rather than disabled inside the handler. A disabled control is
   * omitted from the form data, and the browser serializes that data after this
   * listener returns -- so disabling synchronously drops the submit button's own name
   * and value from the POST. No form here carries a named submitter today; the tick
   * is what stops the first one that does from failing for an invisible reason.
   */
  var DISABLE_DELAY_MS = 0;

  function setDisabled(controls, state) {
    for (var position = 0; position < controls.length; position += 1) {
      controls[position].disabled = state;
    }
  }

  function onSubmit(event) {
    var form = event.target;
    if (!form || form.nodeType !== 1 || form.hasAttribute("hx-disabled-elt")) {
      return;
    }
    var controls = form.querySelectorAll(SUBMITTERS);
    window.setTimeout(function () {
      setDisabled(controls, true);
    }, DISABLE_DELAY_MS);
  }

  document.body.addEventListener("submit", onSubmit);

  /*
   * A restore from the back/forward cache replays the document exactly as it was left,
   * disabled buttons included. Without this, Back lands on a form that cannot be
   * submitted again -- which reads as the product being broken, by the one mechanism
   * a user reaches for when they think something went wrong.
   */
  function onPageShow(event) {
    if (event.persisted) {
      setDisabled(document.querySelectorAll(SUBMITTERS), false);
    }
  }

  window.addEventListener("pageshow", onPageShow);

  /*
   * Alpine components. The CSP build of Alpine evaluates only references in x-*
   * attributes, so all behaviour lives here, in plain methods, and templates name
   * a component and its methods and never write an expression.
   *
   * Values a component needs from the server arrive through data-* attributes read
   * with dataset, never through the expression itself (docs/frontend.md).
   */

  /*
   * controlHidden is false once a component is running. A control that needs script
   * to act ships with the hidden attribute and binds it to this, so it appears only
   * when it can work: without script there is no dead button to press.
   */
  function dialog() {
    return {
      controlHidden: false,
      opener: null,

      /*
       * A dialog rendered open by the server -- a drawer reached by its own URL --
       * arrives as a plain open dialog, which is what a script-less reader gets.
       * Here it is closed and reopened modal, so it gains the inert background and
       * Escape like one opened by a click.
       */
      init: function () {
        var element = this.$refs.dialog;
        this.opener = document.getElementById(element.dataset.openerId);
        if (!element.open) return;
        // Fragment navigation runs during loading; upgrade after it so it cannot
        // move keyboard focus back out of the modal.
        if (document.readyState === "complete") {
          this.openFromUrl();
        } else {
          window.addEventListener("load", this.openFromUrl.bind(this), { once: true });
        }
      },

      openFromUrl: function () {
        var element = this.$refs.dialog;
        var remembered;
        var target;
        if (element.open) {
          element.close();
          element.showModal();
          if (element.dataset.openUrl) {
            window.history.replaceState(
              window.history.state, "", element.dataset.openUrl,
            );
          }
          remembered = document.getElementById(
            window.history.state && window.history.state.exceptionEvidence,
          );
          target = element.querySelector("[data-dialog-focus]") ||
            (remembered && element.contains(remembered) ? remembered :
              element.querySelector(".overlay-head button"));
          if (target) target.focus();
        }
      },

      rememberLink: function (event) {
        var link = event.target.closest("a[data-exception-evidence]");
        if (link) {
          window.history.replaceState(
            Object.assign({}, window.history.state, { exceptionEvidence: link.id }),
            "",
          );
        }
      },

      show: function () {
        this.opener = document.activeElement;
        this.$refs.dialog.showModal();
      },

      close: function () {
        this.$refs.dialog.close();
      },

      /*
       * Bound to the dialog's own close event, so it runs however the dialog was
       * closed -- the close button, Escape, or a form inside it. Focus goes back
       * to the control that opened it, if that control still exists.
       */
      restoreFocus: function () {
        var element = this.$refs.dialog;
        var state;
        // The server-open upgrade queues a close event before reopening modal.
        if (element.open) return;
        if (element.dataset.closeUrl) {
          state = Object.assign({}, window.history.state);
          delete state.exceptionEvidence;
          window.history.replaceState(state, "", element.dataset.closeUrl);
        }
        var opener = this.opener;
        this.opener = null;
        if (opener && opener.isConnected && typeof opener.focus === "function") {
          opener.focus();
        }
      },
    };
  }

  function dismissable() {
    return {
      controlHidden: false,

      dismiss: function () {
        this.$root.remove();
      },
    };
  }

  /*
   * Warn before leaving a form with edits nobody saved. Attach with
   * x-data="unsavedGuard" on the form. Any input or change marks it dirty; its own
   * submit marks it clean, so saving never triggers the warning.
   *
   * beforeunload is the only warning a browser permits here, and it shows the
   * browser's own wording: a page may ask, it may not choose the sentence.
   */
  function unsavedGuard() {
    return {
      dirty: false,
      handler: null,

      init: function () {
        var state = this;
        this.handler = function (event) {
          if (state.dirty) {
            event.preventDefault();
            event.returnValue = "";
          }
        };
        window.addEventListener("beforeunload", this.handler);
        this.$el.addEventListener("input", function () {
          state.dirty = true;
        });
        this.$el.addEventListener("change", function () {
          state.dirty = true;
        });
        this.$el.addEventListener("submit", function () {
          state.dirty = false;
        });
      },

      destroy: function () {
        window.removeEventListener("beforeunload", this.handler);
      },
    };
  }

  var BYTES_PER_KB = 1024;
  var BYTES_PER_MB = 1024 * 1024;
  var WRONG_TYPE = "Tipo de arquivo não aceito.";

  function sizeLabel(bytes) {
    if (bytes >= BYTES_PER_MB) {
      return (
        (bytes / BYTES_PER_MB).toLocaleString("pt-BR", {
          maximumFractionDigits: 1,
        }) + " MB"
      );
    }
    return Math.max(1, Math.round(bytes / BYTES_PER_KB)) + " KB";
  }

  /*
   * The accept list as the browser reads it: ".pdf" is an extension, "image/*" a
   * family of types, anything else one exact type. Empty accepts everything.
   */
  function accepts(acceptList, file) {
    var tokens = (acceptList || "")
      .split(",")
      .map(function (token) {
        return token.trim().toLowerCase();
      })
      .filter(Boolean);
    if (tokens.length === 0) {
      return true;
    }
    var name = file.name.toLowerCase();
    var type = (file.type || "").toLowerCase();
    return tokens.some(function (token) {
      if (token.charAt(0) === ".") {
        return name.slice(-token.length) === token;
      }
      if (token.slice(-2) === "/*") {
        return type.indexOf(token.slice(0, -1)) === 0;
      }
      return type === token;
    });
  }

  /*
   * Show every chosen file before it is sent, and drop the ones this page already
   * knows the server will refuse. Refused files stay in the list with the reason,
   * but are removed from the input, so they are never uploaded. The server checks
   * again and is the authority; this is the courtesy of saying so early.
   *
   * When the form around it posts through HTMX (the portal request page, T11), the
   * same component also shows the upload itself: `htmx:xhr:progress` moves the bar,
   * "Cancelar envio" raises `htmx:abort`, and a failed or cancelled send keeps the
   * chosen file in the input so "Tentar de novo" resubmits it without choosing again.
   * A refusal from the server (400) is shown as the text it answered, via x-text,
   * never as HTML.
   */
  var UPLOAD_SENDING = "Enviando…";
  var UPLOAD_DONE = "Arquivo enviado.";
  var UPLOAD_CANCELLED =
    "Envio cancelado. O arquivo continua escolhido: toque em Tentar de novo.";
  var UPLOAD_FAILED =
    "Não foi possível enviar. Verifique sua conexão; o arquivo continua escolhido.";
  var HTTP_BAD_REQUEST = 400;

  function uploadList() {
    return {
      items: [],
      counter: 0,
      phase: "idle",
      progress: 0,
      message: "",

      get isUploading() {
        return this.phase === "uploading" || this.phase === "cancelling";
      },

      get canRetry() {
        return this.phase === "failed" || this.phase === "cancelled";
      },

      /* Negations as getters: the CSP build evaluates names, not expressions. */
      get isIdle() {
        return !this.isUploading;
      },

      get notUploading() {
        return !this.isUploading;
      },

      get cannotRetry() {
        return !this.canRetry;
      },

      get progressLabel() {
        return this.isUploading ? UPLOAD_SENDING + " " + this.progress + "%" : "";
      },

      onStart: function () {
        this.phase = "uploading";
        this.progress = 0;
        this.message = UPLOAD_SENDING;
      },

      onProgress: function (event) {
        var detail = event.detail || {};
        if (detail.lengthComputable && detail.total > 0) {
          this.progress = Math.min(
            100,
            Math.round((detail.loaded * 100) / detail.total)
          );
        }
      },

      onDone: function (event) {
        if (this.phase === "cancelling") {
          this.phase = "cancelled";
          this.message = UPLOAD_CANCELLED;
          return;
        }
        var detail = event.detail || {};
        if (detail.successful) {
          this.phase = "done";
          this.progress = 100;
          this.message = UPLOAD_DONE;
          this.items = [];
          this.$refs.input.value = "";
          return;
        }
        var xhr = detail.xhr;
        this.phase = "failed";
        this.message =
          xhr && xhr.status === HTTP_BAD_REQUEST && xhr.responseText
            ? xhr.responseText
            : UPLOAD_FAILED;
      },

      cancel: function () {
        if (!this.isUploading) {
          return;
        }
        this.phase = "cancelling";
        window.htmx.trigger(this.$root, "htmx:abort");
      },

      retry: function () {
        this.$root.requestSubmit();
      },

      get isEmpty() {
        return this.items.length === 0;
      },

      get summary() {
        var ready = this.items.filter(function (item) {
          return item.ready;
        }).length;
        if (ready === 0) {
          return "";
        }
        return ready === 1
          ? "1 arquivo pronto para enviar."
          : ready + " arquivos prontos para enviar.";
      },

      refresh: function () {
        var data = this.$root.dataset;
        var ceiling = parseInt(data.maxBytes, 10);
        var tooLarge = "Arquivo acima do limite" +
          (data.maxLabel ? " de " + data.maxLabel : "") + ".";
        var files = Array.prototype.slice.call(this.$refs.input.files);
        var counter = this.counter;
        var items = files.map(function (file) {
          var error = "";
          if (!accepts(data.accept, file)) {
            error = WRONG_TYPE;
          } else if (ceiling > 0 && file.size > ceiling) {
            error = tooLarge;
          }
          counter += 1;
          return {
            key: String(counter),
            file: file,
            name: file.name,
            size: sizeLabel(file.size),
            error: error,
            ready: error === "",
            refused: error !== "",
            state: error === "" ? "ready" : "refused",
          };
        });
        this.counter = counter;
        this.items = items;
        this.sync();
      },

      remove: function (event) {
        var key = event.currentTarget.dataset.key;
        this.items = this.items.filter(function (item) {
          return item.key !== key;
        });
        this.sync();
      },

      /* The input carries exactly the files still listed as ready. */
      sync: function () {
        var transfer = new DataTransfer();
        this.items.forEach(function (item) {
          if (item.ready) {
            transfer.items.add(item.file);
          }
        });
        this.$refs.input.files = transfer.files;
      },
    };
  }

  /*
   * Single-key shortcuts for a page that opts in with `data-shortcuts` (the document
   * review, T13). A key names an element by `data-shortcut`: a field is focused, a
   * button or link is activated exactly as a click would. Never while the user types
   * in a field or holds a modifier, so typing an "a" in the reason is just a letter.
   */
  function typing(target) {
    return (
      target instanceof HTMLElement &&
      (target.isContentEditable ||
        target.matches("input, textarea, select, [role='textbox']"))
    );
  }

  document.addEventListener("keydown", function (event) {
    if (
      event.defaultPrevented ||
      event.altKey ||
      event.ctrlKey ||
      event.metaKey ||
      typing(event.target) ||
      !event.key ||
      event.key.length !== 1
    ) {
      return;
    }
    var scope = document.querySelector("[data-shortcuts]");
    if (!scope) {
      return;
    }
    var key = event.key.toLowerCase();
    var target = scope.querySelector('[data-shortcut="' + key + '"]');
    if (!target) {
      return;
    }
    event.preventDefault();
    target.focus();
    if (!target.matches("input, textarea, select")) {
      target.click();
    }
  });

  /*
   * The command palette (templates/partials/ui/palette.html, AXR1-021). A combobox
   * in a native modal <dialog>: Ctrl/Cmd+K opens it from anywhere on the page, HTMX
   * fills #palette-results as the user types, and the arrow keys walk the options
   * by aria-activedescendant -- focus never leaves the field, which is what lets
   * the user keep typing to narrow the list. Enter or a click follows the active
   * option's data-href, a path the server built with reverse().
   */
  var PALETTE_UPDATED = "Resultados atualizados.";
  var PALETTE_EMPTY = "Nenhum resultado.";
  var PALETTE_KEY = "k";

  function palette() {
    return {
      expanded: false,
      activeId: null,
      opener: null,
      shortcut: null,

      init: function () {
        var state = this;
        this.shortcut = function (event) {
          if (
            (event.ctrlKey || event.metaKey) &&
            !event.altKey &&
            !event.shiftKey &&
            event.key &&
            event.key.toLowerCase() === PALETTE_KEY
          ) {
            event.preventDefault();
            state.show();
          }
        };
        document.addEventListener("keydown", this.shortcut);
        this.$refs.results.addEventListener("htmx:afterSwap", function () {
          state.onResults();
        });
        this.$refs.results.addEventListener("click", function (event) {
          var option = event.target.closest('[role="option"]');
          if (option) {
            state.follow(option);
          }
        });
      },

      destroy: function () {
        document.removeEventListener("keydown", this.shortcut);
      },

      show: function () {
        var element = this.$refs.dialog;
        if (!element.open) {
          this.opener = document.activeElement;
          element.showModal();
        }
        this.$refs.input.focus();
        this.$refs.input.select();
      },

      close: function () {
        this.$refs.dialog.close();
      },

      restoreFocus: function () {
        var opener = this.opener;
        this.opener = null;
        if (opener && opener.isConnected && typeof opener.focus === "function") {
          opener.focus();
        }
      },

      options: function () {
        return Array.prototype.slice.call(
          this.$refs.results.querySelectorAll('[role="option"]')
        );
      },

      onResults: function () {
        var count = this.options().length;
        this.activeId = null;
        this.expanded = count > 0;
        this.$refs.status.textContent = count > 0 ? PALETTE_UPDATED : PALETTE_EMPTY;
      },

      activate: function (options, index) {
        options.forEach(function (option, position) {
          option.setAttribute("aria-selected", position === index ? "true" : "false");
        });
        this.activeId = options[index].id;
        options[index].scrollIntoView({ block: "nearest" });
      },

      move: function (step) {
        var options = this.options();
        if (options.length === 0) {
          return;
        }
        var activeId = this.activeId;
        var index = options.findIndex(function (option) {
          return option.id === activeId;
        });
        if (index < 0) {
          index = step > 0 ? 0 : options.length - 1;
        } else {
          index = (index + step + options.length) % options.length;
        }
        this.activate(options, index);
      },

      follow: function (option) {
        var href = option.dataset.href || "";
        // Only a same-origin path: the attribute is server-built, and this keeps a
        // future template mistake from ever following a scheme or another host.
        if (href.charAt(0) === "/" && href.charAt(1) !== "/") {
          window.location.assign(href);
        }
      },

      onKeydown: function (event) {
        if (event.key === "ArrowDown") {
          event.preventDefault();
          this.move(1);
        } else if (event.key === "ArrowUp") {
          event.preventDefault();
          this.move(-1);
        } else if (event.key === "Enter" && this.activeId) {
          event.preventDefault();
          this.follow(document.getElementById(this.activeId));
        }
      },
    };
  }

  document.addEventListener("alpine:init", function () {
    window.Alpine.data("dialog", dialog);
    window.Alpine.data("palette", palette);
    window.Alpine.data("dismissable", dismissable);
    window.Alpine.data("unsavedGuard", unsavedGuard);
    window.Alpine.data("uploadList", uploadList);
  });
})();
