/**
 * 校验 3D 场景配置与模型是否对得上。
 *
 * 用法（在仓库根执行）：
 *     node _工具/validateScene.mjs
 *
 * 查三件事：
 *   1. sceneConfig.json 里引用的每一个节点名，在 泵房.glb 里是否真的存在；
 *   2. devices.json 的 node3d 字段里的节点名是否存在（它是讲解卡片的依据，同样不能错）；
 *   3. 同一台设备在 sceneConfig 与 devices.json 里挂的节点是否一致（防两份数据各改各的）。
 *
 * 退出码 0 = 全过；1 = 有硬错误（节点名不存在）。两文件不一致算警告，不拦提交。
 */
import { readFileSync } from 'node:fs';

const ROOT = new URL('../', import.meta.url);           // _工具/ 的上一级就是仓库根
const GLB = new URL('泵房.glb', ROOT);
const SCENE = new URL('代码/frontend/sceneConfig.json', ROOT);
const DEVICES = new URL('代码/backend/devices.json', ROOT);

/** 读 JSON 文件。@param {URL} url @returns {object} */
function readJson(url) {
  return JSON.parse(readFileSync(url, 'utf8'));
}

/**
 * 从 GLB 的 glTF JSON 块里取出全部节点名。
 *
 * @param {URL} url GLB 文件路径。
 * @returns {string[]} 节点名数组（可能含重复）。
 */
function readGlbNodeNames(url) {
  const buf = readFileSync(url);
  if (buf.readUInt32LE(0) !== 0x46546c67) throw new Error(`${url.pathname} 不是 GLB 文件`);
  let offset = 12;
  while (offset < buf.length) {
    const len = buf.readUInt32LE(offset);
    const type = buf.readUInt32LE(offset + 4);
    if (type === 0x4e4f534a) {
      const gltf = JSON.parse(buf.slice(offset + 8, offset + 8 + len).toString('utf8'));
      return (gltf.nodes || []).map((n) => n.name).filter(Boolean);
    }
    offset += 8 + len;
  }
  throw new Error('GLB 里没有 JSON 块');
}

/**
 * 收集 sceneConfig.json 里引用的全部节点名，并记住「是哪个配置项引用的」。
 *
 * @param {object} cfg sceneConfig.json 的内容。
 * @returns {Map<string, string[]>} 节点名 → 引用来源说明。
 */
function collectSceneRefs(cfg) {
  const refs = new Map();
  /** 记一条引用。@param {string} name 节点名 @param {string} where 出处 */
  const add = (name, where) => {
    if (!name) return;
    if (!refs.has(name)) refs.set(name, []);
    refs.get(name).push(where);
  };

  (cfg.views || []).forEach((v) => add(v.focus, `views[${v.key}].focus`));
  if (cfg.tank) {
    add(cfg.tank.bodyNode, 'tank.bodyNode');
    add(cfg.tank.surfaceNode, 'tank.surfaceNode');
  }
  (cfg.pumps || []).forEach((p) => {
    add(p.statusNode, `pumps[${p.deviceId}].statusNode`);
    add(p.rotorHub, `pumps[${p.deviceId}].rotorHub`);
    (p.rotorBlades || []).forEach((n) => add(n, `pumps[${p.deviceId}].rotorBlades`));
    (p.rotorShaft || []).forEach((n) => add(n, `pumps[${p.deviceId}].rotorShaft`));
  });
  (cfg.hotspots || []).forEach((h) => (h.nodes || []).forEach((n) => add(n, `hotspots[${h.deviceId}]`)));

  return refs;
}

/**
 * 把 devices.json 的 node3d 字段拆成节点名数组。
 *
 * 该字段历史上是「一个字符串塞多个节点名」，以 / 分隔，所以这里要拆。
 *
 * @param {string|null} raw node3d 原始值。
 * @returns {string[]} 节点名数组。
 */
function splitNode3d(raw) {
  if (!raw) return [];
  return String(raw).split('/').map((s) => s.trim()).filter(Boolean);
}

// ---------------------------------------------------------------- 开跑

const glbNodes = new Set(readGlbNodeNames(GLB));
const cfg = readJson(SCENE);
const devices = readJson(DEVICES);

let errors = 0;
let warnings = 0;

console.log(`模型节点 ${glbNodes.size} 个 · sceneConfig 引用 ${collectSceneRefs(cfg).size} 个 · devices.json ${devices.devices.length} 条设备\n`);

console.log('【1】sceneConfig.json → GLB');
const refs = collectSceneRefs(cfg);
for (const [name, wheres] of refs) {
  if (glbNodes.has(name)) {
    console.log(`  ✓ ${name}`);
  } else {
    errors++;
    console.log(`  ✗ ${name}   ← 模型里没有这个名字！出处：${wheres.join(', ')}`);
  }
}

console.log('\n【2】devices.json 的 node3d → GLB');
for (const d of devices.devices) {
  for (const name of splitNode3d(d.node3d)) {
    if (glbNodes.has(name)) {
      console.log(`  ✓ ${d.id}: ${name}`);
    } else {
      errors++;
      console.log(`  ✗ ${d.id}: ${name}   ← 模型里没有这个名字！`);
    }
  }
}

console.log('\n【3】讲解卡片的挂点是否都被场景配置覆盖（devices.json.node3d ⊆ sceneConfig.hotspots）');
// 注意方向：sceneConfig 的热区**有意做成超集**——点电机壳、点泵体、点法兰都该弹同一张卡，
// 所以「sceneConfig 多出来的」是正常的；「devices.json 有、sceneConfig 没有」才是漏配。
const hotspotByDevice = new Map((cfg.hotspots || []).map((h) => [h.deviceId, new Set(h.nodes || [])]));
for (const d of devices.devices) {
  const fromDevices = new Set(splitNode3d(d.node3d));
  if (!fromDevices.size) continue;                 // node3d 为 null 的设备（电度表/互感器）不参与
  const fromScene = hotspotByDevice.get(d.id);
  if (!fromScene) {
    warnings++;
    console.log(`  ! ${d.id}: devices.json 有 node3d，但 sceneConfig.hotspots 里整条都没有`);
    continue;
  }
  const missing = [...fromDevices].filter((n) => !fromScene.has(n));
  const extra = [...fromScene].filter((n) => !fromDevices.has(n));
  if (missing.length) {
    warnings++;
    console.log(`  ! ${d.id}: 这些节点没进 sceneConfig 热区：${missing.join(', ')}`);
  } else {
    console.log(`  ✓ ${d.id}（sceneConfig 另有 ${extra.length} 个扩展热区，正常）`);
  }
}

console.log('\n' + '='.repeat(56));
if (errors) {
  console.log(`❌ 硬错误 ${errors} 处（节点名在模型里不存在，必须修）`);
} else {
  console.log('✅ 全部节点名都能在模型里找到');
}
if (warnings) console.log(`⚠️  警告 ${warnings} 处（两份配置不完全一致，建议对齐）`);
process.exit(errors ? 1 : 0);
