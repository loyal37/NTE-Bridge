"""Opt-in, read-only original-asset export test; all output stays in --out."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

import bpy
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'blender_addon'))
import nte_bridge
from nte_bridge.blender_export import export_job


def coordinates(data):
    values = np.empty(len(data) * 3, dtype=np.float32)
    data.foreach_get('co', values)
    return values


def snapshot(mesh, rig):
    digest = hashlib.sha256(coordinates(mesh.data.vertices).tobytes())
    for key in mesh.data.shape_keys.key_blocks if mesh.data.shape_keys else []:
        digest.update(key.name.encode('utf-8'))
        digest.update(coordinates(key.data).tobytes())
        digest.update(str(key.value).encode())
    indices = np.empty(len(mesh.data.polygons), dtype=np.int32)
    mesh.data.polygons.foreach_get('material_index', indices)
    digest.update(indices.tobytes())
    digest.update(json.dumps([list(row) for row in mesh.matrix_world]).encode())
    return {'data_sha256': digest.hexdigest(), 'slot_materials': [s.material.name if s.material else '' for s in mesh.material_slots],
            'bones': [(b.name, b.parent.name if b.parent else '') for b in rig.data.bones],
            'uv_layers': [u.name for u in mesh.data.uv_layers],
            'datablocks': {name: sorted(item.name for item in getattr(bpy.data, name))
                           for name in ('objects', 'meshes', 'armatures', 'shape_keys', 'materials', 'scenes')}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--blend', required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--asset-root', default='/Game/NTEBridgeReal/Fadia')
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    source, output = Path(args.blend).resolve(), Path(args.out).resolve()
    output.mkdir(parents=True, exist_ok=True)
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    bpy.ops.wm.open_mainfile(filepath=str(source), load_ui=False, use_scripts=False)
    nte_bridge.register()
    meshes = [obj for obj in bpy.context.scene.objects if obj.type == 'MESH' and obj.find_armature()]
    if len(meshes) != 1:
        raise RuntimeError('Real smoke fixture must contain exactly one bound mesh')
    mesh = meshes[0]
    rig = mesh.find_armature()
    zeros, nearzeros = [], []
    if mesh.data.shape_keys:
        for key in mesh.data.shape_keys.key_blocks[1:]:
            delta = coordinates(key.data) - coordinates(key.relative_key.data)
            maximum = float(np.max(np.abs(delta)))
            if maximum == 0:
                zeros.append(key.name)
            elif maximum < 1e-6:
                nearzeros.append({'name': key.name, 'maximum': maximum})
    result = {'source': str(source), 'source_sha256': original_hash,
              'vertices': len(mesh.data.vertices), 'polygons': len(mesh.data.polygons),
              'slots': len(mesh.material_slots), 'bones': len(rig.data.bones),
              'shape_keys_excluding_basis': len(mesh.data.shape_keys.key_blocks) - 1 if mesh.data.shape_keys else 0,
              'all_zero_shape_keys': zeros, 'near_zero_shape_keys': nearzeros,
              'modifiers': [{'name': m.name, 'type': m.type} for m in mesh.modifiers], 'success': False}
    (output / 'preflight.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    if zeros:
        print('NTE_REAL_PREFLIGHT_EMPTY_SHAPES=' + json.dumps(result, ensure_ascii=False), flush=True)
        return
    settings = bpy.context.scene.nte_bridge
    settings.mesh = mesh
    settings.armature = rig
    settings.project_file = str(Path(args.project).resolve())
    settings.mesh_path = args.asset_root
    settings.skeleton_path = args.asset_root + '_Skeleton'
    settings.create_placeholders = True
    settings.cache_root = str(output)
    bpy.ops.nte_bridge.refresh_slots()
    for part in settings.parts:
        material_name = re.sub('[^A-Za-z0-9_]', '_', part.source_material.name if part.source_material else 'Empty')
        part.material_path = '/Game/NTEBridgeReal/Materials/' + material_name
    before = snapshot(mesh, rig)
    manifest_path = export_job(bpy.context, timeout=600)
    result['source_scene_unchanged'] = before == snapshot(mesh, rig)
    result['source_file_unchanged'] = hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    result['manifest'] = str(manifest_path)
    result['success'] = result['source_scene_unchanged'] and result['source_file_unchanged']
    (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('NTE_BRIDGE_REAL_SMOKE=' + json.dumps(result, ensure_ascii=False), flush=True)
    if not result['success']:
        raise RuntimeError('Source preservation check failed')


if __name__ == '__main__':
    main()
