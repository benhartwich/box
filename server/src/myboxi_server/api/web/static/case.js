// Box gestalten: live 3D preview of the configured case (docs/gehaeuse.md).
// The server builds the meshes (hardware/case); this only draws them. Without WebGL the page
// keeps working: the form, the example image and the download.
import * as THREE from './vendor/three-0.186.1/three.module.js';
import { OrbitControls } from './vendor/three-0.186.1/OrbitControls.js';

document.documentElement.classList.add('js');

const form = document.getElementById('case-form');
const stage = document.getElementById('stage');
const note = document.getElementById('viewer-note');
const errorBox = document.getElementById('case-error');
const download = document.getElementById('case-download');
const order = document.getElementById('case-order');
const defaults = JSON.parse(form.dataset.defaults);
const suggested = JSON.parse(form.dataset.suggested);
let coloursChosen = [...new URLSearchParams(location.search).keys()].some((k) => k.startsWith('color_'));
const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

const EXPLODE_MM = 45;
const HIDDEN_WHEN_INSIDE = new Set(['body', 'body_inlay', 'front', 'front_inlay']);

function query() {
  const params = new URLSearchParams();
  for (const [key, value] of new FormData(form)) {
    // Until someone picks colours, the server uses the colours suggested for the form.
    if (key.startsWith('color_') && !coloursChosen) continue;
    const text = String(value).trim();
    if (text !== '' && text !== String(defaults[key])) params.set(key, text);
  }
  return params.toString();
}

function say(text) {
  note.textContent = text;
  note.hidden = !text;
}

// --- preview format (hardware/case export.preview) ---------------------------------------------

function decode(buffer) {
  const view = new DataView(buffer);
  const magic = String.fromCharCode(...new Uint8Array(buffer, 0, 4));
  if (magic !== 'MBXP') throw new Error('unknown preview format');
  const headerLength = view.getUint32(4, true);
  const info = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, 8, headerLength)));
  let offset = 8 + headerLength;
  const meshes = info.meshes.map((meta) => {
    const raw = new Int16Array(buffer, offset, meta.vertices * 3);
    offset += meta.vertices * 6;
    offset += (4 - (offset % 4)) % 4;
    const index = new Uint32Array(buffer, offset, meta.triangles * 3);
    offset += meta.triangles * 12;
    const position = new Float32Array(raw.length);
    for (let i = 0; i < raw.length; i++) position[i] = raw[i] * info.step;
    return { meta, position, index: new Uint32Array(index) };
  });
  return { info, meshes };
}

// --- scene -------------------------------------------------------------------------------------

let renderer;
try {
  renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
} catch {
  renderer = null;
}

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(32, 4 / 3, 1, 5000);
// The generator uses z up; three.js uses y up. The case group turns z into y.
const caseGroup = new THREE.Group();
caseGroup.rotation.x = -Math.PI / 2;
scene.add(caseGroup);
scene.add(new THREE.HemisphereLight(0xffffff, 0xd9cfbd, 2.1));
const sun = new THREE.DirectionalLight(0xffffff, 2.3);
sun.position.set(160, 300, 220);
sun.castShadow = true;
sun.shadow.mapSize.set(1024, 1024);
sun.shadow.camera.left = -200;
sun.shadow.camera.right = 200;
sun.shadow.camera.top = 200;
sun.shadow.camera.bottom = -200;
scene.add(sun);
const ground = new THREE.Mesh(new THREE.PlaneGeometry(1200, 1200), new THREE.ShadowMaterial({ opacity: 0.16 }));
ground.rotation.x = -Math.PI / 2;
ground.receiveShadow = true;
scene.add(ground);

let controls = null;
let parts = [];
let explode = 0;
let explodeTarget = 0;
let inside = false;
let framed = false;

function setupRenderer() {
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFShadowMap;
  stage.prepend(renderer.domElement);
  stage.classList.add('live');
  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.enablePan = false;
  controls.minDistance = 150;
  controls.maxDistance = 900;
  controls.maxPolarAngle = Math.PI * 0.49;
  controls.autoRotate = !reduceMotion;
  controls.autoRotateSpeed = 0.8;
  controls.addEventListener('start', () => { controls.autoRotate = false; });
  // Zooming by hand ends the automatic distance for the exploded view.
  renderer.domElement.addEventListener('wheel', () => { baseDistance = 0; }, { passive: true });
  new ResizeObserver(resize).observe(stage);
  resize();
  renderer.setAnimationLoop(frame);
}

function resize() {
  const { clientWidth: w, clientHeight: h } = stage;
  if (!w || !h) return;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}

let baseDistance = 0;

function frame() {
  explode += (explodeTarget - explode) * 0.15;
  if (baseDistance) {
    const want = baseDistance * (1 + 0.35 * explode);
    const offset = camera.position.clone().sub(controls.target);
    offset.setLength(offset.length() + (want - offset.length()) * 0.15);
    camera.position.copy(controls.target).add(offset);
  }
  for (const part of parts) {
    const e = part.userData.explode;
    part.position.set(e[0] * explode, e[1] * explode, e[2] * explode);
  }
  controls.update();
  renderer.render(scene, camera);
}

function frameCamera(size) {
  const [w, d, h] = size;
  const radius = Math.hypot(w, d, h) / 2;
  controls.target.set(0, h / 2, 0);
  const distance = radius / Math.sin(THREE.MathUtils.degToRad(camera.fov / 2)) * 1.05;
  camera.position.set(distance * 0.55, h / 2 + distance * 0.42, distance * 0.72);
  baseDistance = camera.position.distanceTo(controls.target);
  controls.update();
}

function show({ info, meshes }) {
  for (const part of parts) {
    part.geometry.dispose();
    part.material.dispose();
    caseGroup.remove(part);
  }
  parts = [];
  const [w, d] = info.size;
  caseGroup.position.set(-w / 2, 0, d / 2);
  for (const { meta, position, index } of meshes) {
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(position, 3));
    geometry.setIndex(new THREE.BufferAttribute(index, 1));
    const component = meta.kind === 'component';
    const material = new THREE.MeshStandardMaterial({
      color: new THREE.Color(meta.color),
      roughness: component ? 0.55 : 0.78,
      metalness: 0,
      flatShading: true,
      polygonOffset: meta.kind === 'inlay',
      polygonOffsetFactor: -1,
    });
    const mesh = new THREE.Mesh(geometry, material);
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    mesh.userData = { key: meta.key, kind: meta.kind, explode: meta.explode.map((v) => v * EXPLODE_MM) };
    caseGroup.add(mesh);
    parts.push(mesh);
  }
  applyInside();
  if (!framed) {
    frameCamera(info.size);
    framed = true;
  }
}

function applyInside() {
  for (const part of parts) {
    if (part.userData.kind === 'component') {
      part.visible = inside;
    } else if (HIDDEN_WHEN_INSIDE.has(part.userData.key)) {
      part.material.transparent = inside;
      part.material.opacity = inside ? 0.14 : 1;
      part.material.depthWrite = !inside;
      part.material.needsUpdate = true;
      part.castShadow = !inside;
    }
  }
}

// --- updates -----------------------------------------------------------------------------------

let pending = null;
let timer = 0;

async function refresh() {
  const q = query();
  const suffix = q ? `?${q}` : '';
  history.replaceState(null, '', `/gestalten${suffix}`);
  download.href = `/gestalten/download.zip${suffix}`;
  if (order) order.href = `/gestalten/anfrage${suffix}`;
  if (!renderer) return;
  pending?.abort();
  pending = new AbortController();
  say('Vorschau wird gebaut …');
  try {
    const response = await fetch(`${stage.dataset.preview}${suffix}`, { signal: pending.signal });
    if (!response.ok) {
      const text = await response.text();
      errorBox.textContent = text;
      errorBox.hidden = false;
      download.setAttribute('aria-disabled', 'true');
      say('');
      return;
    }
    errorBox.hidden = true;
    download.removeAttribute('aria-disabled');
    show(decode(await response.arrayBuffer()));
    say('');
  } catch (error) {
    if (error.name !== 'AbortError') say('Vorschau gerade nicht möglich.');
  }
}

function suggestColours(shape) {
  for (const [name, value] of Object.entries(suggested[shape] || {})) {
    const input = form.querySelector(`input[name="${name}"][value="${value}"]`);
    if (input) input.checked = true;
  }
}

form.addEventListener('input', (event) => {
  const name = event.target.name;
  if (name.startsWith('color_')) coloursChosen = true;
  if (name === 'form') {
    framed = false;
    if (!coloursChosen) suggestColours(event.target.value);
  }
  window.clearTimeout(timer);
  timer = window.setTimeout(refresh, event.target.name === 'name' ? 450 : 120);
});
form.addEventListener('submit', (event) => {
  event.preventDefault();
  refresh();
});

for (const button of document.querySelectorAll('[data-toggle]')) {
  button.addEventListener('click', () => {
    const on = button.getAttribute('aria-pressed') !== 'true';
    button.setAttribute('aria-pressed', String(on));
    if (button.dataset.toggle === 'explode') explodeTarget = on ? 1 : 0;
    if (button.dataset.toggle === 'inside') {
      inside = on;
      applyInside();
    }
  });
}

if (renderer) {
  setupRenderer();
  refresh();
} else {
  say('3D-Vorschau braucht WebGL. Die Druckdateien kannst du trotzdem laden.');
}
