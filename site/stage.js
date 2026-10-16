/* DebugBrief field. The terminal timeline calls the public API below.

   Simplex noise license: https://github.com/ashima/webgl-noise/blob/master/LICENSE
   Copyright (C) 2011 by Ashima Arts (Simplex noise)
   Copyright (C) 2011-2016 by Stefan Gustavson (Classic noise and others)

   Permission is hereby granted, free of charge, to any person obtaining a copy
   of this software and associated documentation files (the "Software"), to deal
   in the Software without restriction, including without limitation the rights
   to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
   copies of the Software, and to permit persons to whom the Software is
   furnished to do so, subject to the following conditions:
   The above copyright notice and this permission notice shall be included in
   all copies or substantial portions of the Software.
   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
   IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
   FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
   AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
   LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
   OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
   THE SOFTWARE.
*/
(() => {
  "use strict";

  const host = document.querySelector(".hero-stage[data-stage]");
  if (!host) return;

  const hero = host.querySelector(".hero");
  const motion = matchMedia("(prefers-reduced-motion: reduce)");
  const scheme = matchMedia("(prefers-color-scheme: dark)");

  let reduced = motion.matches;
  let state = reduced ? 2 : 0;
  let sweep = 1;
  let sweepStart = null;
  let sweepDuration = 1.4;
  let settled = false;
  let visible = false;
  let elapsed = 0;
  let noiseTime = 0;
  let ready = false;
  let previousStamp = null;
  let frame = 0;
  let renderer, scene, camera, material, geometry, uniforms, THREE;
  let failed = false;
  let disposed = false;
  let size = { width: 1, height: 1 };

  const pointer = { x: -10, y: -10 };
  const click = { x: -10, y: -10, time: -100 };

  function stateNumber(value) {
    if (value === "fail" || value === "failed" || value === 1) return 1;
    if (value === "pass" || value === "passed" || value === 2) return 2;
    return 0;
  }

  function setState(value) {
    state = reduced ? 2 : stateNumber(value);
    sweep = state === 2 ? 1 : 0;
    sweepStart = null;
    settled = false;
    host.dataset.fieldState = String(state);
    syncState();
    invalidate();
  }

  function sweepToPass(seconds = 1.4) {
    if (reduced) {
      setState(2);
      return;
    }

    state = 2;
    sweep = 0;
    sweepStart = elapsed;
    sweepDuration = Number.isFinite(seconds)
      ? Math.max(0.1, seconds)
      : 1.4;

    host.dataset.fieldState = "2";
    syncState();
    invalidate();
  }

  function settle() {
    settled = true;
    invalidate();
  }

  window.dbStage = Object.freeze({ setState, sweepToPass, settle });
  host.dataset.fieldState = String(state);

  function syncState() {
    if (!uniforms) return;
    uniforms.uState.value = reduced ? 2 : state;
    uniforms.uSweep.value = reduced ? 1 : sweep;
  }

  function readColors() {
    if (!uniforms) return;

    const css = getComputedStyle(host);
    for (const [name, token] of [
      ["uPaper", "--paper"],
      ["uInk", "--ink-2"],
      ["uFail", "--fail"],
      ["uPass", "--pass"],
    ]) {
      uniforms[name].value.setStyle(
        css.getPropertyValue(token).trim(),
      );
    }
  }

  function resize() {
    if (!renderer || disposed || failed) return;

    const bounds = host.getBoundingClientRect();
    const start = host.querySelector("[data-field-start]") || hero;
    const startBounds = start.getBoundingClientRect();
    const cell = bounds.width <= 600 ? 3 : 4;
    const width = Math.max(1, Math.ceil(bounds.width / cell));
    const height = Math.max(1, Math.ceil(bounds.height / cell));
    const heroEnd = Math.min(
      0.95,
      (startBounds.bottom - bounds.top) / Math.max(bounds.height, 1),
    );

    if (
      renderer.domElement.width === width &&
      renderer.domElement.height === height &&
      uniforms.uHeroEnd.value === heroEnd
    ) {
      return;
    }

    size = { width, height };
    renderer.setSize(width, height, false);
    renderer.domElement.style.width = width * cell + "px";
    renderer.domElement.style.height = height * cell + "px";
    uniforms.uResolution.value.set(width, height);
    uniforms.uHeroEnd.value = heroEnd;
    invalidate();
  }

  function stop() {
    if (frame) cancelAnimationFrame(frame);
    frame = 0;
    previousStamp = null;
  }

  function invalidate() {
    if (
      !renderer ||
      !ready ||
      disposed ||
      failed ||
      !visible ||
      document.hidden ||
      frame
    ) {
      return;
    }

    frame = requestAnimationFrame(render);
  }

  function render(stamp) {
    frame = 0;

    if (
      !renderer ||
      disposed ||
      failed ||
      !visible ||
      document.hidden
    ) {
      return;
    }

    if (!reduced && previousStamp !== null) {
      const delta = Math.min(
        (stamp - previousStamp) / 1000,
        0.1,
      );
      elapsed += delta;
      noiseTime += delta * (settled ? 0.4 : 1);
    }

    previousStamp = stamp;

    if (sweepStart !== null && !reduced) {
      const progress = Math.min(
        1,
        (elapsed - sweepStart) / sweepDuration,
      );

      sweep = progress < 0.5
        ? 2 * progress * progress
        : 1 - Math.pow(-2 * progress + 2, 2) / 2;

      if (progress === 1) sweepStart = null;
    }

    uniforms.uTime.value = reduced ? 0 : elapsed;
    uniforms.uNoiseTime.value = reduced ? 0 : noiseTime;
    uniforms.uState.value = reduced ? 2 : state;
    uniforms.uSweep.value = reduced ? 1 : sweep;
    uniforms.uPointer.value.set(
      reduced ? -10 : pointer.x,
      reduced ? -10 : pointer.y,
    );
    uniforms.uClickPos.value.set(click.x, click.y);
    uniforms.uClickTime.value = reduced ? -100 : click.time;

    renderer.render(scene, camera);

    if (!reduced) invalidate();
  }

  function fallback() {
    failed = true;
    stop();
    host.classList.remove("has-webgl");
    host.classList.add("no-webgl");

    if (renderer) {
      renderer.domElement.remove();
      renderer.dispose();
    }

    material?.dispose();
    geometry?.dispose();
  }

  const visibility = new IntersectionObserver(
    entries => {
      visible = entries[0].isIntersecting;
      if (visible) invalidate();
      else stop();
    },
    { threshold: 0 },
  );

  visibility.observe(host);

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stop();
    else invalidate();
  });

  host.addEventListener(
    "pointermove",
    event => {
      if (reduced || event.pointerType === "touch") return;

      const rect = host.getBoundingClientRect();
      pointer.x = (event.clientX - rect.left) / rect.width;
      pointer.y = 1 - (event.clientY - rect.top) / rect.height;
    },
    { passive: true },
  );

  host.addEventListener("pointerleave", () => {
    pointer.x = pointer.y = -10;
  });

  host.addEventListener("click", event => {
    if (
      reduced ||
      event.target.closest(
        "a, button, input, select, textarea, summary, pre, code, [role='tab']",
      )
    ) {
      return;
    }

    const rect = host.getBoundingClientRect();
    click.x = (event.clientX - rect.left) / rect.width;
    click.y = 1 - (event.clientY - rect.top) / rect.height;
    click.time = elapsed;
  });

  motion.addEventListener("change", event => {
    reduced = event.matches;
    stop();

    if (reduced) {
      state = 2;
      sweep = 1;
      sweepStart = null;
    }

    host.dataset.fieldState = String(state);
    syncState();
    invalidate();
  });

  scheme.addEventListener("change", () => {
    readColors();
    invalidate();
  });

  const themeObserver = new MutationObserver(() => {
    readColors();
    invalidate();
  });

  themeObserver.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });

  const resizeObserver = new ResizeObserver(resize);
  resizeObserver.observe(host);
  resizeObserver.observe(hero);

  window.addEventListener("pageshow", () => {
    resize();
    invalidate();
  });

  window.addEventListener("pagehide", event => {
    stop();
    if (event.persisted) return;

    disposed = true;
    visibility.disconnect();
    resizeObserver.disconnect();
    themeObserver.disconnect();
    material?.dispose();
    geometry?.dispose();
    renderer?.dispose();
  });

  const vertexShader = `
    varying vec2 vUv;

    void main() {
      vUv = uv;
      gl_Position = vec4(position.xy, 0.0, 1.0);
    }
  `;

  const fragmentShader = `
    precision highp float;

    varying vec2 vUv;

    uniform float uTime;
    uniform vec2 uResolution;
    uniform float uHeroEnd;
    uniform float uState;
    uniform float uSweep;
    uniform float uNoiseTime;
    uniform vec2 uPointer;
    uniform vec2 uClickPos;
    uniform float uClickTime;
    uniform vec3 uPaper;
    uniform vec3 uInk;
    uniform vec3 uFail;
    uniform vec3 uPass;

    // 2D simplex noise, Ashima Arts and Stefan Gustavson, MIT license.
    vec3 mod289(vec3 x) {
      return x - floor(x * (1.0 / 289.0)) * 289.0;
    }

    vec2 mod289(vec2 x) {
      return x - floor(x * (1.0 / 289.0)) * 289.0;
    }

    vec3 permute(vec3 x) {
      return mod289(((x * 34.0) + 1.0) * x);
    }

    float simplex(vec2 v) {
      const vec4 C = vec4(
        0.211324865405187,
        0.366025403784439,
        -0.577350269189626,
        0.024390243902439
      );

      vec2 i = floor(v + dot(v, C.yy));
      vec2 x0 = v - i + dot(i, C.xx);
      vec2 i1 = x0.x > x0.y
        ? vec2(1.0, 0.0)
        : vec2(0.0, 1.0);

      vec4 x12 = x0.xyxy + C.xxzz;
      x12.xy -= i1;
      i = mod289(i);

      vec3 p = permute(
        permute(
          i.y + vec3(0.0, i1.y, 1.0)
        ) + i.x + vec3(0.0, i1.x, 1.0)
      );

      vec3 m = max(
        0.5 - vec3(
          dot(x0, x0),
          dot(x12.xy, x12.xy),
          dot(x12.zw, x12.zw)
        ),
        0.0
      );

      m *= m;
      m *= m;

      vec3 x = 2.0 * fract(p * C.www) - 1.0;
      vec3 h = abs(x) - 0.5;
      vec3 ox = floor(x + 0.5);
      vec3 a0 = x - ox;

      m *= 1.79284291400159
        - 0.85373472095314 * (a0 * a0 + h * h);

      vec3 g;
      g.x = a0.x * x0.x + h.x * x0.y;
      g.yz = a0.yz * x12.xz + h.yz * x12.yw;

      return 130.0 * dot(m, g);
    }

    float fbm(vec2 p) {
      float n = 0.5 * simplex(p);
      p = mat2(1.6, 1.2, -1.2, 1.6) * p;
      n += 0.25 * simplex(p);
      p = mat2(1.6, 1.2, -1.2, 1.6) * p;
      n += 0.125 * simplex(p);
      return n;
    }

    float bayer2(vec2 p) {
      vec2 q = mod(p, 2.0);
      return 2.0 * q.x + 3.0 * q.y - 4.0 * q.x * q.y;
    }

    float bayer8(vec2 p) {
      return (
        16.0 * bayer2(p)
        + 4.0 * bayer2(floor(p / 2.0))
        + bayer2(floor(p / 4.0))
        + 0.5
      ) / 64.0;
    }

    void main() {
      float aspect = uResolution.x / uResolution.y;
      vec2 p = vUv * vec2(aspect, 1.0) * 2.6;
      float top = 1.0 - vUv.y;

      vec2 pointerDelta = (
        vUv - uPointer
      ) * vec2(aspect, 1.0);

      float distanceToPointer = length(pointerDelta);

      p += pointerDelta
        / max(distanceToPointer, 0.001)
        * 0.15
        * exp(-distanceToPointer * 24.0);

      float t = uNoiseTime * 0.05;
      vec2 warp = vec2(simplex(p * 0.6 + vec2(t * 0.8, 0.0)), simplex(p * 0.6 + vec2(0.0, -t * 0.8) + 7.3));
      float n = fbm(p + vec2(t, -t * 0.7) + warp * 0.45);

      float terminalRegion = smoothstep(
        max(0.0, uHeroEnd - 0.025),
        min(1.0, uHeroEnd + 0.12),
        top
      );

      float heroDensity = 0.03 + 0.12 * smoothstep(-0.1, 0.6, n);

      float fieldDensity = smoothstep(-0.45, 0.55, n) * 0.6 + top * 0.06;

      float density = mix(
        heroDensity,
        fieldDensity,
        terminalRegion
      );

      // Fade the field out at the bottom edge so the stage ends cleanly.
      density *= 1.0 - smoothstep(0.82, 1.0, top);

      float clickAge = uTime - uClickTime;

      if (clickAge >= 0.0 && clickAge < 1.8) {
        float radius = clickAge * 0.55;
        float d = length(
          (vUv - uClickPos) * vec2(aspect, 1.0)
        );

        float ring = 1.0 - smoothstep(
          0.0,
          0.025,
          abs(d - radius)
        );

        density += ring * 0.12 * (1.0 - clickAge / 1.8);
      }

      vec3 ink = uInk;

      if (uState > 0.5 && uState < 1.5) ink = uFail;

      if (uState > 1.5) {
        float edge = uSweep * 1.24
          - 0.12
          + simplex(vec2(vUv.y * 9.0, 4.7)) * 0.065;

        float passMix = 1.0 - smoothstep(edge - 0.07, edge + 0.07, vUv.x);
        ink = bayer8(floor(gl_FragCoord.xy) + vec2(3.0, 5.0)) < passMix ? uPass : uFail;
      }

      float onCell = step(
        bayer8(floor(gl_FragCoord.xy)),
        clamp(density, 0.0, 1.0)
      );

      gl_FragColor = vec4(
        mix(uPaper, ink, onCell),
        1.0
      );

      #include <tonemapping_fragment>
      #include <colorspace_fragment>
    }
  `;

  async function initialize() {
    if (disposed) return;

    try {
      THREE = await import(
        "https://cdn.jsdelivr.net/npm/three@0.186.1/+esm"
      );

      if (disposed) return;

      renderer = new THREE.WebGLRenderer({
        antialias: false,
        alpha: false,
        powerPreference: "low-power",
      });

      renderer.setPixelRatio(1);
      renderer.outputColorSpace = THREE.SRGBColorSpace;
      renderer.toneMapping = THREE.NoToneMapping;
      renderer.domElement.setAttribute("aria-hidden", "true");

      renderer.domElement.addEventListener(
        "webglcontextlost",
        event => {
          event.preventDefault();
          fallback();
        },
      );

      uniforms = {
        uTime: { value: 0 },
        uResolution: { value: new THREE.Vector2(1, 1) },
        uHeroEnd: { value: 0.4 },
        uState: { value: state },
        uSweep: { value: sweep },
        uNoiseTime: { value: 0 },
        uPointer: { value: new THREE.Vector2(-10, -10) },
        uClickPos: { value: new THREE.Vector2(-10, -10) },
        uClickTime: { value: -100 },
        uPaper: { value: new THREE.Color() },
        uInk: { value: new THREE.Color() },
        uFail: { value: new THREE.Color() },
        uPass: { value: new THREE.Color() },
      };

      scene = new THREE.Scene();
      camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);
      geometry = new THREE.PlaneGeometry(2, 2);

      material = new THREE.ShaderMaterial({
        uniforms,
        vertexShader,
        fragmentShader,
        depthTest: false,
        depthWrite: false,
        toneMapped: false,
      });

      scene.add(new THREE.Mesh(geometry, material));
      host.prepend(renderer.domElement);

      readColors();
      syncState();
      resize();

      const rect = host.getBoundingClientRect();
      visible = rect.bottom > 0 && rect.top < innerHeight;
      ready = true;

      if (visible && !document.hidden) {
        render(performance.now());

        if (
          renderer.info.programs?.some(
            program =>
              program.diagnostics &&
              !program.diagnostics.runnable,
          )
        ) {
          throw new Error("Field shader did not compile.");
        }
      }

      host.classList.add("has-webgl");
      if (!reduced) invalidate();

      document.fonts.ready.then(resize);
    } catch {
      fallback();
    }
  }

  // Two frames leave the headline's first paint ahead of the module request.
  requestAnimationFrame(() => requestAnimationFrame(initialize));
})();
