document.documentElement.classList.add('js');

const root = document.documentElement;
const body = document.body;
const menuToggle = document.querySelector('.menu-toggle');
const primaryNav = document.querySelector('.primary-nav');
const themeToggle = document.querySelector('.theme-toggle');
const themeMeta = document.querySelector('meta[name="theme-color"]');
const toast = document.querySelector('[data-toast]');
let toastTimer;

function showToast(message) {
  if (!toast) return;
  toast.textContent = message;
  toast.classList.add('is-visible');
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => toast.classList.remove('is-visible'), 1800);
}

function closeMenu(restoreFocus = false) {
  if (!menuToggle || !primaryNav) return;
  const wasOpen = menuToggle.getAttribute('aria-expanded') === 'true';
  menuToggle.setAttribute('aria-expanded', 'false');
  const label = menuToggle.querySelector('.sr-only');
  if (label) label.textContent = 'Open navigation';
  primaryNav.classList.remove('is-open');
  if (restoreFocus && wasOpen) menuToggle.focus();
}

if (menuToggle && primaryNav) {
  menuToggle.addEventListener('click', () => {
    const open = menuToggle.getAttribute('aria-expanded') === 'true';
    menuToggle.setAttribute('aria-expanded', String(!open));
    const label = menuToggle.querySelector('.sr-only');
    if (label) label.textContent = open ? 'Open navigation' : 'Close navigation';
    primaryNav.classList.toggle('is-open', !open);
  });
  primaryNav.querySelectorAll('a').forEach((link) => {
    link.addEventListener('click', () => closeMenu());
  });
}

function setTheme(theme) {
  const nextTheme = theme === 'paper' ? 'paper' : 'ink';
  root.dataset.theme = nextTheme;
  if (themeToggle) {
    themeToggle.textContent = nextTheme === 'ink' ? 'Paper' : 'Ink';
    themeToggle.setAttribute('aria-pressed', String(nextTheme === 'ink'));
  }
  if (themeMeta) {
    themeMeta.setAttribute('content', nextTheme === 'ink' ? '#0b0d0c' : '#f7f7f3');
  }
  try {
    window.localStorage.setItem('debugbrief-theme', nextTheme);
  } catch (_error) {
    return;
  }
}

if (themeToggle) {
  let storedTheme = null;
  try {
    storedTheme = window.localStorage.getItem('debugbrief-theme');
  } catch (_error) {
    storedTheme = null;
  }
  setTheme(storedTheme === 'paper' ? 'paper' : 'ink');
  themeToggle.addEventListener('click', () => {
    setTheme(root.dataset.theme === 'ink' ? 'paper' : 'ink');
  });
}

function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text);
  }
  return new Promise((resolve, reject) => {
    const field = document.createElement('textarea');
    field.value = text;
    field.setAttribute('readonly', '');
    field.style.position = 'fixed';
    field.style.opacity = '0';
    document.body.appendChild(field);
    field.select();
    try {
      const copied = document.execCommand('copy');
      field.remove();
      if (copied) resolve();
      else reject(new Error('copy failed'));
    } catch (error) {
      field.remove();
      reject(error);
    }
  });
}

document.querySelectorAll('[data-copy]').forEach((button) => {
  button.addEventListener('click', () => {
    const value = button.dataset.copy || '';
    copyText(value)
      .then(() => showToast('Command copied.'))
      .catch(() => showToast('Select and copy the command manually.'));
  });
});

function setupTabs(buttonSelector, panelSelector, buttonKey, panelKey, initialValue) {
  const buttons = [...document.querySelectorAll(buttonSelector)];
  const panels = [...document.querySelectorAll(panelSelector)];
  if (!buttons.length || !panels.length) return;

  const select = (button) => {
    const value = button.dataset[buttonKey];
    buttons.forEach((candidate) => {
      const active = candidate === button;
      candidate.classList.toggle('is-active', active);
      candidate.setAttribute('aria-selected', String(active));
      candidate.setAttribute('tabindex', active ? '0' : '-1');
    });
    panels.forEach((panel) => {
      panel.classList.toggle('is-active', panel.dataset[panelKey] === value);
    });
  };

  buttons.forEach((button, index) => {
    button.addEventListener('click', () => select(button));
    button.addEventListener('keydown', (event) => {
      let nextIndex = index;
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
        nextIndex = (index + 1) % buttons.length;
      } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
        nextIndex = (index - 1 + buttons.length) % buttons.length;
      } else if (event.key === 'Home') {
        nextIndex = 0;
      } else if (event.key === 'End') {
        nextIndex = buttons.length - 1;
      } else {
        return;
      }
      event.preventDefault();
      buttons[nextIndex].focus();
      select(buttons[nextIndex]);
    });
  });

  const initial = buttons.find((button) => button.dataset[buttonKey] === initialValue) || buttons[0];
  select(initial);
}

setupTabs('[data-install-tab]', '[data-install-panel]', 'installTab', 'installPanel', 'pipx');
setupTabs('[data-workflow-tab]', '[data-workflow-panel]', 'workflowTab', 'workflowPanel', 'capture');
setupTabs('[data-mode-tab]', '[data-mode-panel]', 'modeTab', 'modePanel', 'pr');

document.querySelectorAll('[data-replay]').forEach((control) => {
  control.addEventListener('click', () => {
    const stage = document.querySelector('.hero-stage');
    if (!stage) return;
    stage.classList.remove('is-replaying');
    void stage.offsetWidth;
    stage.classList.add('is-replaying');
    const replay = document.querySelector('.replay-button');
    if (replay) {
      replay.textContent = 'Proof replayed';
      window.setTimeout(() => {
        replay.innerHTML = 'Replay the proof <span aria-hidden="true">↻</span>';
      }, 900);
    }
    window.setTimeout(() => stage.classList.remove('is-replaying'), 1900);
  });
});

const docsSearch = document.querySelector('[data-docs-search]');
const docItems = [...document.querySelectorAll('[data-doc-item]')];
const docsNavLinks = [...document.querySelectorAll('.docs-nav a, .docs-toc a')];
const docsEmpty = document.querySelector('[data-docs-empty]');

if (docsSearch && docItems.length) {
  const filterDocs = () => {
    const query = docsSearch.value.trim().toLowerCase();
    let visible = 0;
    docItems.forEach((item) => {
      const matches = !query || item.textContent.toLowerCase().includes(query);
      item.hidden = !matches;
      if (matches) visible += 1;
    });
    docsNavLinks.forEach((link) => {
      const target = document.querySelector(link.getAttribute('href'));
      link.hidden = Boolean(target?.hidden);
    });
    if (docsEmpty) docsEmpty.classList.toggle('is-visible', visible === 0);
  };
  docsSearch.addEventListener('input', filterDocs);
  docsSearch.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      docsSearch.value = '';
      filterDocs();
    }
  });
}

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && menuToggle?.getAttribute('aria-expanded') === 'true') {
    closeMenu(true);
  }
  if (
    docsSearch
    && event.key === '/'
    && document.activeElement !== docsSearch
    && !['INPUT', 'TEXTAREA'].includes(document.activeElement?.tagName)
  ) {
    event.preventDefault();
    docsSearch.focus();
  }
});

const revealItems = [...document.querySelectorAll('.reveal')];
if ('IntersectionObserver' in window) {
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      entry.target.classList.add('is-visible');
      observer.unobserve(entry.target);
    });
  }, { threshold: 0.12, rootMargin: '0px 0px -40px' });
  revealItems.forEach((item) => observer.observe(item));
} else {
  revealItems.forEach((item) => item.classList.add('is-visible'));
}

window.addEventListener('resize', () => {
  if (window.innerWidth > 820) closeMenu();
});
