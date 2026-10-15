document.documentElement.classList.add('js');

const menuToggle = document.querySelector('.report-menu');
const primaryNav = document.querySelector('.report-nav');
const toast = document.querySelector('[data-toast]');
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
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
  primaryNav.querySelectorAll('a').forEach((link) => link.addEventListener('click', () => closeMenu()));
}

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
      const active = panel.dataset[panelKey] === value;
      panel.classList.toggle('is-active', active);
      panel.hidden = !active;
    });
  };

  buttons.forEach((button, index) => {
    button.addEventListener('click', () => select(button));
    button.addEventListener('keydown', (event) => {
      let nextIndex = index;
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') nextIndex = (index + 1) % buttons.length;
      else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') nextIndex = (index - 1 + buttons.length) % buttons.length;
      else if (event.key === 'Home') nextIndex = 0;
      else if (event.key === 'End') nextIndex = buttons.length - 1;
      else return;
      event.preventDefault();
      buttons[nextIndex].focus();
      select(buttons[nextIndex]);
    });
  });

  const initial = buttons.find((button) => button.dataset[buttonKey] === initialValue) || buttons[0];
  select(initial);
}

setupTabs('[data-runner-tab]', '[data-runner-panel]', 'runnerTab', 'runnerPanel', 'pytest');

function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
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
    copyText(button.dataset.copy || '')
      .then(() => showToast('Command copied.'))
      .catch(() => showToast('Select and copy the command manually.'));
  });
});

const proofTimeline = document.querySelector('.proof-timeline');
const replayButton = document.querySelector('[data-replay-proof]');
const replayLabel = document.querySelector('[data-replay-label]');

if (replayButton && proofTimeline) {
  replayButton.addEventListener('click', () => {
    proofTimeline.classList.remove('is-replaying');
    void proofTimeline.offsetWidth;
    proofTimeline.classList.add('is-replaying');
    if (replayLabel) replayLabel.textContent = 'Proof replayed';
    window.setTimeout(() => {
      proofTimeline.classList.remove('is-replaying');
      if (replayLabel) replayLabel.textContent = 'Replay the proof';
    }, 1000);
  });
}

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && menuToggle?.getAttribute('aria-expanded') === 'true') closeMenu(true);
});

window.addEventListener('resize', () => {
  if (window.innerWidth > 820) closeMenu();
});

const revealItems = [...document.querySelectorAll('.section-intro, .proof-layout, .runner-tabs, .runner-panels, .limits-copy, .limits-list, .limits-files, .install-grid')];
if ('IntersectionObserver' in window && !reducedMotion.matches) {
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      entry.target.classList.add('is-visible');
      observer.unobserve(entry.target);
    });
  }, { threshold: 0.12, rootMargin: '0px 0px -36px' });
  revealItems.forEach((item) => {
    item.classList.add('reveal');
    observer.observe(item);
  });
} else {
  revealItems.forEach((item) => item.classList.add('is-visible'));
}
