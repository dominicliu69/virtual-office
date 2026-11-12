// Post chain: the scene (MSAA, half-float, alpha kept) -> tilt-shift "miniature" blur (sharp band around the active
// floor, soft toward the top and bottom edges; `?tiltshift=0` leaves it out) -> OutputPass (tone mapping + sRGB) ->
// the studio pass (studio.js: white backdrop + the building's soft shadow aura), which is what reaches the screen.
import * as THREE from 'three';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { ShaderPass } from 'three/addons/postprocessing/ShaderPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { StudioPass } from './studio.js';
import { renderSettings } from './render-settings.js';

const TiltShiftShader = {
  name: 'BotHQTiltShift',
  uniforms: {
    tDiffuse: { value: null },
    dir: { value: new THREE.Vector2(1, 0) },     // texel step (1/w, 0) or (0, 1/h)
    focus: { value: 0.5 },                      // uv.y of the sharp line
    band: { value: 0.24 },                       // half height of the fully sharp band (uv)
    falloff: { value: 0.5 },                   // uv distance over which the blur ramps up
    strength: { value: 1.5 },                   // blur radius in px at full effect
  },
  vertexShader: /* glsl */`
    varying vec2 vUv;
    void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4( position, 1.0 ); }`,
  fragmentShader: /* glsl */`
    uniform sampler2D tDiffuse;
    uniform vec2 dir;
    uniform float focus, band, falloff, strength;
    varying vec2 vUv;
    void main() {
      float d = max( 0.0, abs( vUv.y - focus ) - band );
      float amt = smoothstep( 0.0, falloff, d );
      vec2 s = dir * strength * amt;
      vec4 c = texture2D( tDiffuse, vUv ) * 0.2270270270;
      c += texture2D( tDiffuse, vUv + s * 1.3846153846 ) * 0.3162162162;
      c += texture2D( tDiffuse, vUv - s * 1.3846153846 ) * 0.3162162162;
      c += texture2D( tDiffuse, vUv + s * 3.2307692308 ) * 0.0702702703;
      c += texture2D( tDiffuse, vUv - s * 3.2307692308 ) * 0.0702702703;
      gl_FragColor = c;
    }`,
};

export function tiltShiftEnabled() {
  return new URLSearchParams(location.search).get('tiltshift') !== '0';
}

function halfFloatTarget(w, h, samples) {
  return new THREE.WebGLRenderTarget(w, h, { type: THREE.HalfFloatType, samples });
}

function pinSceneTarget(composer) {
  composer.readBuffer = composer.renderTarget1;
  composer.writeBuffer = composer.renderTarget2;
}

export function createPost(renderer, scene, camera) {
  const size = renderer.getSize(new THREE.Vector2());
  const pr = renderer.getPixelRatio();
  const q = renderSettings();
  let samples = q.aa === 2 || q.aa === 4 ? q.aa : 0;
  const pxW = size.x * pr, pxH = size.y * pr;
  // The scene pass draws into writeBuffer. That buffer carries the MSAA samples; the other one stays plain.
  const rt = halfFloatTarget(pxW, pxH, samples);
  const composer = new EffectComposer(renderer, rt);
  composer.setPixelRatio(pr);
  composer.renderTarget2.dispose();
  composer.renderTarget2 = halfFloatTarget(pxW, pxH, 0);
  // r170 RenderPass draws the scene into readBuffer and does not swap. Samples belong on that target.
  pinSceneTarget(composer);
  composer.addPass(new RenderPass(scene, camera));
  const h = new ShaderPass(TiltShiftShader);
  const v = new ShaderPass(TiltShiftShader);      // ShaderPass clones the uniforms, so each pass owns its own
  h.enabled = v.enabled = q.tilt;
  composer.addPass(h);
  composer.addPass(v);
  composer.addPass(new OutputPass());
  const studio = new StudioPass(scene, camera);
  studio.aura = q.aura;
  composer.addPass(studio);

  const api = {
    composer,
    setSize(w, hgt) {
      composer.setPixelRatio(renderer.getPixelRatio());
      composer.setSize(w, hgt);
      const pxW = w * renderer.getPixelRatio(), pxH = hgt * renderer.getPixelRatio();
      h.uniforms.dir.value.set(1 / pxW, 0);
      v.uniforms.dir.value.set(0, 1 / pxH);
      this.base = 1.05 * renderer.getPixelRatio();
      h.uniforms.strength.value = v.uniforms.strength.value = this.base;
    },
    render(focusV, dt, intensity = 1) {
      h.uniforms.focus.value = v.uniforms.focus.value = focusV;
      h.uniforms.strength.value = v.uniforms.strength.value = this.base * intensity;
      pinSceneTarget(composer);
      composer.render(dt);
      if (window.bothq) window.bothq.postSamples = composer.readBuffer.samples;
    },
    setSamples(n) {
      n = n === 2 || n === 4 ? n : 0;
      if (n === samples) return;
      samples = n;
      const old = composer.renderTarget1;
      const next = halfFloatTarget(old.width, old.height, n);
      composer.renderTarget1 = next;
      if (composer.writeBuffer === old) composer.writeBuffer = next;
      if (composer.readBuffer === old) composer.readBuffer = next;
      old.dispose();
    },
    setTilt(on) { h.enabled = v.enabled = !!on; },
    setAura(on) { studio.aura = !!on; },
  };
  api.setSize(size.x, size.y);
  return api;
}
