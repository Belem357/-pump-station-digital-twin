/**
 * fixGlbNodeNames.mjs —— 把三维同学导出的 GLB 节点名对齐到后端。
 *
 * 背景：backend/devices.json 的 node3d 字段用的是《03.接口规范与数据字典》第二节的节点名
 * （tank_body / pump_01_motor / ...），而 Blender 导出的节点名是中文，两边对不上。
 * 本脚本改 GLB 的 JSON 块，把名字改齐，让后端点名的节点都能被找到。
 *
 * 做三件事：
 *   1. 重命名 7 个节点为契约名
 *   2. 把 进水阀体/进水阀杆/进水手轮 收进一个新父节点 valve_inlet
 *      （后端把它当一个设备，收成一个整体后前端才能整体旋转）
 *   3. 补一个 tank_water_surface 节点：水体圆柱，原点在水箱底面，scale.y 映射液位
 *
 * 用法：
 *   node _工具/fixGlbNodeNames.mjs <输入.glb> <输出.glb>
 *
 * 注意：脚本只改名字与层级，不动任何已有网格的顶点数据，也不动材质。
 * 重新导出模型后重跑一次即可。
 */

import { readFileSync, writeFileSync } from 'node:fs';

/* ============================ 契约定义 ============================ */

/** 节点重命名表：Blender 导出名 -> 《03.接口规范》契约名
 *  注：进水阀体不在表里——它要先被收进 valve_inlet 父节点，再作为子节点保留原名 */
const RENAME_MAP = {
  '水箱筒体':   'tank_body',
  '水泵1电机':  'pump_01_motor',
  '水泵2电机':  'pump_02_motor',
  '水泵1泵体':  'pump_01_body',
  '水泵2泵体':  'pump_02_body',
  '出水总管':   'pipe_main_out',
};

/** 进水阀被拆成的三个零件，收进 valve_inlet 这一个父节点 */
const VALVE_PARTS = ['进水阀体', '进水阀杆', '进水手轮'];

/** 阀门旋转轴心。竖直阀杆穿过 (-0.20, ·, 0)，取阀体中心为原点 */
const VALVE_PIVOT = [-0.20, 0.60, 0.00];

/** 水体圆柱参数。原点在水箱底面：筒体 y 0.60~2.60，故底面取 0.61 */
const WATER = {
  name: 'tank_water_surface',
  radius: 0.665,        // 筒体内径 0.70，留 0.035 间隙避免与筒壁共面闪烁
  height: 1.80,         // 顶点到 0.61+1.80=2.41，在顶盖球冠之下
  segments: 48,
  origin: [-1.80, 0.61, 0.00],
  materialName: '水体',
};

/** 对齐后必须存在的节点名（与 devices.json 的 node3d 字段一致） */
const REQUIRED_NODES = [
  'tank_body', 'tank_water_surface',
  'pump_01_motor', 'pump_01_body',
  'pump_02_motor', 'pump_02_body',
  'valve_inlet', 'pipe_main_out',
];

/* ============================ GLB 读写 ============================ */

const GLB_MAGIC = 0x46546C67;
const CHUNK_JSON = 0x4E4F534A;
const CHUNK_BIN = 0x004E4942;

/**
 * 把字节数向上取整到 4 的倍数（GLB 各块必须 4 字节对齐）。
 *
 * @param {number} n 原始字节数
 * @returns {number} 对齐后的字节数
 */
function align4(n) {
  return (n + 3) & ~3;
}

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
  let bin = Buffer.alloc(0);
  let offset = 12;

  while (offset < totalLength) {
    const chunkLength = buf.readUInt32LE(offset);
    const chunkType = buf.readUInt32LE(offset + 4);
    const payload = buf.subarray(offset + 8, offset + 8 + chunkLength);
    if (chunkType === CHUNK_JSON) json = JSON.parse(new TextDecoder().decode(payload));
    if (chunkType === CHUNK_BIN) bin = Buffer.from(payload);
    offset += 8 + chunkLength;
  }

  if (!json) throw new Error('GLB 里没有 JSON 块');
  return { json, bin };
}

/**
 * 把 JSON 与 BIN 重新打包成 GLB 写盘。
 * JSON 块用空格补齐、BIN 块用 0x00 补齐，这是 glTF 规范要求（为了兼容文本解析器）。
 *
 * @param {string} path 输出路径
 * @param {object} json glTF 根对象
 * @param {Buffer} bin 二进制缓冲
 */
function writeGlb(path, json, bin) {
  const jsonBytes = Buffer.from(JSON.stringify(json), 'utf8');
  const jsonPadded = align4(jsonBytes.length);
  const binPadded = align4(bin.length);
  const totalLength = 12 + 8 + jsonPadded + 8 + binPadded;

  const out = Buffer.alloc(totalLength);
  out.writeUInt32LE(GLB_MAGIC, 0);
  out.writeUInt32LE(2, 4);
  out.writeUInt32LE(totalLength, 8);

  out.writeUInt32LE(jsonPadded, 12);
  out.writeUInt32LE(CHUNK_JSON, 16);
  out.fill(0x20, 20, 20 + jsonPadded);          // 空格填充
  jsonBytes.copy(out, 20);

  const binHeaderAt = 20 + jsonPadded;
  out.writeUInt32LE(binPadded, binHeaderAt);
  out.writeUInt32LE(CHUNK_BIN, binHeaderAt + 4);
  bin.copy(out, binHeaderAt + 8);               // 尾部本就是 0x00

  writeFileSync(path, out);
}

/* ============================ 处理步骤 ============================ */

/**
 * 按契约名重命名节点。
 *
 * @param {object} json glTF 根对象
 * @returns {number} 实际改名的节点数
 */
function renameNodes(json) {
  let count = 0;
  for (const node of json.nodes) {
    const target = RENAME_MAP[node.name];
    if (target) {
      console.log(`  改名  ${node.name}  ->  ${target}`);
      node.name = target;
      count++;
    }
  }
  for (const [from, to] of Object.entries(RENAME_MAP)) {
    if (!json.nodes.some(n => n.name === to)) console.warn(`  ⚠ 没找到「${from}」，无法生成 ${to}`);
  }
  return count;
}

/**
 * 把进水阀的三个零件收进一个新的 valve_inlet 父节点。
 * 原模型所有节点都是场景根节点（平移即世界坐标），所以子节点平移减去轴心即可保持位置不变。
 *
 * @param {object} json glTF 根对象
 * @returns {boolean} 是否执行了合并
 */
function groupValve(json) {
  if (json.nodes.some(n => n.name === 'valve_inlet' && Array.isArray(n.children))) {
    console.log('  阀门已合并过，跳过');
    return false;
  }

  const partIndices = VALVE_PARTS
    .map(partName => json.nodes.findIndex(n => n.name === partName))
    .filter(i => i >= 0);

  if (partIndices.length === 0) {
    console.warn('  ⚠ 一个阀门零件都没找到，跳过合并');
    return false;
  }

  // 子节点平移改为相对轴心：新平移 = 原世界平移 - 轴心
  for (const idx of partIndices) {
    const node = json.nodes[idx];
    const world = node.translation ?? [0, 0, 0];
    node.translation = world.map((v, i) => +(v - VALVE_PIVOT[i]).toFixed(6));
  }

  const valveIndex = json.nodes.length;
  json.nodes.push({
    name: 'valve_inlet',
    translation: [...VALVE_PIVOT],
    children: partIndices,
  });
  console.log(`  合并  ${VALVE_PARTS.slice(0, partIndices.length).join(' + ')}  ->  valve_inlet（父节点 #${valveIndex}）`);

  // 三个零件原本挂在场景根上，改挂到新父节点下
  const scene = json.scenes[json.scene ?? 0];
  scene.nodes = scene.nodes.filter(i => !partIndices.includes(i));
  scene.nodes.push(valveIndex);
  return true;
}

/**
 * 生成水体圆柱的顶点/法线/索引数据。
 * 侧面 + 顶盖，不要底面（贴水箱底，看不见）。绕序按外法线朝外/朝上。
 *
 * @returns {{positions: number[], normals: number[], indices: number[]}}
 */
function buildWaterGeometry() {
  const { radius: r, height: h, segments: seg } = WATER;
  const positions = [];
  const normals = [];
  const indices = [];

  // 侧壁：每段两个顶点（下、上），法线为径向
  for (let i = 0; i <= seg; i++) {
    const theta = (i / seg) * Math.PI * 2;
    const cos = Math.cos(theta);
    const sin = Math.sin(theta);
    positions.push(r * cos, 0, r * sin, r * cos, h, r * sin);
    normals.push(cos, 0, sin, cos, 0, sin);
  }
  for (let i = 0; i < seg; i++) {
    const bottom = i * 2;
    const top = bottom + 1;
    const nextBottom = bottom + 2;
    const nextTop = bottom + 3;
    indices.push(bottom, top, nextBottom);
    indices.push(top, nextTop, nextBottom);
  }

  // 顶盖：圆心 + 一圈边缘，法线朝上
  const centerIndex = positions.length / 3;
  positions.push(0, h, 0);
  normals.push(0, 1, 0);
  const rimStart = positions.length / 3;
  for (let i = 0; i <= seg; i++) {
    const theta = (i / seg) * Math.PI * 2;
    positions.push(r * Math.cos(theta), h, r * Math.sin(theta));
    normals.push(0, 1, 0);
  }
  for (let i = 0; i < seg; i++) {
    // 绕序 (圆心, 后一点, 前一点) 才得到 +Y 法线
    indices.push(centerIndex, rimStart + i + 1, rimStart + i);
  }

  return { positions, normals, indices };
}

/**
 * 往 GLB 里追加 tank_water_surface 节点（含网格、材质、顶点数据）。
 * 新增数据一律 4 字节对齐，直接续在 BIN 块尾部。
 *
 * @param {object} json glTF 根对象
 * @param {Buffer} bin 原二进制缓冲
 * @returns {Buffer} 追加后的二进制缓冲
 */
function addWaterSurface(json, bin) {
  if (json.nodes.some(n => n.name === WATER.name)) {
    console.log('  水体节点已存在，跳过');
    return bin;
  }

  const { positions, normals, indices } = buildWaterGeometry();

  // 续写顶点数据：每段都保证 4 字节对齐
  const chunks = [];
  let cursor = align4(bin.length);

  const positionBuffer = Buffer.alloc(positions.length * 4);
  positions.forEach((v, i) => positionBuffer.writeFloatLE(v, i * 4));
  const normalBuffer = Buffer.alloc(normals.length * 4);
  normals.forEach((v, i) => normalBuffer.writeFloatLE(v, i * 4));
  const indexBuffer = Buffer.alloc(indices.length * 2);
  indices.forEach((v, i) => indexBuffer.writeUInt16LE(v, i * 2));

  const positionOffset = cursor;
  chunks.push({ offset: positionOffset, data: positionBuffer });
  cursor = align4(cursor + positionBuffer.length);

  const normalOffset = cursor;
  chunks.push({ offset: normalOffset, data: normalBuffer });
  cursor = align4(cursor + normalBuffer.length);

  const indexOffset = cursor;
  chunks.push({ offset: indexOffset, data: indexBuffer });
  cursor = align4(cursor + indexBuffer.length);

  const newBin = Buffer.alloc(cursor);        // 多出的间隙填 0x00
  bin.copy(newBin, 0);
  for (const chunk of chunks) chunk.data.copy(newBin, chunk.offset);

  // 登记 bufferView
  json.bufferViews = json.bufferViews ?? [];
  const positionView = json.bufferViews.length;
  json.bufferViews.push({ buffer: 0, byteOffset: positionOffset, byteLength: positionBuffer.length, target: 34962 });
  const normalView = json.bufferViews.length;
  json.bufferViews.push({ buffer: 0, byteOffset: normalOffset, byteLength: normalBuffer.length, target: 34962 });
  const indexView = json.bufferViews.length;
  json.bufferViews.push({ buffer: 0, byteOffset: indexOffset, byteLength: indexBuffer.length, target: 34963 });

  // 登记 accessor。POSITION 必须带 min/max，渲染器靠它算包围盒
  json.accessors = json.accessors ?? [];
  const vertexCount = positions.length / 3;
  const positionAccessor = json.accessors.length;
  json.accessors.push({
    bufferView: positionView,
    componentType: 5126, type: 'VEC3', count: vertexCount,
    min: [-WATER.radius, 0, -WATER.radius],
    max: [WATER.radius, WATER.height, WATER.radius],
  });
  const normalAccessor = json.accessors.length;
  json.accessors.push({ bufferView: normalView, componentType: 5126, type: 'VEC3', count: vertexCount });
  const indexAccessor = json.accessors.length;
  json.accessors.push({ bufferView: indexView, componentType: 5123, type: 'SCALAR', count: indices.length });

  // 登记材质（水体蓝，前端可再改透明度）
  json.materials = json.materials ?? [];
  const materialIndex = json.materials.length;
  json.materials.push({
    name: WATER.materialName,
    pbrMetallicRoughness: {
      baseColorFactor: [0.05, 0.55, 0.78, 1.0],
      metallicFactor: 0.1,
      roughnessFactor: 0.15,
    },
    doubleSided: true,
  });

  // 登记网格与节点
  json.meshes = json.meshes ?? [];
  const meshIndex = json.meshes.length;
  json.meshes.push({
    name: '水面',
    primitives: [{
      attributes: { POSITION: positionAccessor, NORMAL: normalAccessor },
      indices: indexAccessor,
      material: materialIndex,
    }],
  });

  const nodeIndex = json.nodes.length;
  json.nodes.push({ name: WATER.name, mesh: meshIndex, translation: [...WATER.origin] });
  json.scenes[json.scene ?? 0].nodes.push(nodeIndex);

  json.buffers[0].byteLength = newBin.length;

  console.log(`  新建  ${WATER.name}（${vertexCount} 顶点 / ${indices.length / 3} 三角面，原点 [${WATER.origin}]，scale.y 映射液位）`);
  return newBin;
}

/**
 * 自检：确认契约要求的节点都在，且原有节点没丢。
 *
 * @param {object} json glTF 根对象
 * @param {number} originalNodeCount 处理前的节点总数
 * @param {number} addedNodeCount 本次处理新增的节点数（重复运行时为 0）
 * @returns {boolean} 是否全部通过
 */
function verify(json, originalNodeCount, addedNodeCount) {
  const names = new Set(json.nodes.map(n => n.name));
  let ok = true;

  for (const required of REQUIRED_NODES) {
    if (!names.has(required)) {
      console.error(`  ✗ 缺少契约节点 ${required}`);
      ok = false;
    }
  }

  const valveNode = json.nodes.find(n => n.name === 'valve_inlet');
  if (valveNode?.children?.length !== VALVE_PARTS.length) {
    console.error(`  ✗ valve_inlet 未收全零件：应 ${VALVE_PARTS.length} 个，实 ${valveNode?.children?.length ?? 0} 个`);
    ok = false;
  }

  const expected = originalNodeCount + addedNodeCount;
  if (json.nodes.length !== expected) {
    console.error(`  ✗ 节点数异常：期望 ${expected}，实际 ${json.nodes.length}`);
    ok = false;
  }

  if (ok) console.log(`  ✓ 自检通过：${REQUIRED_NODES.length} 个契约节点齐全，节点数 ${originalNodeCount} -> ${json.nodes.length}`);
  return ok;
}

/* ============================ 主流程 ============================ */

const [inputPath, outputPath] = process.argv.slice(2);
if (!inputPath || !outputPath) {
  console.error('用法: node _工具/fixGlbNodeNames.mjs <输入.glb> <输出.glb>');
  process.exit(1);
}

console.log(`读取 ${inputPath}`);
const { json, bin } = readGlb(inputPath);
const originalNodeCount = json.nodes.length;
console.log(`  节点 ${originalNodeCount} 个，BIN ${bin.length} 字节`);

console.log('\n1) 合并进水阀（必须先于改名：合并要按原名找零件）');
const valveAdded = groupValve(json) ? 1 : 0;

console.log('\n2) 节点改名');
renameNodes(json);

console.log('\n3) 补水体节点');
const newBin = addWaterSurface(json, bin);
const waterAdded = json.nodes.some(n => n.name === WATER.name) ? 1 : 0;

console.log('\n4) 自检');
if (!verify(json, originalNodeCount, valveAdded + waterAdded)) {
  console.error('自检未通过，不写出文件');
  process.exit(1);
}

writeGlb(outputPath, json, newBin);
console.log(`\n已写出 ${outputPath}（BIN ${newBin.length} 字节）`);
