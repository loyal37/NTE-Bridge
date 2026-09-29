"""Blender 4.5.7 automatic diffuse staging with source-preservation assertions."""
from array import array
import hashlib
import json
from pathlib import Path
import sys
import uuid

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'blender_addon'))
import nte_bridge
from nte_bridge.blender_export import export_job, profile_manifest
from nte_bridge.blender_textures import discover_material_previews, material_diffuse_image, stage_preview_images
from nte_bridge.core import BridgeError


def pixels_hash(image):
    pixels = array('f', [0.0]) * len(image.pixels)
    image.pixels.foreach_get(pixels)
    return hashlib.sha256(pixels.tobytes()).hexdigest()


def image_state(image):
    return (image.name, image.filepath, image.source, image.file_format, image.is_dirty,
            image.colorspace_settings.name, image.alpha_mode, pixels_hash(image),
            bytes(image.packed_file.data) if image.packed_file else None)


def material(name, image, reroute=False, diffuse=False):
    value = bpy.data.materials.new(name)
    value.use_nodes = True
    tree = value.node_tree
    tree.nodes.clear()
    output = tree.nodes.new('ShaderNodeOutputMaterial')
    shader = tree.nodes.new('ShaderNodeBsdfDiffuse' if diffuse else 'ShaderNodeBsdfPrincipled')
    texture = tree.nodes.new('ShaderNodeTexImage')
    texture.image = image
    color = texture.outputs['Color']
    if reroute:
        route = tree.nodes.new('NodeReroute')
        tree.links.new(color, route.inputs[0])
        color = route.outputs[0]
    tree.links.new(color, shader.inputs['Color' if diffuse else 'Base Color'])
    tree.links.new(shader.outputs[0], output.inputs['Surface'])
    return value


def assign(settings, first, second, shared=False):
    settings.mesh.data.materials[0] = first
    settings.mesh.data.materials[1] = second
    # Replacing the fixture's authored materials intentionally replaces its part
    # identities. Recreate the test graph instead of retaining dangling old nodes.
    if settings.graph:
        bpy.data.node_groups.remove(settings.graph)
    assert bpy.ops.nte_bridge.refresh_slots() == {'FINISHED'}
    records = []
    for index, part in enumerate(settings.parts):
        part.material_path = '/Game/NTEBridgePreviewFixture/M_' + str(0 if shared else index)
        if not shared or not records:
            records.append({'asset_path': part.material_path, 'textures': [
                {'name': 'BaseColor', 'type': 'Texture2D',
                 'asset_path': '/Game/NTEBridgePreviewFixture/Original/T_' + str(0 if first == second else index)}]})
    settings.material_catalog.clear()
    settings.source_data = json.dumps({'materials': records})


def expect_error(call, text):
    try:
        call()
    except BridgeError as error:
        assert text in str(error), str(error)
    else:
        raise AssertionError('Expected rejection: ' + text)


def main():
    assert bpy.app.version == (4, 5, 7), bpy.app.version_string
    nte_bridge.register()
    source = ROOT / 'artifacts/blender_smoke/synthetic_source.blend'
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    bpy.ops.wm.open_mainfile(filepath=str(source), load_ui=False, use_scripts=False)
    settings = bpy.context.scene.nte_bridge
    settings.textures.clear()
    output = ROOT / 'artifacts/preview_smoke' / uuid.uuid4().hex[:10]
    output.mkdir(parents=True)
    settings.cache_root = str(output / '预览 缓存')
    file_path = output / '漫射 file.png'
    generated = bpy.data.images.new('Generated diffuse', width=4, height=4, alpha=True)
    generated.pixels = [0.1, 0.4, 0.8, 1.0] * 16
    generated.filepath_raw = str(file_path)
    generated.file_format = 'PNG'
    generated.save()
    file_image = bpy.data.images.load(str(file_path), check_existing=False)
    generated.pixels = [0.6, 0.2, 0.3, 1.0] * 16
    first = material('File diffuse material', file_image, reroute=True)
    second = material('Generated diffuse material', generated, diffuse=True)
    # A disconnected normal image and an inactive output must never replace base color.
    unrelated = first.node_tree.nodes.new('ShaderNodeTexImage')
    unrelated.image = generated
    inactive = first.node_tree.nodes.new('ShaderNodeOutputMaterial')
    inactive.is_active_output = False
    assign(settings, first, second)
    assert material_diffuse_image(first) == file_image
    before = {image.name: image_state(image) for image in (file_image, generated)}
    names_before = sorted(image.name for image in bpy.data.images)
    original_bytes = file_path.read_bytes()
    preview = profile_manifest(settings)
    assert len(preview['textures']) == len(preview['material_previews']) == 2
    assert all(texture['origin'] == 'preview' for texture in preview['textures'])
    assert len(preview['export_assets']) == 1
    assert {texture['asset_path'] for texture in preview['textures']} == {
        '/Game/NTEBridgePreviewFixture/Original/T_0', '/Game/NTEBridgePreviewFixture/Original/T_1'}
    assert [texture['asset_path'] for texture in profile_manifest(settings)['textures']] == [
        texture['asset_path'] for texture in preview['textures']]
    manifest_path = export_job(bpy.context)
    assert manifest_path.is_relative_to(Path(settings.cache_root) / 'Jobs')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    assert all((manifest_path.parent / texture['source_file']).is_file() for texture in manifest['textures'])
    staged = discover_material_previews(settings, [])[2]
    file_record = next(item for item in staged if item['mode'] == 'file')
    assert (manifest_path.parent / file_record['source_file']).read_bytes() == original_bytes
    generated_record = next(item for item in staged if item['mode'] == 'pixels')
    imported_preview = bpy.data.images.load(str(manifest_path.parent / generated_record['source_file']), check_existing=False)
    try:
        assert all(abs(actual - expected) <= 2 / 255 for actual, expected in
                   zip(imported_preview.pixels[:4], generated.pixels[:4])), (
                       imported_preview.pixels[:4], generated.pixels[:4])
    finally:
        bpy.data.images.remove(imported_preview)
    assert {image.name: image_state(image) for image in (file_image, generated)} == before
    assert sorted(image.name for image in bpy.data.images) == names_before
    checks = ['active-diffuse-only-reroute-and-diffuse-bsdf', 'unrelated-image-and-inactive-output-ignored',
              'deterministic-preview-only-assets-at-original-paths', 'real-export-file-and-generated-images-under-cache',
              'source-image-properties-pixels-and-file-bytes-unchanged', 'temporary-image-cleanup']
    checks.append('generated-preview-png-contains-edited-pixels')

    assign(settings, first, first)
    deduplicated = profile_manifest(settings)
    assert len(deduplicated['textures']) == 1 and len(deduplicated['material_previews']) == 2
    assign(settings, first, first, shared=True)
    assert len(profile_manifest(settings)['material_previews']) == 1
    assign(settings, first, second, shared=True)
    expect_error(lambda: profile_manifest(settings), '漫射贴图不同')
    checks += ['shared-image-and-material-bindings-deduplicated', 'conflicting-shared-ue-material-rejected']

    assign(settings, first, first)
    manual = settings.textures.add()
    manual.texture_id = str(uuid.uuid4())
    manual.file_path = str(file_path)
    manual.role = 'BASE_COLOR'
    manual.asset_path = '/Game/NTEBridgePreviewFixture/Original/T_0'
    reused = profile_manifest(settings)
    assert len(reused['textures']) == 1 and reused['textures'][0]['origin'] == 'mod'
    assert all(item['texture_path'] == manual.asset_path for item in reused['material_previews'])
    assert len(reused['export_assets']) == 2
    checks.append('explicit-replacement-texture-reused-and-remains-packable')
    settings.source_data = '{}'
    fallback = profile_manifest(settings)
    assert all(item['texture_path'] == manual.asset_path for item in fallback['material_previews'])
    settings.source_data = json.dumps({'materials': [
        {'asset_path': part.material_path, 'textures': [{'name': 'NormalMap', 'type': 'Texture2D',
          'asset_path': '/Game/NTEBridgePreviewFixture/Original/T_Normal'}]} for part in settings.parts]})
    assert len(profile_manifest(settings)['textures']) == 1
    checks.append('explicit-manual-path-resolves-missing-original-metadata')
    settings.textures.clear()

    packed = bpy.data.images.load(str(file_path), check_existing=False)
    packed.pack()
    packed_material = material('Packed diffuse', packed)
    assign(settings, packed_material, packed_material)
    packed_before = image_state(packed)
    _, _, packed_staging = discover_material_previews(settings, [])
    assert len(packed_staging) == 1 and packed_staging[0]['mode'] == 'packed'
    packed_output = output / 'packed'
    (packed_output / 'textures').mkdir(parents=True)
    stage_preview_images(packed_staging, packed_output)
    assert (packed_output / packed_staging[0]['source_file']).read_bytes() == original_bytes
    assert image_state(packed) == packed_before
    checks.append('packed-image-bytes-staged-without-unpacking-source')

    assign(settings, first, first)
    metadata = json.loads(settings.source_data)
    metadata['materials'][1]['textures'][0]['asset_path'] = '/Game/NTEBridgePreviewFixture/Original/T_Another'
    settings.source_data = json.dumps(metadata)
    assert len(profile_manifest(settings)['textures']) == 2, 'Shared image must retain distinct original targets'
    checks.append('shared-image-keeps-each-evidenced-original-asset-path')
    assign(settings, first, second)
    metadata = json.loads(settings.source_data)
    metadata['materials'][1]['textures'][0]['asset_path'] = metadata['materials'][0]['textures'][0]['asset_path']
    settings.source_data = json.dumps(metadata)
    expect_error(lambda: profile_manifest(settings), '同一个原游戏贴图路径')
    settings.source_data = '{}'
    expect_error(lambda: profile_manifest(settings), '无法确定唯一的原游戏贴图路径')
    assign(settings, first, first)
    metadata = json.loads(settings.source_data)
    metadata['materials'][0]['textures'].append({'name': 'DiffuseColor', 'type': 'Texture2D',
                                                'asset_path': '/Game/NTEBridgePreviewFixture/Original/T_Ambiguous'})
    settings.source_data = json.dumps(metadata)
    expect_error(lambda: profile_manifest(settings), '无法确定唯一的原游戏贴图路径')
    checks.append('ambiguous-missing-or-conflicting-original-paths-rejected')

    assign(settings, first, first)
    metadata = json.loads(settings.source_data)
    for record in metadata['materials']:
        record['textures'][0]['name'] = 'DiffuseColorMap'
    settings.source_data = json.dumps(metadata)
    assert len(profile_manifest(settings)['textures']) == 1
    checks.append('renamed-image-resolves-unique-diffuse-color-map-parameter')

    # Unsaved pixel edits on a file image must override its older on-disk bytes,
    # without saving those edits into the original source file.
    file_image.pixels = [0.25, 0.55, 0.75, 1.0] * 16
    dirty_before = image_state(file_image)
    _, _, dirty_staging = discover_material_previews(settings, [])
    assert len(dirty_staging) == 1 and dirty_staging[0]['mode'] == 'pixels'
    dirty_output = output / 'dirty'
    (dirty_output / 'textures').mkdir(parents=True)
    stage_preview_images(dirty_staging, dirty_output)
    dirty_loaded = bpy.data.images.load(str(dirty_output / dirty_staging[0]['source_file']), check_existing=False)
    try:
        assert all(abs(actual - expected) <= 2 / 255 for actual, expected in
                   zip(dirty_loaded.pixels[:4], file_image.pixels[:4]))
    finally:
        bpy.data.images.remove(dirty_loaded)
    assert image_state(file_image) == dirty_before and file_path.read_bytes() == original_bytes
    checks.append('dirty-file-image-pixels-export-without-saving-source')

    bad = material('Procedural diffuse', None)
    tree = bad.node_tree
    shader = next(node for node in tree.nodes if node.bl_idname == 'ShaderNodeBsdfPrincipled')
    noise = tree.nodes.new('ShaderNodeTexNoise')
    tree.links.new(noise.outputs['Color'], shader.inputs['Base Color'])
    expect_error(lambda: material_diffuse_image(bad), '不支持的节点')
    missing = material('Missing image', None)
    expect_error(lambda: material_diffuse_image(missing), '没有选择图像')
    checks.append('procedural-or-unset-connected-image-diagnosed')
    assert file_path.read_bytes() == original_bytes
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    result = {'success': True, 'blender': bpy.app.version_string, 'manifest': str(manifest_path), 'checks': checks}
    (output / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    (ROOT / 'artifacts/preview_smoke/latest_result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print('NTE_BRIDGE_PREVIEW_SMOKE=' + json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
