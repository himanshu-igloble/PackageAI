// Three.js mesh viewer with risk-zone tinting overlay.
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";

let scene, camera, renderer, controls, currentMesh = null;

export function initViewer(container) {
  scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0f1115);

  const w = container.clientWidth, h = container.clientHeight;
  camera = new THREE.PerspectiveCamera(45, w / h, 0.1, 5000);
  camera.position.set(180, 180, 260);

  renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setSize(w, h);
  renderer.setPixelRatio(window.devicePixelRatio);
  container.appendChild(renderer.domElement);

  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;

  const ambient = new THREE.AmbientLight(0xffffff, 0.5);
  scene.add(ambient);
  const dir = new THREE.DirectionalLight(0xffffff, 0.8);
  dir.position.set(200, 300, 400);
  scene.add(dir);

  const grid = new THREE.GridHelper(400, 20, 0x232a36, 0x232a36);
  scene.add(grid);

  window.addEventListener("resize", () => onResize(container));
  animate();
}

function onResize(container) {
  if (!renderer) return;
  const w = container.clientWidth, h = container.clientHeight;
  renderer.setSize(w, h);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}

function animate() {
  requestAnimationFrame(animate);
  controls && controls.update();
  renderer && renderer.render(scene, camera);
}

/**
 * Gentle auto-rotation when a mesh first loads — a one-shot hint that the
 * model is interactive. Stops on any pointer interaction.
 */
let autoRotateTimer = null;
function nudgeAutoRotate() {
  if (!currentMesh || !controls) return;
  if (autoRotateTimer) clearTimeout(autoRotateTimer);
  controls.autoRotate = true;
  controls.autoRotateSpeed = 0.8;
  autoRotateTimer = setTimeout(() => { controls.autoRotate = false; }, 1800);
  const stopOnInteract = () => {
    controls.autoRotate = false;
    if (autoRotateTimer) clearTimeout(autoRotateTimer);
    renderer?.domElement?.removeEventListener("pointerdown", stopOnInteract);
  };
  renderer?.domElement?.addEventListener("pointerdown", stopOnInteract, { once: true });
}

export async function loadGlb(url) {
  if (currentMesh) {
    scene.remove(currentMesh);
    currentMesh.traverse?.(o => o.geometry?.dispose?.());
    currentMesh = null;
  }
  return new Promise((resolve, reject) => {
    const loaderUrl = url.startsWith("blob:")
      ? url
      : url.replace(/[?&]t=\d+/, '') + "?t=" + Date.now();
    new GLTFLoader().load(loaderUrl, gltf => {
      const root = gltf.scene;
      // Center
      const box = new THREE.Box3().setFromObject(root);
      const center = box.getCenter(new THREE.Vector3());
      root.position.sub(center);
      // Default material so we can recolor by zone or vertex
      root.traverse(obj => {
        if (obj.isMesh) {
          obj.material = new THREE.MeshStandardMaterial({
            color: 0x9ca3af, metalness: 0.0, roughness: 0.6,
            transparent: true, opacity: 0.92,
            vertexColors: true,
          });
        }
      });
      scene.add(root);
      currentMesh = root;
      // Frame
      const size = box.getSize(new THREE.Vector3()).length();
      camera.position.set(size * 0.9, size * 0.7, size * 1.1);
      controls.target.set(0, 0, 0);
      controls.update();
      // Subtle entrance: scale-up + auto-rotate so the user knows it's draggable
      root.scale.set(0.001, 0.001, 0.001);
      if (window.gsap) {
        window.gsap.to(root.scale, { x: 1, y: 1, z: 1, duration: 0.45, ease: "back.out(1.7)" });
      } else {
        root.scale.set(1, 1, 1);
      }
      nudgeAutoRotate();
      resolve(root);
    }, undefined, reject);
  });
}

// Cache of vertex counts per mesh so we can validate per_vertex_color length
let currentVertexCount = 0;

/**
 * High-resolution heatmap rendering: apply a per-vertex color buffer that
 * came pre-computed from the backend (`per_vertex_color`, uint8 RGB triples).
 * The mesh GLB itself is unchanged; we only swap the vertex color attribute.
 */
export function applyVertexColors(perVertexColor) {
  if (!currentMesh || !Array.isArray(perVertexColor)) return false;
  let applied = false;
  currentMesh.traverse(obj => {
    if (!obj.isMesh) return;
    const geom = obj.geometry;
    const nv = geom.attributes.position.count;
    // If the payload was computed for a different mesh size, give up gracefully
    if (perVertexColor.length !== nv) {
      console.warn(`vertex color length mismatch: got ${perVertexColor.length}, mesh has ${nv}`);
      return;
    }
    const colors = new Float32Array(nv * 3);
    for (let i = 0; i < nv; i++) {
      const c = perVertexColor[i] || [200, 200, 200];
      colors[i * 3 + 0] = c[0] / 255;
      colors[i * 3 + 1] = c[1] / 255;
      colors[i * 3 + 2] = c[2] / 255;
    }
    geom.setAttribute("color", new THREE.BufferAttribute(colors, 3));
    obj.material.vertexColors = true;
    obj.material.transparent = false;
    obj.material.opacity = 1.0;
    obj.material.needsUpdate = true;
    applied = true;
  });
  return applied;
}

// ────────────────────────────────────────────────────────────────────────
// Multi-viewer factory: a self-contained Three.js scene per call. Used by
// the side-by-side ISTA viewers (top / bottom / side) and by the per-variant
// optimisation comparison panel. Each instance manages its own camera,
// renderer, controls, and current mesh.
// ────────────────────────────────────────────────────────────────────────

export function makeMiniViewer(container, { autoRotate = true } = {}) {
  console.log("[3D-DEBUG] makeMiniViewer | container:", container?.id, "| clientW:", container?.clientWidth, "| offsetW:", container?.offsetWidth);
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0xfafafa);

  const w = container.clientWidth || container.offsetWidth || 300, h = container.clientHeight || 240;
  const camera = new THREE.PerspectiveCamera(45, w / h, 0.1, 5000);
  camera.position.set(180, 180, 260);

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setSize(w, h);
  renderer.setPixelRatio(window.devicePixelRatio);
  container.appendChild(renderer.domElement);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  if (autoRotate) {
    controls.autoRotate = true;
    controls.autoRotateSpeed = 0.6;
    container.addEventListener("pointerdown", () => { controls.autoRotate = false; },
      { once: true, passive: true });
  }

  scene.add(new THREE.AmbientLight(0xffffff, 0.6));
  const dir = new THREE.DirectionalLight(0xffffff, 0.7);
  dir.position.set(200, 300, 400);
  scene.add(dir);

  let mesh = null;

  function animate() {
    requestAnimationFrame(animate);
    controls.update();
    renderer.render(scene, camera);
  }
  animate();

  function _onResize() {
    const W = container.clientWidth, H = container.clientHeight || 240;
    console.log(`[3D-DEBUG] _onResize ${container.id}: clientW=${W} clientH=${container.clientHeight} offsetW=${container.offsetWidth}`);
    if (W <= 0) return;       // container hidden — wait until visible
    renderer.setSize(W, H);
    camera.aspect = W / H;
    camera.updateProjectionMatrix();
    console.log(`[3D-DEBUG] _onResize ${container.id}: renderer resized to ${W}x${H}`);
  }
  window.addEventListener("resize", _onResize);
  // Also observe the container directly so we re-size when the parent stage
  // becomes display:block (initial size of 0 → real size on first paint).
  if (typeof ResizeObserver !== "undefined") {
    new ResizeObserver((entries) => {
      const e = entries[0];
      console.log(`[3D-DEBUG] ResizeObserver fired for ${container.id}: contentRect ${e.contentRect.width}x${e.contentRect.height}`);
      _onResize();
    }).observe(container);
  }

  return {
    resize: _onResize,
    async loadGlb(url) {
      if (mesh) {
        scene.remove(mesh);
        mesh.traverse?.(o => o.geometry?.dispose?.());
        mesh = null;
      }
      return new Promise((resolve, reject) => {
        // Blob URLs cannot have query parameters — skip cache-busting for them.
        const loaderUrl = url.startsWith("blob:")
          ? url
          : url.replace(/[?&]t=\d+/, '') + "?t=" + Date.now();
        new GLTFLoader().load(loaderUrl, gltf => {
          const root = gltf.scene;
          const box = new THREE.Box3().setFromObject(root);
          const center = box.getCenter(new THREE.Vector3());
          root.position.sub(center);
          root.traverse(obj => {
            if (obj.isMesh) {
              obj.material = new THREE.MeshStandardMaterial({
                color: 0x9ca3af, metalness: 0.0, roughness: 0.6,
                vertexColors: true,
              });
            }
          });
          scene.add(root);
          mesh = root;
          const size = box.getSize(new THREE.Vector3()).length();
          camera.position.set(size * 0.9, size * 0.7, size * 1.1);
          controls.target.set(0, 0, 0);
          controls.update();
          resolve(root);
        }, undefined, reject);
      });
    },
    applyVertexColors(perVertexColor) {
      if (!mesh || !Array.isArray(perVertexColor)) return false;
      let applied = false;
      mesh.traverse(obj => {
        if (!obj.isMesh) return;
        const geom = obj.geometry;
        const nv = geom.attributes.position.count;
        if (perVertexColor.length !== nv) return;
        const colors = new Float32Array(nv * 3);
        for (let i = 0; i < nv; i++) {
          const c = perVertexColor[i] || [200, 200, 200];
          colors[i * 3] = c[0] / 255;
          colors[i * 3 + 1] = c[1] / 255;
          colors[i * 3 + 2] = c[2] / 255;
        }
        geom.setAttribute("color", new THREE.BufferAttribute(colors, 3));
        obj.material.vertexColors = true;
        obj.material.needsUpdate = true;
        applied = true;
      });
      return applied;
    },
    dispose() {
      window.removeEventListener("resize", _onResize);
      renderer.dispose();
      container.removeChild(renderer.domElement);
    },
  };
}


/**
 * Render the FEA jet colormap as a vertical gradient onto the colorbar canvas.
 */
export function paintColorbar(canvas, lut) {
  if (!canvas || !lut || !lut.length) return;
  const ctx = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height;
  const img = ctx.createImageData(w, h);
  for (let y = 0; y < h; y++) {
    // Top of canvas = peak stress (last lut row), bottom = 0
    const idx = Math.round((1 - y / (h - 1)) * (lut.length - 1));
    const c = lut[idx];
    for (let x = 0; x < w; x++) {
      const p = (y * w + x) * 4;
      img.data[p + 0] = c[0];
      img.data[p + 1] = c[1];
      img.data[p + 2] = c[2];
      img.data[p + 3] = 255;
    }
  }
  ctx.putImageData(img, 0, 0);
}

// ---- legacy 4-zone overlay (kept for backwards compatibility) -----------
const ZONE_BANDS = {
  base:      { axis: "z", lo: 0.00, hi: 0.18 },
  side_wall: { axis: "z", lo: 0.18, hi: 0.65 },
  shoulder:  { axis: "z", lo: 0.65, hi: 0.85 },
  neck:      { axis: "z", lo: 0.85, hi: 1.00 },
};

export function applyRiskColors(zoneOverlays) {
  if (!currentMesh) return;
  const zoneByName = Object.fromEntries(zoneOverlays.map(z => [z.zone, z]));

  currentMesh.traverse(obj => {
    if (!obj.isMesh) return;
    const geom = obj.geometry;
    geom.computeBoundingBox();
    const bb = geom.boundingBox;
    const minZ = bb.min.z, maxZ = bb.max.z;
    const span = maxZ - minZ || 1;
    const colors = new Float32Array(geom.attributes.position.count * 3);

    const pos = geom.attributes.position.array;
    for (let i = 0; i < geom.attributes.position.count; i++) {
      const z = pos[i * 3 + 2];
      const t = (z - minZ) / span;
      let zone = "side_wall";
      for (const [name, band] of Object.entries(ZONE_BANDS)) {
        if (t >= band.lo && t <= band.hi) { zone = name; break; }
      }
      // Blend zone color toward the overlay color
      const ov = zoneByName[zone];
      const c = new THREE.Color(ov?.color || "#9ca3af");
      colors[i * 3 + 0] = c.r;
      colors[i * 3 + 1] = c.g;
      colors[i * 3 + 2] = c.b;
    }
    geom.setAttribute("color", new THREE.BufferAttribute(colors, 3));
    obj.material.needsUpdate = true;
  });
}
