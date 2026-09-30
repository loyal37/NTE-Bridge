"""UE-side fixture for blueprint material tests in the isolated smoke project only.

Creates /Game/BPTest/MI_Mother (Material with game-style texture parameters) and
/Game/BPTest/MI_Existing (a user-authored instance the bridge must not modify).
Run: UnrealEditor-Cmd.exe <artifacts/ue_smoke/NTEBridgeSmoke.uproject> -run=pythonscript -script=<this file>
"""
import json
import os

import unreal

OUT = os.environ.get('NTE_BLUEPRINT_FIXTURE_OUT', '')
library = unreal.MaterialEditingLibrary
tools = unreal.AssetToolsHelpers.get_asset_tools()
assets = unreal.EditorAssetLibrary
project = unreal.Paths.convert_relative_path_to_full(unreal.Paths.get_project_file_path())
assert project.replace('\\', '/').endswith('artifacts/ue_smoke/NTEBridgeSmoke.uproject'), project

mother_path, existing_path = '/Game/BPTest/MI_Mother', '/Game/BPTest/MI_Existing'
parameters = [('BaseColor', unreal.MaterialSamplerType.SAMPLERTYPE_COLOR, '/Engine/EngineResources/DefaultTexture'),
              ('ID_Tex', unreal.MaterialSamplerType.SAMPLERTYPE_LINEAR_COLOR, '/Engine/EngineResources/DefaultTexture'),
              ('LightMap', unreal.MaterialSamplerType.SAMPLERTYPE_LINEAR_COLOR, '/Engine/EngineResources/DefaultTexture'),
              ('NomralMap', unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL, '/Engine/EngineMaterials/DefaultNormal'),
              ('SkilMask', unreal.MaterialSamplerType.SAMPLERTYPE_LINEAR_COLOR, '/Engine/EngineResources/DefaultTexture')]
mother = unreal.load_asset(mother_path)
if mother is None:
    mother = tools.create_asset('MI_Mother', '/Game/BPTest', unreal.Material, unreal.MaterialFactoryNew())
    for index, (name, sampler, default) in enumerate(parameters):
        node = library.create_material_expression(mother, unreal.MaterialExpressionTextureSampleParameter2D, -500, index * 260)
        node.set_editor_property('parameter_name', name)
        node.set_editor_property('sampler_type', sampler)
        node.set_editor_property('texture', unreal.load_asset(default))
        if name == 'BaseColor':
            library.connect_material_property(node, 'RGB', unreal.MaterialProperty.MP_BASE_COLOR)
        if name == 'NomralMap':
            library.connect_material_property(node, 'RGB', unreal.MaterialProperty.MP_NORMAL)
    library.recompile_material(mother)
    assert assets.save_loaded_asset(mother, only_if_is_dirty=False)
existing = unreal.load_asset(existing_path)
if existing is None:
    existing = tools.create_asset('MI_Existing', '/Game/BPTest', unreal.MaterialInstanceConstant,
                                  unreal.MaterialInstanceConstantFactoryNew())
    library.set_material_instance_parent(existing, mother)
    library.set_material_instance_scalar_parameter_value(existing, 'UserValue', 0.25)
    library.update_material_instance(existing)
    assert assets.save_loaded_asset(existing, only_if_is_dirty=False)
result = {'mother': mother.get_path_name(), 'existing': existing.get_path_name(),
          'parameters': sorted(str(n) for n in library.get_texture_parameter_names(mother))}
if OUT:
    with open(OUT, 'w', encoding='utf-8') as stream:
        json.dump(result, stream)
print('NTE_BLUEPRINT_FIXTURE=' + json.dumps(result))
