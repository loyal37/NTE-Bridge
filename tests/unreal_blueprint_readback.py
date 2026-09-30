"""Fresh-process readback of the instance saved by blender_blueprint_send_smoke.py (isolated project only)."""
import json
import os

import unreal

result = json.load(open(os.environ['NTE_BLUEPRINT_SEND_RESULT'], encoding='utf-8'))
instance = unreal.load_asset(result['instance'])
existing = unreal.load_asset('/Game/BPTest/MI_Existing')
mesh = unreal.load_asset(result['asset_root'] + '/SK_Body')
overrides = {str(v.get_editor_property('parameter_info').get_editor_property('name')):
             v.get_editor_property('parameter_value').get_path_name().split('.')[0]
             for v in instance.get_editor_property('texture_parameter_values')}
readback = {
    'class': instance.get_class().get_name(),
    'parent': instance.get_editor_property('parent').get_path_name().split('.')[0],
    'overrides': overrides,
    'owner_tag': unreal.EditorAssetLibrary.get_metadata_tag(instance, 'NTEBridge.MaterialInstanceOwner'),
    'mask_srgb': unreal.load_asset(overrides['SkilMask']).get_editor_property('srgb'),
    'existing_scalar': [(str(v.get_editor_property('parameter_info').get_editor_property('name')), v.get_editor_property('parameter_value'))
                        for v in existing.get_editor_property('scalar_parameter_values')],
    'mesh_slots': [(str(s.get_editor_property('material_slot_name')), s.get_editor_property('material_interface').get_path_name().split('.')[0])
                   for s in mesh.get_editor_property('materials')],
}
print('NTE_BLUEPRINT_READBACK=' + json.dumps(readback, ensure_ascii=False))
