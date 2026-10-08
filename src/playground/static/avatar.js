// Mouse-Man on the welcome and conversation screens: the character from
// HuRI_website_demo.
//
// Two ways he moves, picked per session by what HuRI can run:
//   * gestures: HuRI's `mov` module (EMAGE) streams SMPL-X motion, retargeted
//     onto the Mixamo rig in step with the voice (see HuRI_website_demo's
//     Character.jsx, which this follows);
//   * idle: without `mov`, breathing and head sway, nodding with the voice.
//
// The model is a 30 MB FBX kept out of git. Without it at MODEL_URL the page
// stays text only. It (Mixamo "Ch14") has no jaw bone and no blend shapes, so
// its mouth cannot open; a model with ARKit-style shapes (jawOpen,
// eyeBlinkLeft/Right) gets a talking mouth and blinks.
import * as THREE from "three";
import { FBXLoader } from "three/addons/loaders/FBXLoader.js";

const MODEL_URL = "/static/avatar/model.fbx";

// Voice loudness (RMS) -> talking cues (0..1).
const MOUTH_GAIN = 7;
const MOUTH_FLOOR = 0.01; // below this it is silence or breath
const MOUTH_SMOOTH_S = 0.05;

// Bones ease toward their target pose: hides seams between gesture chunks
// (EMAGE windows are separate inferences) and the switch to and from idle.
const GESTURE_SMOOTH_S = 0.06;
const IDLE_SMOOTH_S = 0.3;
// Gestures are sampled this far ahead so, once eased, they land on time.
const GESTURE_LEAD_S = GESTURE_SMOOTH_S;
// Past the last gesture frame by this much, he goes back to idling.
const GESTURE_HOLD_S = 0.3;

// SMPL-X joint order: poses[:, i*3 : i*3+3] is joint i, axis-angle.
const SMPLX_JOINTS = [
  "pelvis", "left_hip", "right_hip", "spine1", "left_knee", "right_knee",
  "spine2", "left_ankle", "right_ankle", "spine3", "left_foot", "right_foot",
  "neck", "left_collar", "right_collar", "head", "left_shoulder",
  "right_shoulder", "left_elbow", "right_elbow", "left_wrist", "right_wrist",
  "jaw", "left_eye", "right_eye",
  "left_index1", "left_index2", "left_index3",
  "left_middle1", "left_middle2", "left_middle3",
  "left_pinky1", "left_pinky2", "left_pinky3",
  "left_ring1", "left_ring2", "left_ring3",
  "left_thumb1", "left_thumb2", "left_thumb3",
  "right_index1", "right_index2", "right_index3",
  "right_middle1", "right_middle2", "right_middle3",
  "right_pinky1", "right_pinky2", "right_pinky3",
  "right_ring1", "right_ring2", "right_ring3",
  "right_thumb1", "right_thumb2", "right_thumb3",
];
const POSE_DIM = 165;
const EXPR_DIM = 100;

// [SMPL-X joint, its parent, Mixamo bone], parents first.
const BODY = [
  ["pelvis", null, "Hips"],
  ["left_hip", "pelvis", "LeftUpLeg"],
  ["right_hip", "pelvis", "RightUpLeg"],
  ["spine1", "pelvis", "Spine"],
  ["left_knee", "left_hip", "LeftLeg"],
  ["right_knee", "right_hip", "RightLeg"],
  ["spine2", "spine1", "Spine1"],
  ["left_ankle", "left_knee", "LeftFoot"],
  ["right_ankle", "right_knee", "RightFoot"],
  ["spine3", "spine2", "Spine2"],
  ["left_foot", "left_ankle", "LeftToeBase"],
  ["right_foot", "right_ankle", "RightToeBase"],
  ["neck", "spine3", "Neck"],
  ["left_collar", "spine3", "LeftShoulder"],
  ["right_collar", "spine3", "RightShoulder"],
  ["head", "neck", "Head"],
  ["left_shoulder", "left_collar", "LeftArm"],
  ["right_shoulder", "right_collar", "RightArm"],
  ["left_elbow", "left_shoulder", "LeftForeArm"],
  ["right_elbow", "right_shoulder", "RightForeArm"],
  ["left_wrist", "left_elbow", "LeftHand"],
  ["right_wrist", "right_elbow", "RightHand"],
];
for (const side of ["left", "right"]) {
  const Side = side === "left" ? "Left" : "Right";
  for (const [finger, Finger] of [
    ["thumb", "Thumb"], ["index", "Index"], ["middle", "Middle"],
    ["ring", "Ring"], ["pinky", "Pinky"],
  ]) {
    for (let k = 1; k <= 3; k++) {
      const parent = k === 1 ? `${side}_wrist` : `${side}_${finger}${k - 1}`;
      BODY.push([`${side}_${finger}${k}`, parent, `${Side}Hand${Finger}${k}`]);
    }
  }
}
const JOINT_INDEX = Object.fromEntries(SMPLX_JOINTS.map((name, i) => [name, i]));

const container = document.getElementById("avatar");

async function main() {
  const probe = await fetch(MODEL_URL, { method: "HEAD" });
  if (!probe.ok) return;

  container.hidden = false;
  container.dataset.state = "loading";
  document.body.classList.add("has-avatar");

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  container.append(renderer.domElement);

  const scene = new THREE.Scene();
  scene.add(new THREE.HemisphereLight(0xffffff, 0x8899aa, 2.2));
  const key = new THREE.DirectionalLight(0xffffff, 1.6);
  key.position.set(1, 2, 3);
  scene.add(key);

  const model = await new FBXLoader().loadAsync(MODEL_URL);
  scene.add(model);

  const box = new THREE.Box3().setFromObject(model);
  const center = box.getCenter(new THREE.Vector3());
  model.position.set(-center.x, -box.min.y, -center.z);
  const height = box.max.y - box.min.y;
  model.updateMatrixWorld(true);

  // Rest (T-pose) world orientations, before anything is moved: gesture
  // retargeting is relative to them.
  const rig = retargetRig(model);

  lowerArm(model, "mixamorigLeftArm", "mixamorigLeftForeArm", 1);
  lowerArm(model, "mixamorigRightArm", "mixamorigRightForeArm", -1);
  // The idle pose: rest with the arms down.
  const idle = new Map(rig.bones.map(({ bone }) => [bone, bone.quaternion.clone()]));

  const camera = new THREE.PerspectiveCamera(25, 1, height * 0.01, height * 20);
  const shots = cameraShots(model, height);
  let wide = 0; // 0 = head and shoulders, 1 = waist up (for gestures)
  let wideTarget = 0;

  const face = faceControls(model);
  const head = model.getObjectByName("mixamorigHead");
  const neck = model.getObjectByName("mixamorigNeck");
  const spine = model.getObjectByName("mixamorigSpine2");

  const gestures = new GestureTrack();
  window.addEventListener("huri-session", (e) => {
    gestures.clear();
    wideTarget = e.detail.outbound.includes("motion") ? 1 : 0;
  });
  window.addEventListener("huri-motion", (e) => {
    const speaker = window.huriSpeaker;
    if (speaker) gestures.add(e.detail, speaker);
  });

  function resize() {
    const { clientWidth: w, clientHeight: h } = container;
    if (!w || !h) return;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  new ResizeObserver(resize).observe(container);
  resize();

  const clock = new THREE.Clock();
  const sway = new THREE.Euler();
  const swayQ = new THREE.Quaternion();
  const target = new THREE.Quaternion();
  let mouth = 0;
  let nextBlink = 2;

  function easeIdle(bone, x, y, z, ease) {
    if (!bone) return;
    sway.set(x, y, z);
    target.copy(idle.get(bone)).multiply(swayQ.setFromEuler(sway));
    bone.quaternion.slerp(target, ease);
  }

  renderer.setAnimationLoop(() => {
    const dt = clock.getDelta();
    const t = clock.elapsedTime;
    if (container.offsetParent === null) return; // screen without Mouse-Man

    const speaker = window.huriSpeaker;
    const level = speaker ? speaker.level() : 0;
    const open = Math.min(1, Math.max(0, (level - MOUTH_FLOOR) * MOUTH_GAIN));
    mouth += (open - mouth) * (1 - Math.exp(-dt / MOUTH_SMOOTH_S));
    face.set("jawOpen", mouth * 0.8);
    face.set("mouthFunnel", mouth * 0.2);

    let blink = 0;
    if (t > nextBlink) {
      const phase = (t - nextBlink) / 0.15;
      blink = phase < 1 ? Math.sin(phase * Math.PI) : 0;
      if (phase >= 1) nextBlink = t + 2 + Math.random() * 3;
    }
    face.set("eyeBlinkLeft", blink);
    face.set("eyeBlinkRight", blink);

    const now = speaker && speaker.ctx ? speaker.ctx.currentTime : null;
    const pose = now === null ? null : gestures.sample(now + GESTURE_LEAD_S);
    if (pose) {
      rig.apply(pose, 1 - Math.exp(-dt / GESTURE_SMOOTH_S));
    } else {
      // Idle: slow breathing and head sway. While HuRI speaks the head
      // follows the voice: it dips on loud syllables.
      const ease = 1 - Math.exp(-dt / IDLE_SMOOTH_S);
      const talk = Math.min(1, mouth * 1.5);
      for (const [bone, rest] of idle) {
        if (bone !== head && bone !== neck && bone !== spine) {
          bone.quaternion.slerp(rest, ease);
        }
      }
      // Sway is eased hard, or it would lag behind the voice.
      const fast = 1 - Math.exp(-dt / MOUTH_SMOOTH_S);
      easeIdle(spine, Math.sin(t * 1.6) * 0.012 + talk * 0.03, 0, 0, fast);
      easeIdle(neck, mouth * 0.06, Math.sin(t * 0.45) * 0.04, 0, fast);
      easeIdle(
        head,
        Math.sin(t * 0.7) * 0.025 + mouth * 0.1,
        Math.sin(t * 0.37) * 0.05 + Math.sin(t * 2.1) * 0.04 * talk,
        Math.sin(t * 0.53) * 0.02 + Math.sin(t * 3.4) * 0.03 * talk,
        fast,
      );
    }
    // No mouth on this model: a little cartoon squash on loud syllables.
    if (head) head.scale.set(1 + mouth * 0.025, 1 - mouth * 0.035, 1 + mouth * 0.025);

    wide += (wideTarget - wide) * (1 - Math.exp(-dt / 0.5));
    const y = THREE.MathUtils.lerp(shots.close.y, shots.wide.y, wide);
    const z = THREE.MathUtils.lerp(shots.close.distance, shots.wide.distance, wide);
    camera.position.set(0, y, z);
    camera.lookAt(0, y, 0);

    renderer.render(scene, camera);
  });
  container.dataset.state = "ready";
}

// Gesture frames received from HuRI, on the AudioContext clock.
class GestureTrack {
  constructor() {
    this.frames = []; // {time, quats: Float32Array(55 * 4)}, by time
    this.out = new Float32Array(SMPLX_JOINTS.length * 4);
  }

  clear() {
    this.frames = [];
  }

  // payload: [f64 BE pts][u32 BE fps][u32 BE n][poses n*165][expr n*100][trans n*3]
  add(payload, speaker) {
    const view = new DataView(payload);
    const pts = view.getFloat64(0);
    const fps = view.getUint32(8) || 30;
    const n = view.getUint32(12);
    const floats = new Float32Array(payload.slice(16));
    if (floats.length < n * (POSE_DIM + EXPR_DIM + 3)) return;

    const axis = new THREE.Vector3();
    const q = new THREE.Quaternion();
    for (let i = 0; i < n; i++) {
      const time = speaker.contextTime(pts + i / fps);
      if (time === null) continue; // no voice to sync to
      const quats = new Float32Array(SMPLX_JOINTS.length * 4);
      for (let j = 0; j < SMPLX_JOINTS.length; j++) {
        const o = i * POSE_DIM + j * 3;
        axis.set(floats[o], floats[o + 1], floats[o + 2]);
        const angle = axis.length();
        if (angle > 1e-8) q.setFromAxisAngle(axis.divideScalar(angle), angle);
        else q.identity();
        q.toArray(quats, j * 4);
      }
      this.frames.push({ time, quats });
    }
    this.frames.sort((a, b) => a.time - b.time);
  }

  // Interpolated joint quaternions at `time`, or null when not gesturing.
  sample(time) {
    const frames = this.frames;
    while (frames.length > 2 && frames[1].time < time - 1) frames.shift();
    if (!frames.length) return null;
    const last = frames[frames.length - 1];
    if (time < frames[0].time || time > last.time + GESTURE_HOLD_S) return null;

    let lo = 0;
    let hi = frames.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (frames[mid].time <= time) lo = mid;
      else hi = mid - 1;
    }
    const a = frames[lo];
    const b = frames[Math.min(lo + 1, frames.length - 1)];
    const alpha =
      b.time > a.time ? Math.min(1, Math.max(0, (time - a.time) / (b.time - a.time))) : 0;
    for (let k = 0; k < this.out.length; k += 4) {
      THREE.Quaternion.slerpFlat(this.out, k, a.quats, k, b.quats, k, alpha);
    }
    return this.out;
  }
}

// SMPL-X rotations onto the Mixamo rig. SMPL-X's rest pose is identity, so a
// joint's chained (global) SMPL-X rotation is its world-space delta from rest;
// applied on top of the bone's rest world orientation it gives the bone's
// target world orientation, from which the local one follows.
function retargetRig(model) {
  const bones = [];
  const byJoint = {};
  for (const [joint, parent, mixamo] of BODY) {
    const bone = model.getObjectByName(`mixamorig${mixamo}`);
    if (!bone) continue;
    const entry = {
      joint,
      parent,
      bone,
      index: JOINT_INDEX[joint] * 4,
      rest: bone.getWorldQuaternion(new THREE.Quaternion()),
    };
    bones.push(entry);
    byJoint[joint] = entry;
  }
  const hips = byJoint.pelvis && byJoint.pelvis.bone;
  const rootParent = new THREE.Quaternion();
  if (hips && hips.parent) hips.parent.getWorldQuaternion(rootParent);
  const rootParentInv = rootParent.clone().invert();

  const smplxGlobal = {};
  const desired = {};
  for (const name of SMPLX_JOINTS) {
    smplxGlobal[name] = new THREE.Quaternion();
    desired[name] = new THREE.Quaternion();
  }
  const local = new THREE.Quaternion();
  const parentInv = new THREE.Quaternion();
  const target = new THREE.Quaternion();

  return {
    bones,
    apply(quats, ease) {
      // BODY is parents-first, and a joint without a bone in this rig leaves
      // its children unanimated rather than wrongly chained.
      for (const { joint, parent, bone, index, rest } of bones) {
        local.fromArray(quats, index);
        if (parent === null) smplxGlobal[joint].copy(local);
        else if (byJoint[parent]) smplxGlobal[joint].copy(smplxGlobal[parent]).multiply(local);
        else continue;
        desired[joint].copy(smplxGlobal[joint]).multiply(rest);

        if (parent === null) parentInv.copy(rootParentInv);
        else parentInv.copy(desired[parent]).invert();
        target.copy(parentInv).multiply(desired[joint]);
        bone.quaternion.slerp(target, ease);
      }
    },
  };
}

// Camera heights and distances: head and shoulders, or waist up for gestures.
// Framed from the bones: a cartoon head is far bigger than human proportions.
function cameraShots(model, height) {
  const y = (name, fallback) => {
    const bone = model.getObjectByName(name);
    return bone ? bone.getWorldPosition(new THREE.Vector3()).y : fallback;
  };
  const top = y("mixamorigHeadTop_End", height);
  const neck = y("mixamorigNeck", height * 0.8);
  const hips = y("mixamorigHips", height * 0.45);
  const fit = (visible) => visible / 2 / Math.tan(THREE.MathUtils.degToRad(25 / 2));

  const close = (top - neck) * 1.75;
  // Room above for leaning and below for the hands.
  const wide = (top - hips) * 1.45;
  return {
    close: { y: (neck + top) / 2 - (top - neck) * 0.08, distance: fit(close) },
    wide: { y: (hips + top) / 2 + (top - hips) * 0.02, distance: fit(wide) },
  };
}

// Mixamo rigs come in a T-pose: swing an upper arm down along the body.
function lowerArm(model, armName, forearmName, side) {
  const arm = model.getObjectByName(armName);
  const forearm = model.getObjectByName(forearmName);
  if (!arm || !forearm) return;
  model.updateMatrixWorld(true);

  const from = forearm
    .getWorldPosition(new THREE.Vector3())
    .sub(arm.getWorldPosition(new THREE.Vector3()))
    .normalize();
  const to = new THREE.Vector3(side * 0.22, -1, 0.08).normalize();
  const swing = new THREE.Quaternion().setFromUnitVectors(from, to);

  const world = swing.multiply(arm.getWorldQuaternion(new THREE.Quaternion()));
  const parent = arm.parent.getWorldQuaternion(new THREE.Quaternion()).invert();
  arm.quaternion.copy(parent.multiply(world));
}

// Blend shapes can be spread over several meshes (head, teeth, eyelashes):
// set(name, weight) updates every mesh that has that shape.
function faceControls(model) {
  const targets = [];
  model.traverse((obj) => {
    if (obj.isMesh && obj.morphTargetDictionary) targets.push(obj);
  });
  return {
    set(name, weight) {
      for (const mesh of targets) {
        const i = mesh.morphTargetDictionary[name];
        if (i !== undefined) mesh.morphTargetInfluences[i] = weight;
      }
    },
  };
}

main().catch((err) => {
  console.error("[avatar] could not load Mouse-Man:", err);
  container.hidden = true;
  document.body.classList.remove("has-avatar");
});
