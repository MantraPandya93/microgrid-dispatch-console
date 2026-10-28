// 3D "digital twin" of the microgrid (Three.js). Driven by the same forecast hour as the 2D diagram.
// Loaded as an ES module; if WebGL or the CDN is unavailable the page keeps the 2D diagram.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { CSS2DRenderer, CSS2DObject } from "three/addons/renderers/CSS2DRenderer.js";

const host = document.getElementById("twin");
const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
const coarse = window.matchMedia("(pointer: coarse)").matches;
const C = {
  grid: 0x78b8ff, storage: 0xffad33, solar: 0xf3dd5b, crit: 0xff5d52, ink: 0xecefe8,
  ground: 0x151c18, steel: 0xb4c2cf, panel: 0x2a5d8f, body: 0x1d2420,
};

function webglOK() {
  try { const c = document.createElement("canvas"); return !!(c.getContext("webgl2") || c.getContext("webgl")); }
  catch (_) { return false; }
}
if (!host || !webglOK()) throw new Error("3D twin unavailable");

/* ---------- renderer, camera, controls ---------- */
const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.3;
renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
host.appendChild(renderer.domElement);
renderer.domElement.setAttribute("aria-hidden", "true");

const labels = new CSS2DRenderer();
labels.domElement.className = "twin-labels";
host.appendChild(labels.domElement);

const scene = new THREE.Scene();
scene.fog = new THREE.Fog(0x0b0d0c, 18, 34);
const camera = new THREE.PerspectiveCamera(38, 1.6, 0.1, 100);
camera.position.set(9.5, 7.2, 10.5);

const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0, 0.9, 0);
controls.enableDamping = true;
controls.enablePan = false;
controls.minDistance = 10;
controls.maxDistance = 24;
controls.maxPolarAngle = Math.PI * 0.46;
controls.autoRotate = !reduce.matches;
controls.autoRotateSpeed = 0.6;
controls.enabled = !coarse;   // on phones a drag would fight page scrolling, so it just auto-rotates
controls.addEventListener("start", () => { controls.autoRotate = false; });

/* ---------- lights ---------- */
const hemi = new THREE.HemisphereLight(0xc4d6e8, 0x1a1f1c, 1.1);
scene.add(hemi);
const rim = new THREE.DirectionalLight(0x78b8ff, 0.7);   // cool back light so silhouettes read at night
rim.position.set(-8, 6, -10);
scene.add(rim);
const sun = new THREE.DirectionalLight(0xfff1d6, 1.2);
sun.castShadow = true;
sun.shadow.mapSize.set(1024, 1024);
Object.assign(sun.shadow.camera, { left: -10, right: 10, top: 10, bottom: -10, near: 1, far: 40 });
scene.add(sun, sun.target);
const sunBall = new THREE.Mesh(new THREE.SphereGeometry(0.35, 20, 20), new THREE.MeshBasicMaterial({ color: C.solar }));
scene.add(sunBall);

/* ---------- ground ---------- */
const ground = new THREE.Mesh(new THREE.CircleGeometry(10, 64), new THREE.MeshStandardMaterial({ color: C.ground, roughness: 0.95 }));
ground.rotation.x = -Math.PI / 2;
ground.receiveShadow = true;
scene.add(ground);
const gridHelper = new THREE.GridHelper(20, 20, 0x2a332e, 0x1a201c);
gridHelper.position.y = 0.01;
scene.add(gridHelper);

const std = (color, extra = {}) => new THREE.MeshStandardMaterial({ color, roughness: 0.6, metalness: 0.2, ...extra });
function box(w, h, d, mat, x, y, z, parent = scene) {
  const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), mat);
  m.position.set(x, y, z);
  m.castShadow = m.receiveShadow = true;
  parent.add(m);
  return m;
}
function label(text, cls, obj, y) {
  const div = document.createElement("div");
  div.className = "twin-label " + cls;
  div.textContent = text;
  const l = new CSS2DObject(div);
  l.position.set(0, y, 0);
  obj.add(l);
  return div;
}

/* ---------- AC bus hub ---------- */
const hub = new THREE.Group();
scene.add(hub);
const hubMesh = new THREE.Mesh(new THREE.CylinderGeometry(0.55, 0.65, 0.5, 32), std(0x2b3430, { emissive: C.ink, emissiveIntensity: 0.08 }));
hubMesh.position.y = 0.25; hubMesh.castShadow = true;
hub.add(hubMesh);
const hubRing = new THREE.Mesh(new THREE.TorusGeometry(0.75, 0.04, 8, 48), new THREE.MeshBasicMaterial({ color: C.ink }));
hubRing.rotation.x = Math.PI / 2; hubRing.position.y = 0.52;
hub.add(hubRing);
label("AC BUS", "dim", hub, 1.1);

/* ---------- solar array ---------- */
const solar = new THREE.Group();
solar.position.set(-4.4, 0, -3.4);
scene.add(solar);
const panelMat = std(C.panel, { metalness: 0.6, roughness: 0.25, emissive: C.solar, emissiveIntensity: 0 });
for (let r = 0; r < 2; r++) for (let c = 0; c < 3; c++) {
  const g = new THREE.Group();
  g.position.set((c - 1) * 1.6, 0, (r - 0.5) * 1.5);
  solar.add(g);
  box(0.08, 0.7, 0.08, std(C.steel), 0, 0.35, 0, g);
  const p = box(1.45, 0.06, 0.95, panelMat, 0, 0.78, 0, g);
  p.rotation.x = -0.5;
}
const solarLabel = label("SOLAR", "solar", solar, 2.0);

/* ---------- grid pylon ---------- */
const pylon = new THREE.Group();
pylon.position.set(4.6, 0, -3.6);
scene.add(pylon);
const steel = std(C.steel, { metalness: 0.7, roughness: 0.35 });
const H = 4.4;
[[-1, -1], [1, -1], [1, 1], [-1, 1]].forEach(([sx, sz]) => {
  const leg = new THREE.Mesh(new THREE.CylinderGeometry(0.05, 0.07, H, 6), steel);
  leg.position.set(sx * 0.32, H / 2, sz * 0.32);
  leg.rotation.z = -sx * 0.1; leg.rotation.x = sz * 0.1;
  leg.castShadow = true;
  pylon.add(leg);
});
for (let i = 1; i < 5; i++) box(0.9 - i * 0.1, 0.04, 0.04, steel, 0, i * 0.85, 0, pylon);
box(2.6, 0.08, 0.08, steel, 0, H - 0.35, 0, pylon);
box(1.8, 0.08, 0.08, steel, 0, H - 1.0, 0, pylon);
const wireMat = new THREE.LineBasicMaterial({ color: 0x4a5a68 });
[-1.25, 1.25].forEach((x) => {
  const pts = [new THREE.Vector3(x, H - 0.4, 0), new THREE.Vector3(x + 1.6, H - 0.9, -2.4), new THREE.Vector3(x + 3.2, H - 0.4, -4.8)];
  pylon.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(new THREE.CatmullRomCurve3(pts).getPoints(20)), wireMat));
});
const gridLabel = label("MAIN GRID", "grid", pylon, H + 0.6);

/* ---------- battery container ---------- */
const battery = new THREE.Group();
battery.position.set(-3.8, 0, 3.2);
scene.add(battery);
box(2.6, 1.3, 1.25, std(0x5a4a2c, { roughness: 0.5 }), 0, 0.65, 0, battery);
for (let i = -2; i <= 2; i++) box(0.04, 1.1, 0.02, std(0x6e5b36), i * 0.5, 0.65, 0.635, battery);
box(2.2, 0.16, 0.04, std(0x111111), 0, 1.0, 0.66, battery);
const socBar = box(2.2, 0.16, 0.05, new THREE.MeshBasicMaterial({ color: C.storage }), 0, 1.0, 0.68, battery);
socBar.geometry.translate(1.1, 0, 0);
socBar.position.x = -1.1;
const battLabel = label("BATTERY", "storage", battery, 2.0);

/* ---------- load: small district of buildings with lit windows ---------- */
function windowTexture(seed) {
  const cv = document.createElement("canvas");
  cv.width = 64; cv.height = 128;
  const g = cv.getContext("2d");
  g.fillStyle = "#151b18"; g.fillRect(0, 0, 64, 128);
  let s = seed;
  for (let y = 6; y < 124; y += 12) for (let x = 6; x < 60; x += 14) {
    s = (s * 9301 + 49297) % 233280;
    g.fillStyle = s / 233280 > 0.42 ? "#ffd58a" : "#222a26";
    g.fillRect(x, y, 8, 6);
  }
  const t = new THREE.CanvasTexture(cv);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}
const load = new THREE.Group();
load.position.set(3.8, 0, 3.2);
scene.add(load);
const buildingMats = [];
[[-1.0, -0.4, 0.9, 2.6], [0.1, -0.6, 0.8, 1.7], [1.1, -0.2, 0.9, 2.2], [-0.6, 0.8, 1.0, 1.2], [0.7, 0.9, 0.8, 1.5]]
  .forEach(([x, z, w, h], i) => {
    const tex = windowTexture(i * 77 + 13);
    const mat = new THREE.MeshStandardMaterial({ color: 0xffffff, map: tex, emissive: 0xffffff, emissiveMap: tex, emissiveIntensity: 0.6, roughness: 0.8 });
    buildingMats.push(mat);
    box(w, h, w, mat, x, h / 2, z, load);
  });
const alarm = new THREE.Mesh(new THREE.RingGeometry(2.0, 2.15, 64), new THREE.MeshBasicMaterial({ color: C.crit, transparent: true, opacity: 0, side: THREE.DoubleSide }));
alarm.rotation.x = -Math.PI / 2; alarm.position.y = 0.03;
load.add(alarm);
const loadLabel = label("LOAD", "load", load, 3.2);

/* ---------- cables + energy particles ---------- */
function cable(from, to, color) {
  const a = from.clone(), b = to.clone();
  const mid = a.clone().lerp(b, 0.5); mid.y += 1.1;
  const curve = new THREE.CatmullRomCurve3([a, mid, b]);
  const tube = new THREE.Mesh(new THREE.TubeGeometry(curve, 48, 0.045, 8, false), std(0x2c3531, { emissive: color, emissiveIntensity: 0.05 }));
  scene.add(tube);
  const N = 14;
  const dots = new THREE.InstancedMesh(new THREE.SphereGeometry(0.11, 12, 12), new THREE.MeshBasicMaterial({ color }), N);
  scene.add(dots);
  return { curve, tube, dots, N, kw: 0, dir: 1, phase: Math.random() };
}
const HUB = new THREE.Vector3(0, 0.5, 0);
const cables = {
  solar: cable(new THREE.Vector3(-4.4, 0.6, -2.4), HUB, C.solar),
  grid: cable(new THREE.Vector3(4.6, 3.9, -3.6), HUB, C.grid),
  batt: cable(new THREE.Vector3(-3.8, 1.3, 2.6), HUB, C.storage),
  load: cable(HUB, new THREE.Vector3(3.8, 0.6, 2.4), C.ink),
};
const breaker = box(0.32, 0.32, 0.32, new THREE.MeshStandardMaterial({ color: C.grid, emissive: C.grid, emissiveIntensity: 0.6 }), 0, 0, 0);
breaker.position.copy(cables.grid.curve.getPoint(0.35));

const tmp = new THREE.Object3D();
function placeDots(c, t) {
  const on = c.kw > 0.05;
  c.dots.visible = on;
  c.tube.material.emissiveIntensity = on ? 0.35 : 0.03;
  if (!on) return;
  const speed = 0.08 + Math.min(0.5, c.kw / 900);
  for (let i = 0; i < c.N; i++) {
    let u = (i / c.N + c.phase + t * speed) % 1;
    if (c.dir < 0) u = 1 - u;
    tmp.position.copy(c.curve.getPoint(u));
    const s = 0.6 + Math.min(1.2, c.kw / 200);
    tmp.scale.setScalar(s);
    tmp.updateMatrix();
    c.dots.setMatrixAt(i, tmp.matrix);
  }
  c.dots.instanceMatrix.needsUpdate = true;
}

/* ---------- state from the dashboard ---------- */
const sky = { night: new THREE.Color(0x060809), day: new THREE.Color(0x10202c) };
let overload = false;
const fmt = (n) => Number(n).toFixed(1);

function update(h, f) {
  cables.solar.kw = f.solar; cables.solar.dir = 1;
  cables.grid.kw = f.grid; cables.grid.dir = 1;
  cables.batt.kw = Math.abs(f.batt); cables.batt.dir = f.batt < 0 ? 1 : -1;  // discharging flows to the bus
  cables.load.kw = f.load; cables.load.dir = 1;
  const storage = h.mode === "Renewable Storage Mode";
  breaker.material.color.set(storage ? 0x3a4640 : C.grid);
  breaker.material.emissiveIntensity = storage ? 0 : 0.6;

  const irr = Math.max(0, Math.min(1, (h.solar_irradiance_wm2 || 0) / 900));
  panelMat.emissiveIntensity = irr * 0.35;
  const ang = ((h.hour - 6) / 12) * Math.PI;              // sunrise 06:00 -> sunset 18:00
  const up = Math.sin(ang);
  sun.position.set(Math.cos(ang) * 12, Math.max(0.5, up * 10), -4);
  sun.intensity = 0.6 + irr * 2.2;
  sunBall.visible = up > 0;
  sunBall.position.set(Math.cos(ang) * 9, Math.max(0.5, up * 6.5), -7);
  hemi.intensity = 0.9 + irr * 0.6;
  scene.background = null;
  host.style.setProperty("--twin-sky", "#" + sky.night.clone().lerp(sky.day, irr).getHexString());
  buildingMats.forEach((m) => (m.emissiveIntensity = 0.35 + (1 - irr) * 0.6));

  socBar.scale.x = Math.max(0.01, Math.min(1, h.soc_pct / 100));
  socBar.material.color.set(h.battery_low ? C.crit : C.storage);
  overload = !!h.overload;
  alarm.material.opacity = overload ? 0.9 : 0;

  solarLabel.textContent = "SOLAR · " + fmt(f.solar) + " kW";
  gridLabel.textContent = storage ? "MAIN GRID · OPEN" : "MAIN GRID · " + fmt(f.grid) + " kW";
  battLabel.textContent = "BATTERY · " + Math.round(h.soc_pct) + "% " + (f.batt < -0.05 ? "▼" : f.batt > 0.05 ? "▲" : "");
  loadLabel.textContent = "LOAD · " + fmt(h.demand_kw) + " kW";
  loadLabel.classList.toggle("crit", overload);
  if (!running) frame(performance.now());   // static render when not animating
}

/* ---------- sizing + loop ---------- */
function resize() {
  const w = host.clientWidth, h = host.clientHeight;
  if (!w || !h) return;
  renderer.setSize(w, h, false);
  labels.setSize(w, h);
  camera.aspect = w / h;
  camera.fov = w < 480 ? 60 : 38;   // wider lens on phones so all four assets and labels fit
  camera.updateProjectionMatrix();
}
new ResizeObserver(() => { resize(); if (!running) frame(performance.now()); }).observe(host);

let running = false, visible = true, inFrame = false, t0 = performance.now();
function frame(now) {
  if (inFrame) return;   // controls.update() can emit "change", whose listener calls frame() again
  inFrame = true;
  try { draw(now); } finally { inFrame = false; }
}
function draw(now) {
  const t = reduce.matches ? 0.37 : (now - t0) / 1000;
  Object.values(cables).forEach((c) => placeDots(c, t));
  if (overload && !reduce.matches) alarm.material.opacity = 0.45 + 0.45 * Math.abs(Math.sin(t * 3));
  hubRing.rotation.z = reduce.matches ? 0 : t * 0.6;
  controls.update();
  renderer.render(scene, camera);
  labels.render(scene, camera);
}
function loop(now) {
  if (!running) return;
  frame(now);
  requestAnimationFrame(loop);
}
function setRunning() {
  const want = visible && !document.hidden && !host.hidden && !reduce.matches;
  if (want && !running) { running = true; requestAnimationFrame(loop); }
  else if (!want) running = false;
}
new IntersectionObserver((e) => { visible = e[0].isIntersecting; setRunning(); }).observe(host);
document.addEventListener("visibilitychange", setRunning);
reduce.addEventListener("change", () => { controls.autoRotate = !reduce.matches; setRunning(); frame(performance.now()); });
controls.addEventListener("change", () => { if (!running) frame(performance.now()); });

resize();
// note: not window.twin; the element id "twin" already owns that global name
window.mgTwin = { update, refresh: () => { resize(); setRunning(); frame(performance.now()); } };
document.dispatchEvent(new CustomEvent("twin-ready"));
