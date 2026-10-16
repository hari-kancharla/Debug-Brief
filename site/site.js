/* DebugBrief: progressive UI and purposeful, optional motion. */
(() => {
  "use strict";
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const root = document.documentElement;
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const systemTheme = matchMedia("(prefers-color-scheme: dark)");
  const fontsReady = document.fonts ? document.fonts.ready : Promise.resolve();
  const animations = new Set();
  let toastTimer;
  let storedTheme = null;
  try { storedTheme = localStorage.getItem("debugbrief-theme"); } catch {}

  function toast(message) {
    const output = $("[data-toast]");
    if (!output) return;
    clearTimeout(toastTimer);
    output.textContent = message;
    toastTimer = setTimeout(() => { output.textContent = ""; }, 2400);
  }

  function applyTheme(theme, save = false) {
    root.dataset.theme = theme;
    const meta = $('meta[name="theme-color"]');
    if (meta) {
      meta.content = theme === "dark" ? "#0D0D0C" : "#F3F2EE";
      meta.removeAttribute("media");
    }
    $$("[data-theme-toggle]").forEach(button => {
      button.textContent = theme;
      button.setAttribute("aria-pressed", String(theme === "dark"));
      button.setAttribute("aria-label", `Color theme: ${theme}. Switch to ${theme === "dark" ? "light" : "dark"}.`);
    });
    if (save) {
      storedTheme = theme;
      try { localStorage.setItem("debugbrief-theme", theme); } catch {}
    }
  }

  applyTheme(storedTheme === "dark" || storedTheme === "light" ? storedTheme : systemTheme.matches ? "dark" : "light");
  $$("[data-theme-toggle]").forEach(button => {
    button.disabled = false;
    button.addEventListener("click", () => applyTheme(root.dataset.theme === "dark" ? "light" : "dark", true));
  });
  systemTheme.addEventListener("change", event => {
    if (storedTheme !== "dark" && storedTheme !== "light") applyTheme(event.matches ? "dark" : "light");
  });

  async function copyText(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch {}
    const active = document.activeElement;
    const selection = window.getSelection();
    const ranges = selection ? Array.from({ length: selection.rangeCount }, (_, i) => selection.getRangeAt(i).cloneRange()) : [];
    const input = document.createElement("textarea");
    input.value = text;
    input.className = "sr-only";
    input.setAttribute("readonly", "");
    document.body.append(input);
    input.select();
    let copied = false;
    try { copied = document.execCommand("copy"); } catch {}
    input.remove();
    if (active instanceof HTMLElement) active.focus({ preventScroll: true });
    if (selection) {
      selection.removeAllRanges();
      ranges.forEach(range => selection.addRange(range));
    }
    return copied;
  }

  $$("button[data-copy]").forEach(button => { button.disabled = false; });
  document.addEventListener("click", async event => {
    const button = event.target.closest("button[data-copy]");
    if (!button || button.disabled) return;
    const copied = await copyText(button.dataset.copy || "");
    toast(copied ? "Copied." : "Copy unavailable. Select the command and copy it.");
  });

  const installers = {
    pipx: "pipx install debugbrief",
    uv: "uv tool install debugbrief",
    pip: "python -m pip install debugbrief",
  };
  $$("[data-install-block]").forEach(block => {
    const command = $("[data-install-command]", block);
    const copy = $("[data-install-copy]", block);
    $$("[data-install-provider]", block).forEach(button => {
      button.disabled = false;
      button.addEventListener("click", () => {
        const text = installers[button.dataset.installProvider];
        if (!text) return;
        command.textContent = text;
        if (copy) copy.dataset.copy = text;
        $$("[data-install-provider]", block).forEach(item => item.setAttribute("aria-pressed", String(item === button)));
      });
    });
  });

  function closeNotes(except = null) {
    $$("[data-strip-note]").forEach(marker => {
      if (marker === except) return;
      marker.setAttribute("aria-expanded", "false");
    });
    $$(".strip-note").forEach(note => {
      if (!except || note.id !== except.getAttribute("aria-controls")) note.hidden = true;
    });
  }

  $$("[data-strip-note]").forEach(marker => {
    const note = document.getElementById(marker.getAttribute("aria-controls"));
    if (!note) return;
    const show = () => {
      closeNotes(marker);
      note.textContent = marker.dataset.stripNote;
      note.style.setProperty("--note-x", `${Math.min(55, Math.max(5, Number(marker.dataset.notePosition) || 25))}%`);
      note.hidden = false;
      marker.setAttribute("aria-expanded", "true");
    };
    const hide = () => {
      note.hidden = true;
      marker.setAttribute("aria-expanded", "false");
    };
    marker.addEventListener("pointerenter", event => { if (event.pointerType !== "touch") show(); });
    marker.addEventListener("pointerleave", () => { if (document.activeElement !== marker) hide(); });
    marker.addEventListener("focus", show);
    marker.addEventListener("blur", hide);
    marker.addEventListener("click", show);
    marker.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        show();
      }
      if (event.key === "Escape") {
        event.preventDefault();
        hide();
      }
    });
  });

  document.addEventListener("pointerdown", event => {
    if (!event.target.closest("[data-strip-note], .strip-note")) closeNotes();
  });

  $$("[role='tablist']").forEach(list => {
    const buttons = $$("[role='tab']", list);
    const activate = button => {
      const panel = document.getElementById(button.getAttribute("aria-controls"));
      if (!panel) return;
      const changed = button.getAttribute("aria-selected") !== "true";
      buttons.forEach(item => {
        const selected = item === button;
        item.setAttribute("aria-selected", String(selected));
        item.tabIndex = selected ? 0 : -1;
        const target = document.getElementById(item.getAttribute("aria-controls"));
        if (!target) return;
        if (window.gsap) window.gsap.killTweensOf(target);
        target.style.removeProperty("opacity");
        target.style.removeProperty("visibility");
        target.hidden = !selected;
      });
      closeNotes();
      if (changed && !reduced.matches && window.gsap) {
        const tween = window.gsap.fromTo(panel, { opacity: 0 }, {
          opacity: 1,
          duration: 0.25,
          ease: "power2.inOut",
          clearProps: "opacity",
        });
        animations.add(tween);
      }
      document.dispatchEvent(new CustomEvent("db:tabchange", { detail: { panel } }));
      if (window.gsap && window.ScrollTrigger) window.ScrollTrigger.refresh();
    };

    buttons.forEach((button, index) => {
      button.addEventListener("click", () => activate(button));
      button.addEventListener("keydown", event => {
        const vertical = list.getAttribute("aria-orientation") === "vertical";
        let next;
        if (event.key === "Home") next = 0;
        if (event.key === "End") next = buttons.length - 1;
        if (event.key === (vertical ? "ArrowDown" : "ArrowRight")) next = (index + 1) % buttons.length;
        if (event.key === (vertical ? "ArrowUp" : "ArrowLeft")) next = (index - 1 + buttons.length) % buttons.length;
        if (next === undefined) return;
        event.preventDefault();
        buttons[next].focus();
        activate(buttons[next]);
      });
    });
  });

  const explorer = $(".explorer-content");
  if (explorer) {
    const annotate = () => $$(".chronology li", explorer).forEach(item => {
      if ($(".badge", item)?.textContent.trim() === "note") item.dataset.kind = "note";
    });
    new MutationObserver(annotate).observe(explorer, { childList: true, subtree: true });
    annotate();
  }

  function setupDocs() {
    const article = $(".docs-article");
    if (!article) return;
    const sections = $$("[data-doc-item][id]", article);
    const links = $$(".docs-nav a[href^='#']");
    let tocFrame = 0;

    const updateToc = () => {
      tocFrame = 0;
      const boundary = ($(".site-header")?.getBoundingClientRect().height || 65) + 36;
      let active = sections[0]?.id;
      sections.forEach(section => {
        if (section.getBoundingClientRect().top <= boundary) active = section.id;
      });
      links.forEach(link => {
        if (link.hash.slice(1) === active) link.setAttribute("aria-current", "location");
        else link.removeAttribute("aria-current");
      });
    };

    window.addEventListener("scroll", () => {
      if (!tocFrame) tocFrame = requestAnimationFrame(updateToc);
    }, { passive: true });
    window.addEventListener("resize", updateToc);
    updateToc();

    const menu = $(".docs-menu");
    if (menu) {
      const mobile = matchMedia("(max-width: 600px)");
      const layout = () => { menu.open = !mobile.matches; };
      mobile.addEventListener("change", layout);
      layout();
      $$("a", menu).forEach(link => link.addEventListener("click", () => {
        if (mobile.matches) menu.open = false;
      }));
    }

    const dialog = $("[data-doc-filter]");
    const input = dialog && $("[data-doc-filter-input]", dialog);
    const results = dialog && $("[data-doc-filter-results]", dialog);
    const status = dialog && $("[data-doc-filter-status]", dialog);
    if (!dialog || !input || !results) return;
    let opener;

    const filter = () => {
      const query = input.value.trim().toLowerCase();
      const matches = sections.filter(section => ($("h2", section)?.textContent || "").toLowerCase().includes(query));
      const limit = Math.max(2, Math.min(6, Math.floor((innerHeight * 0.75 - 140) / 48)));
      results.replaceChildren();
      matches.slice(0, limit).forEach(section => {
        const li = document.createElement("li");
        const a = document.createElement("a");
        a.href = `#${section.id}`;
        a.textContent = $("h2", section).textContent;
        a.addEventListener("click", () => dialog.close());
        li.append(a);
        results.append(li);
      });
      if (status) status.textContent = matches.length
        ? `${Math.min(limit, matches.length)} of ${matches.length} headings. Type to narrow.`
        : "No matching headings.";
    };

    const open = button => {
      if (dialog.open) {
        input.focus();
        return;
      }
      opener = button || document.activeElement;
      input.value = "";
      filter();
      dialog.showModal();
      input.focus();
    };

    $$("[data-doc-filter-open]").forEach(button => {
      button.disabled = false;
      button.addEventListener("click", () => open(button));
    });
    $("[data-doc-filter-close]", dialog)?.addEventListener("click", () => dialog.close());
    input.addEventListener("input", filter);
    dialog.addEventListener("close", () => {
      if (opener instanceof HTMLElement) opener.focus({ preventScroll: true });
    });
    dialog.addEventListener("keydown", event => {
      if (event.key === "Escape") {
        event.preventDefault();
        dialog.close();
        return;
      }
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
      const items = $$("a", results);
      if (!items.length) return;
      event.preventDefault();
      const index = items.indexOf(document.activeElement);
      items[(index + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length].focus();
    });
    document.addEventListener("keydown", event => {
      const typing = event.target.closest("input, textarea, select, [contenteditable='true']");
      if ((event.key === "/" && !typing) || ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k")) {
        event.preventDefault();
        open();
      }
    });
  }

  setupDocs();

  if (window.gsap) {
    const plugins = [window.ScrollTrigger, window.ScrambleTextPlugin, window.DrawSVGPlugin].filter(Boolean);
    if (plugins.length) window.gsap.registerPlugin(...plugins);
  }

  function onceInView(element, callback) {
    if (reduced.matches) {
      callback();
      return;
    }
    if (window.gsap && window.ScrollTrigger) {
      window.ScrollTrigger.create({
        trigger: element,
        start: "top 90%",
        once: true,
        onEnter: callback,
      });
    } else {
      const observer = new IntersectionObserver(entries => {
        if (!entries.some(entry => entry.isIntersecting)) return;
        observer.disconnect();
        callback();
      }, { rootMargin: "0px 0px -10% 0px" });
      observer.observe(element);
    }
  }

  function drawOnce(element, paths) {
    onceInView(element, () => {
      if (!reduced.matches && window.gsap && window.DrawSVGPlugin && paths.length) {
        animations.add(window.gsap.fromTo(paths, { drawSVG: "0%" }, {
          drawSVG: "100%",
          duration: 0.8,
          stagger: 0.04,
          ease: "expo.out",
        }));
      }
    });
  }

  fontsReady.then(() => {
    $$(".evidence-strip").forEach(strip => drawOnce(strip, $$("[data-strip-draw]", strip)));
    const diff = $("[data-principle-diff]");
    if (diff) onceInView(diff, () => {
      if (!reduced.matches && window.gsap) {
        const timeline = window.gsap.timeline();
        timeline.fromTo($$(".diff-removed", diff), { autoAlpha: 0 }, {
          autoAlpha: 1,
          duration: 0.4,
          stagger: 0.08,
          ease: "expo.out",
        });
        timeline.fromTo($$(".diff-added", diff), { clipPath: "inset(0 100% 0 0)" }, {
          clipPath: "inset(0 0% 0 0)",
          duration: 0.6,
          stagger: 0.12,
          ease: "expo.out",
        }, "+=0.08");
        animations.add(timeline);
      }
    });
  });

  const hover = matchMedia("(hover: hover) and (pointer: fine)");
  $$("[data-runner]").forEach(cell => {
    const label = $("span", cell);
    if (!label) return;
    cell.addEventListener("pointerenter", event => {
      if (!hover.matches || event.pointerType === "touch" || reduced.matches) return;
      if (window.gsap && window.ScrambleTextPlugin) {
        window.gsap.killTweensOf(label);
        cell.dataset.outcome = "passed";
        window.gsap.to(label, {
          scrambleText: { text: "exit 0", chars: "01", speed: 0.5 },
          duration: 0.4,
          ease: "power2.inOut",
        });
      }
    });
    cell.addEventListener("pointerleave", () => {
      delete cell.dataset.outcome;
      if (!reduced.matches && window.gsap && window.ScrambleTextPlugin) {
        window.gsap.killTweensOf(label);
        window.gsap.to(label, {
          scrambleText: { text: cell.dataset.runner, chars: "01", speed: 0.5 },
          duration: 0.4,
          ease: "power2.inOut",
        });
      } else label.textContent = cell.dataset.runner;
    });
  });

  function setupTerminal() {
    const host = $(".hero-stage[data-stage]");
    const terminal = host && $(".stage-terminal", host);
    if (!host || !terminal) return;
    const header = $(".site-header");
    const diff = $("[data-stage-diff]", host);
    const records = $$(".terminal-line", host).map(element => ({
      element,
      html: element.innerHTML,
      text: element.textContent,
      prompt: $(".prompt", element)?.cloneNode(true),
      count: 0,
    }));
    const replay = $("[data-stage-replay]", host);
    const skip = $("[data-stage-skip]", host);
    let timeline;
    let ready = false;
    let eligible = false;
    let terminalVisible = false;
    let started = false;
    let completed = false;
    let skipRequested = false;
    let replayRequested = false;
    let timer;

    const state = value => {
      if (header) header.dataset.state = value;
      window.dbStage?.setState(value);
    };

    const restore = () => records.forEach(record => {
      record.element.innerHTML = record.html;
      record.element.classList.remove("is-typing");
      record.element.style.removeProperty("min-block-size");
      record.element.style.removeProperty("visibility");
      record.element.style.removeProperty("opacity");
    });

    const finish = (natural = false) => {
      restore();
      if (diff) {
        diff.style.removeProperty("visibility");
        diff.style.removeProperty("opacity");
        diff.style.removeProperty("transform");
      }
      if (!natural) state("pass");
      window.dbStage?.settle();
      completed = true;
      started = true;
      host.dataset.playback = "complete";
    };

    const measure = () => {
      const current = records.map(record => record.element.innerHTML);
      records.forEach(record => {
        record.element.innerHTML = record.html;
        record.element.style.removeProperty("min-block-size");
      });
      const heights = records.map(record => record.element.getBoundingClientRect().height);
      records.forEach((record, index) => {
        record.element.innerHTML = current[index];
        record.element.style.minBlockSize = `${heights[index]}px`;
      });
    };

    const prepare = () => {
      restore();
      measure();
      records.forEach(record => {
        record.count = 0;
        record.element.style.visibility = "hidden";
        if (record.element.hasAttribute("data-type-command")) record.element.textContent = "";
      });
      if (diff) {
        diff.style.visibility = "hidden";
        diff.style.opacity = "0";
      }
      completed = false;
    };

    const play = () => {
      skipRequested = false;
      if (!ready) {
        replayRequested = true;
        return;
      }
      if (reduced.matches || !window.gsap || !timeline) {
        finish();
        return;
      }
      prepare();
      started = true;
      host.dataset.playback = "playing";
      timeline.restart();
      if (document.hidden || !terminalVisible) timeline.pause();
    };

    const maybeStart = () => {
      if (ready && eligible && terminalVisible && !started && !skipRequested) play();
    };

    if (replay) {
      replay.disabled = false;
      replay.addEventListener("click", play);
    }
    if (skip) {
      skip.disabled = false;
      skip.addEventListener("click", () => {
        skipRequested = true;
        clearTimeout(timer);
        if (timeline) timeline.pause();
        finish();
      });
    }

    const terminalObserver = new IntersectionObserver(entries => {
      terminalVisible = entries[0].isIntersecting && entries[0].intersectionRatio >= 0.4;
      maybeStart();
      if (timeline && started && !completed) {
        if (terminalVisible && !document.hidden) timeline.resume();
        else timeline.pause();
      }
    }, { threshold: 0.4 });
    terminalObserver.observe(terminal);

    document.addEventListener("visibilitychange", () => {
      if (timeline && started && !completed) {
        if (!document.hidden && terminalVisible) timeline.resume();
        else timeline.pause();
      }
    });

    let terminalWidth = 0;
    let terminalFontSize = "";
    new ResizeObserver(entries => {
      const width = entries[0].contentRect.width;
      const fontSize = getComputedStyle($("pre", terminal)).fontSize;
      if (width === terminalWidth && fontSize === terminalFontSize) return;
      terminalWidth = width;
      terminalFontSize = fontSize;
      if (ready && !completed && timeline) measure();
    }).observe(terminal);

    reduced.addEventListener("change", event => {
      if (event.matches) {
        if (timeline) timeline.pause();
        finish();
      }
    });

    fontsReady.then(() => {
      ready = true;
      if (reduced.matches || !window.gsap) {
        finish();
        return;
      }
      if (window.gsap) {
        timeline = window.gsap.timeline({
          paused: true,
          onComplete: () => finish(true),
        });
        timeline.call(() => state("neutral"), [], 0);
        let cursor = 0;

        records.forEach(record => {
          const line = record.element;
          if (line.hasAttribute("data-type-command")) {
            const duration = Math.max(0.15, record.text.length * 0.018);
            timeline.set(line, { autoAlpha: 1 }, cursor);
            timeline.fromTo(record, { count: 0 }, {
              count: record.text.length,
              duration,
              ease: "none",
              immediateRender: false,
              onStart: () => line.classList.add("is-typing"),
              onUpdate: () => {
                const text = record.text.slice(0, Math.floor(record.count));
                if (record.prompt && text) line.replaceChildren(record.prompt, text.slice(1));
                else line.textContent = text;
              },
              onComplete: () => {
                line.innerHTML = record.html;
                line.classList.remove("is-typing");
              },
            }, cursor);
            cursor += duration;
          } else timeline.set(line, { autoAlpha: 1 }, cursor);

          if (line.dataset.terminalState === "fail") {
            timeline.call(() => {
              if (header) header.dataset.state = "fail";
              window.dbStage?.setState("fail");
            }, [], cursor);
          }
          if (line.dataset.terminalState === "pass") {
            timeline.call(() => {
              if (header) header.dataset.state = "pass";
              window.dbStage?.sweepToPass(1.4);
            }, [], cursor);
          }
          if (line.hasAttribute("data-terminal-note") && diff) {
            timeline.fromTo(diff, { autoAlpha: 0, x: 16 }, {
              autoAlpha: 1,
              x: 0,
              duration: 0.6,
              ease: "expo.out",
              immediateRender: false,
            }, cursor);
          }
          if (line.hasAttribute("data-terminal-complete")) {
            timeline.call(() => window.dbStage?.settle(), [], cursor);
          }
          cursor += 0.06 + (Number(line.dataset.pauseAfter) || 0);
        });

        timeline.to({}, { duration: 0.2 }, cursor);
        if (skipRequested) {
          finish();
          return;
        }
        prepare();
        if (replayRequested) play();
        else timer = setTimeout(() => {
          eligible = true;
          maybeStart();
        }, 600);
      }
    });
  }

  setupTerminal();

  reduced.addEventListener("change", event => {
    if (!event.matches) return;
    animations.forEach(animation => {
      animation.totalProgress(1);
      animation.kill();
    });
    animations.clear();
    $$("[data-runner]").forEach(cell => {
      const label = $("span", cell);
      if (window.gsap && label) window.gsap.killTweensOf(label);
      if (label) label.textContent = cell.dataset.runner;
      delete cell.dataset.outcome;
    });
  });
})();
