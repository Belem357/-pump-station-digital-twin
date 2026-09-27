/**
 * validateGlb.mjs —— 离线校验一个 GLB 文件是否结构完整、顶点数据落在声明的位置上。
 *
 * 为什么要单独一个脚本：三维同学每次重新导出模型，前端都可能「加载失败但看不出原因」。
 * 这个脚本不依赖浏览器和任何第三方库，直接按 glTF 规范逐项核对，把问题在命令行里说清楚。
 * 它最有用的一点是**把 POSITION 顶点从 BIN 块里真读回来、重算包围盒**，
 * 跟 accessor 声明的 min/max 对不上就报错——这能抓出偏移算错、数据错位这类肉眼看不见的问题。
 *
 * 用法：
 *   node _工具/validateGlb.mjs 泵房.glb
 *   node _工具/validateGlb.mjs _工具/泵房_原始导出.glb      # 也能校验原始导出件
 *
 * 退出码：0 全部通过；1 有错误（错误明细打在 stderr 之前的 stdout 里，便于复制）。
 */

import { readFileSync } from 'node:fs';

const GLB_MAGIC = 0x46546C67;
const CHUNK_JSON = 0x4E4F534A;
const CHUNK_BIN = 0x004E4942;

/** 组件类型 -> 每个分量的字节数 */
const COMPONENT_SIZE = { 5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4 };
/** 类型 -> 分量个数 */
const TYPE_COUNT = { SCALAR: 1, VEC2: 2, VEC3: 3, VEC4: 4, MAT4: 16 };

/**
 * 解析 GLB，拆出 JSON 块与 BIN 块。
 *
 * @param {string} path GLB 文件路径
 * @returns {{json: object, bin: Buffer}} 解析结果
 */
function readGlb(path) {
  const buf = readFileSync(path);
  if (buf.readUInt32LE(0) !== GLB_MAGIC) throw new Error(`${path} 不是合法的 GLB 文件`);

  const totalLength = buf.readUInt32LE(8);
  let json = null;
  let bin = null;
  let offset = 12;

  while (offset < totalLength) {
    const chunkLength = buf.readUInt32LE(offset);
    const chunkType = buf.readUInt32LE(offset + 4);
    const payload = buf.subarray(offset + 8, offset + 8 + chunkLength);
    if (chunkType === CHUNK_JSON) json = JSON.parse(new TextDecoder().decode(payload));
    if (chunkType === CHUNK_BIN) bin = payload;
    offset += 8 + chunkLength;
  }

  if (!json) throw new Error('GLB 里没有 JSON 块');
  return { json, bin, declaredLength: totalLength, actualLength: buf.length };
}

/**
 * 按 accessor 声明把数据从 BIN 块里真读出来。
 *
 * @param {object} json glTF 根对象
 * @param {Buffer} bin BIN 块
 * @param {number} index accessor 下标
 * @returns {Array<Array<number>>} 每个元素是一个分量数组
 */
function readAccessor(json, bin, index) {
  const accessor = json.accessors[index];
  const view = json.bufferViews[accessor.bufferView];
  const componentSize = COMPONENT_SIZE[accessor.componentType];
  const numComponents = TYPE_COUNT[accessor.type];
  const elementSize = componentSize * numComponents;
  const stride = view.byteStride ?? elementSize;
  const base = (view.byteOffset ?? 0) + (accessor.byteOffset ?? 0);

  const out = [];
  for (let e = 0; e < accessor.count; e++) {
    const item = [];
    for (let c = 0; c < numComponents; c++) {
      const at = base + e * stride + c * componentSize;
      if (accessor.componentType === 5126) item.push(bin.readFloatLE(at));
      else if (accessor.componentType === 5123) item.push(bin.readUInt16LE(at));
      else if (accessor.componentType === 5125) item.push(bin.readUInt32LE(at));
      else if (accessor.componentType === 5121) item.push(bin.readUInt8(at));
      else item.push(bin.readInt16LE(at));
    }
    out.push(item);
  }
  return out;
}

/* ============================ 校验主体 ============================ */

const glbPath = process.argv[2];
if (!glbPath) {
  console.error('用法: node _工具/validateGlb.mjs <文件.glb>');
  process.exit(1);
}

const { json, bin, declaredLength, actualLength } = readGlb(glbPath);
const errors = [];
const warnings = [];
const views = json.bufferViews ?? [];
const accessors = json.accessors ?? [];

/* ---- 1. 文件头自洽 ---- */
if (declaredLength !== actualLength) {
  errors.push(`文件头声明的总长 ${declaredLength} 与实际 ${actualLength} 不符`);
}
if (json.buffers[0].byteLength !== bin.length) {
  errors.push(`buffers[0].byteLength=${json.buffers[0].byteLength} 与 BIN 块实际 ${bin.length} 不符`);
}

/* ---- 2. bufferView 边界与重叠 ---- */
views.forEach((view, i) => {
  const bufferLength = json.buffers[view.buffer].byteLength;
  if ((view.byteOffset ?? 0) + view.byteLength > bufferLength) {
    errors.push(`bufferView[${i}] 越界：${view.byteOffset ?? 0}+${view.byteLength} > ${bufferLength}`);
  }
});
const byOffset = views.map((view, i) => ({ ...view, i })).sort((a, b) => (a.byteOffset ?? 0) - (b.byteOffset ?? 0));
for (let k = 1; k < byOffset.length; k++) {
  const prev = byOffset[k - 1];
  const cur = byOffset[k];
  if ((prev.byteOffset ?? 0) + prev.byteLength > (cur.byteOffset ?? 0)) {
    errors.push(`bufferView[${prev.i}] 与 [${cur.i}] 数据重叠`);
  }
}

/* ---- 3. accessor 边界 ---- */
accessors.forEach((accessor, i) => {
  const view = views[accessor.bufferView];
  if (!view) { errors.push(`accessor[${i}] 引用了不存在的 bufferView ${accessor.bufferView}`); return; }
  const elementSize = COMPONENT_SIZE[accessor.componentType] * TYPE_COUNT[accessor.type];
  const stride = view.byteStride ?? elementSize;
  const needed = (accessor.count - 1) * stride + elementSize;
  if ((accessor.byteOffset ?? 0) + needed > view.byteLength) {
    errors.push(`accessor[${i}] 越界：需要 ${needed} 字节，bufferView 只有 ${view.byteLength}`);
  }
});

/* ---- 4. 逐网格复核 POSITION 包围盒 + 索引范围 ---- */
let checkedMeshes = 0;
(json.meshes ?? []).forEach((mesh, meshIndex) => {
  mesh.primitives.forEach((primitive, primIndex) => {
    const positionIndex = primitive.attributes?.POSITION;
    if (positionIndex == null) return;

    const accessor = accessors[positionIndex];
    if (!accessor.min || !accessor.max) {
      warnings.push(`mesh[${meshIndex}].primitive[${primIndex}] 的 POSITION 缺 min/max`);
      return;
    }

    const positions = readAccessor(json, bin, positionIndex);
    const actualMin = [Infinity, Infinity, Infinity];
    const actualMax = [-Infinity, -Infinity, -Infinity];
    for (const vertex of positions) {
      for (let c = 0; c < 3; c++) {
        actualMin[c] = Math.min(actualMin[c], vertex[c]);
        actualMax[c] = Math.max(actualMax[c], vertex[c]);
      }
    }

    let mismatch = false;
    for (let c = 0; c < 3; c++) {
      if (Math.abs(actualMin[c] - accessor.min[c]) > 1e-5 ||
          Math.abs(actualMax[c] - accessor.max[c]) > 1e-5) {
        mismatch = true;
      }
    }
    if (mismatch) {
      errors.push(`mesh[${meshIndex}] 声明的包围盒与真实顶点不符：` +
        `声明 [${accessor.min}]~[${accessor.max}]，实际 [${actualMin.map(v => v.toFixed(3))}]~[${actualMax.map(v => v.toFixed(3))}]`);
      return;
    }
    checkedMeshes++;

    if (primitive.indices != null) {
      const indices = readAccessor(json, bin, primitive.indices).flat();
      const outOfRange = indices.filter(n => n >= positions.length);
      if (outOfRange.length) {
        errors.push(`mesh[${meshIndex}] 索引越界：最大顶点号 ${Math.max(...outOfRange)} >= 顶点数 ${positions.length}`);
      }
    }
  });
});

/* ---- 5. 节点 / 场景引用有效性 ---- */
(json.nodes ?? []).forEach((node, i) => {
  if (node.mesh != null && !json.meshes[node.mesh]) errors.push(`node[${i}] 引用了不存在的 mesh ${node.mesh}`);
  (node.children ?? []).forEach(child => {
    if (!json.nodes[child]) errors.push(`node[${i}] 的子节点 ${child} 不存在`);
  });
});

const sceneRoots = json.scenes[json.scene ?? 0].nodes;
sceneRoots.forEach(i => { if (!json.nodes[i]) errors.push(`场景引用了不存在的节点 ${i}`); });

// 遍历一遍，找出挂在场景图之外的孤立节点（渲染器根本看不到）
const reachable = new Set();
const walk = (i) => {
  if (reachable.has(i)) return;
  reachable.add(i);
  (json.nodes[i].children ?? []).forEach(walk);
};
sceneRoots.forEach(walk);
(json.nodes ?? []).forEach((node, i) => {
  if (!reachable.has(i)) warnings.push(`node[${i}] ${node.name} 不在场景图里（渲染不出来）`);
});

/* ---- 输出 ---- */
console.log(`校验 ${glbPath}`);
console.log(`  节点 ${json.nodes.length} | 网格 ${json.meshes.length} | 材质 ${json.materials.length}` +
  ` | bufferView ${views.length} | accessor ${accessors.length}`);
console.log(`  POSITION 包围盒逐顶点复核：${checkedMeshes} 个网格通过`);

if (warnings.length) {
  console.log(`\n警告 ${warnings.length} 条：`);
  warnings.forEach(w => console.log('  ! ' + w));
}
if (errors.length) {
  console.log(`\n错误 ${errors.length} 条：`);
  errors.forEach(e => console.log('  ✗ ' + e));
  process.exit(1);
}
console.log('\n✓ 结构校验全部通过');
