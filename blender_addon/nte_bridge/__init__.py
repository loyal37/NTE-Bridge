"""NTE Bridge: Blender authoring and verified Unreal asset transfer."""

bl_info = {
    "name": "NTE Bridge / 异环桥接",
    "author": "loyal37",
    "version": (0, 3, 3),
    "blender": (4, 5, 0),
    "location": "3D View > Sidebar > NTE Bridge",
    "description": "角色资源路径、材质槽及形态键的 Blender / UE 桥接",
    "category": "Import-Export",
}


def register():
    from . import blender_ui
    blender_ui.register()


def unregister():
    from . import blender_ui
    blender_ui.unregister()
