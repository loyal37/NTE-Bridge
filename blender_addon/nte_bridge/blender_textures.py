"""Discover only output-connected diffuse images and stage preview-only copies."""
from array import array
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import uuid

import bpy

from .core import BridgeError

_IMAGE_EXTENSIONS = {'.png', '.tga', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff', '.exr', '.hdr'}


def _error(material, detail):
    return BridgeError('材质“%s”的漫射预览无法自动识别：%s' % (material.name, detail))


def _connected(socket):
    links = [link for link in socket.links if link.is_valid and not link.is_muted]
    return links[0] if len(links) == 1 else None


def _diffuse_image(material, socket, shader=True, visited=None):
    if not socket.is_linked:
        return None
    link = _connected(socket)
    if link is None:
        raise _error(material, '同一插口有多个来源，请保留明确的漫射贴图连接。')
    node = link.from_node
    visited = set(visited or ())
    if node.as_pointer() in visited:
        raise _error(material, '节点形成循环连接。')
    visited.add(node.as_pointer())
    if node.mute:
        raise _error(material, '连接经过停用节点，请整理为明确的漫射连接。')
    if node.bl_idname == 'NodeReroute':
        return _diffuse_image(material, node.inputs[0], shader, visited)
    if shader and node.bl_idname in {'ShaderNodeBsdfPrincipled', 'ShaderNodeBsdfDiffuse'}:
        name = 'Base Color' if node.bl_idname == 'ShaderNodeBsdfPrincipled' else 'Color'
        return _diffuse_image(material, node.inputs[name], False, visited)
    if shader and node.bl_idname == 'ShaderNodeBsdfTransparent':
        return None
    if shader and node.bl_idname == 'ShaderNodeMixShader':
        factor = node.inputs[0]
        if not factor.is_linked and factor.default_value in (0.0, 1.0):
            return _diffuse_image(material, node.inputs[1 + int(factor.default_value)], True, visited)
        images = [_diffuse_image(material, node.inputs[index], True, visited) for index in (1, 2)]
        images = [image for image in images if image is not None]
        if len({image.as_pointer() for image in images}) > 1:
            raise _error(material, '混合着不同漫射贴图，请烘焙成一张贴图后连接到基础色。')
        return images[0] if images else None
    if not shader and node.bl_idname == 'ShaderNodeTexImage' and link.from_socket.name == 'Color':
        if node.image is None:
            raise _error(material, '基础色连接的图像纹理节点没有选择图像。')
        return node.image
    raise _error(material, '连接经过不支持的节点“%s”；请将图像颜色直接连接到基础色/漫射颜色（可经转接点）。' % node.name)


def material_diffuse_image(material):
    if material is None or not material.use_nodes or material.node_tree is None:
        return None
    tree = material.node_tree
    output = tree.get_output_node('ALL')
    if output is None:
        active = [node for node in tree.nodes if node.bl_idname == 'ShaderNodeOutputMaterial' and node.is_active_output]
        if not active:
            return None
        if len(active) != 1:
            raise _error(material, '多个渲染器输出不一致，请使用一个明确的材质输出。')
        output = active[0]
    return _diffuse_image(material, output.inputs['Surface'])


def _source(image):
    if image.source not in {'FILE', 'GENERATED'}:
        raise BridgeError('漫射图像“%s”使用 %s；请先转成单张图像，不自动选取序列、视频或 UDIM 的某一帧/块。' %
                          (image.name, image.source))
    if image.is_dirty or image.source == 'GENERATED':
        if not image.has_data or min(image.size) <= 0:
            raise BridgeError('漫射图像没有可导出的像素：' + image.name)
        library = image.library.filepath if image.library else bpy.data.filepath
        return {'image': image, 'mode': 'pixels', 'suffix': '.png',
                'identity': 'pixels:' + library + ':' + image.name}
    path = Path(bpy.path.abspath(image.filepath, library=image.library)).resolve() if image.filepath else None
    if image.packed_file:
        data = bytes(image.packed_file.data)
        suffix = path.suffix.lower() if path else Path(image.name).suffix.lower()
        if suffix in _IMAGE_EXTENSIONS:
            return {'image': image, 'mode': 'packed', 'data': data, 'suffix': suffix,
                    'identity': 'packed:' + hashlib.sha256(data).hexdigest()}
        return {'image': image, 'mode': 'pixels', 'suffix': '.png',
                'identity': 'packed:' + hashlib.sha256(data).hexdigest()}
    if path is None or not path.is_file():
        raise BridgeError('漫射图像文件不存在，请修复图像路径或将图像打包进 Blender：' + image.name)
    if path.suffix.lower() not in _IMAGE_EXTENSIONS:
        raise BridgeError('漫射预览暂不直接导入此图像格式，请转成 PNG/TGA：' + str(path))
    return {'image': image, 'mode': 'file', 'path': path, 'suffix': path.suffix.lower(),
            'identity': 'file:' + os.path.normcase(str(path))}


def _material_metadata(settings, material_path):
    records = []
    try:
        records.extend(json.loads(settings.source_data or '{}').get('materials', []))
    except (ValueError, TypeError, AttributeError):
        pass
    for entry in settings.material_catalog:
        if entry.asset_path.casefold() == material_path.casefold():
            try:
                records.append(json.loads(entry.metadata_json))
            except (ValueError, TypeError):
                pass
    return [record for record in records if isinstance(record, dict)
            and str(record.get('asset_path', '')).casefold() == material_path.casefold()]


def _original_texture_path(settings, material_path, image, matching_manual):
    references = {}
    diffuse = {}
    names = {'basecolor', 'basecolour', 'basecolortex', 'basecolortexture', 'basecolormap',
             'diffuse', 'diffusecolor', 'diffusecolormap', 'diffusetex', 'diffusetexture', 'diffusemap',
             'albedo', 'albedomap', 'albedotexture'}
    for material in _material_metadata(settings, material_path):
        for reference in material.get('textures', []):
            path = reference.get('asset_path', '')
            if not path or reference.get('type', 'Texture2D') != 'Texture2D':
                continue
            references[path.casefold()] = path
            if re.sub('[^a-z]', '', str(reference.get('name', '')).casefold()) in names:
                diffuse[path.casefold()] = path
    stems = {Path(image.filepath).stem.casefold(), Path(re.sub(r'\.\d{3}$', '', image.name)).stem.casefold()}
    candidates = diffuse or references
    matched = {key: path for key, path in candidates.items() if path.rsplit('/', 1)[-1].casefold() in stems}
    if len(matched) == 1:
        return next(iter(matched.values()))
    if len(diffuse) == 1:
        return next(iter(diffuse.values()))
    explicit = {record['asset_path'].casefold(): record['asset_path'] for record in matching_manual}
    if len(explicit) == 1:
        return next(iter(explicit.values()))
    raise BridgeError('材质“%s”的漫射图像“%s”无法确定唯一的原游戏贴图路径；请读取该角色材质资料，或在替换贴图中明确填写这张图像的原始 UE 路径。' %
                      (material_path, image.name))


def discover_material_previews(settings, manual_textures, parts=None):
    """Return automatic texture records, material bindings and a local staging plan.

    ``parts`` items provide source_material, material_path and optional. Optional
    parts (extra blueprint objects) skip previews they cannot resolve exactly.
    """
    manual = []
    for entry, record in zip(settings.textures, manual_textures):
        if record['role'] == 'BASE_COLOR':
            manual.append((Path(bpy.path.abspath(entry.file_path)).resolve(), record))
    image_sources, textures, staging, targets, bindings, target_sources = {}, {}, [], {}, {}, {}
    manual_targets = {record['asset_path'].casefold(): record for record in manual_textures}
    for part in (settings.parts if parts is None else parts):
        optional = getattr(part, 'optional', False)
        try:
            image = material_diffuse_image(part.source_material)
        except BridgeError:
            if optional:
                continue
            raise
        if image is None:
            continue
        pointer = image.as_pointer()
        if pointer not in image_sources:
            try:
                image_sources[pointer] = _source(image)
            except BridgeError:
                if optional:
                    continue
                raise
        source = image_sources[pointer]
        identity = source['identity']
        material_key = part.material_path.strip().casefold()
        if material_key in targets and targets[material_key] != identity:
            if optional:
                continue
            raise BridgeError('多个 Blender 材质映射到同一个 UE 材质“%s”，但漫射贴图不同；请先调整材质映射。' % part.material_path)
        matching = [record for path, record in manual if
                    (source['mode'] == 'file' and path == source['path']) or
                    (source['mode'] == 'packed' and hashlib.sha256(path.read_bytes()).hexdigest() ==
                     hashlib.sha256(source['data']).hexdigest())]
        try:
            texture_path = _original_texture_path(settings, part.material_path.strip(), image, matching)
        except BridgeError:
            if optional:
                continue
            raise
        texture_key = texture_path.casefold()
        if texture_key in target_sources and target_sources[texture_key] != identity:
            if optional:
                continue
            raise BridgeError('不同漫射图像指向同一个原游戏贴图路径，请检查映射：' + texture_path)
        existing = manual_targets.get(texture_key)
        if texture_key not in textures and existing and existing not in matching:
            if optional:
                continue
            raise BridgeError('自动漫射与手动替换贴图使用同一路径，但源图像或用途不同：' + texture_path)
        targets[material_key] = identity
        target_sources[texture_key] = identity
        if texture_key not in textures:
            if existing:
                textures[texture_key] = existing
            else:
                texture_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'nte-preview:' + texture_key))
                record = {'id': texture_id, 'source_file': 'textures/' + uuid.UUID(texture_id).hex + source['suffix'],
                          'asset_path': texture_path,
                          'role': 'BASE_COLOR', 'origin': 'preview'}
                textures[texture_key] = record
                staging.append(dict(source, source_file=record['source_file']))
        bindings[material_key] = {'material_path': part.material_path.strip(),
                                  'texture_path': textures[texture_key]['asset_path']}
    return ([record for record in textures.values() if record.get('origin') == 'preview'],
            list(bindings.values()), staging)


def stage_preview_images(staging, job_dir):
    for source in staging:
        target = job_dir / source['source_file']
        if source['mode'] == 'file':
            shutil.copyfile(source['path'], target)
        elif source['mode'] == 'packed':
            target.write_bytes(source['data'])
        else:
            image = source['image']
            # Image.copy() does not reliably copy edited pixel buffers. Allocate an
            # independent image and copy pixels explicitly; never save/repath the source.
            copy = bpy.data.images.new('_NTEBridgePreviewCopy', width=image.size[0], height=image.size[1],
                                       alpha=True, float_buffer=image.is_float)
            try:
                copy.colorspace_settings.name = image.colorspace_settings.name
                copy.alpha_mode = image.alpha_mode
                pixels = array('f', [0.0]) * len(image.pixels)
                image.pixels.foreach_get(pixels)
                copy.pixels.foreach_set(pixels)
                copy.update()
                copy.filepath_raw = str(target)
                copy.file_format = 'PNG'
                copy.save()
            finally:
                bpy.data.images.remove(copy)
