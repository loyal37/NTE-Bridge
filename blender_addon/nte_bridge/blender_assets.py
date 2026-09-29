"""Owned asset dialog window: keep it until packaging succeeds."""
import bpy

_windows = {}  # Asset window -> originating window; never close user windows.
_popups = {}


def is_asset_window(window):
    return window is not None and window.as_pointer() in _windows


def worker_window(window):
    parent = _windows.get(window.as_pointer())
    return next((item for item in bpy.context.window_manager.windows
                 if item.as_pointer() == parent), window)


def close_window(pointer):
    if pointer not in _windows:
        return
    _popups.pop(pointer, None)
    _windows.pop(pointer, None)
    window = next((item for item in bpy.context.window_manager.windows if item.as_pointer() == pointer), None)
    if window is not None:
        with bpy.context.temp_override(window=window):
            bpy.ops.wm.window_close()


def close_later(pointer):
    # Let the button / modal callback finish before freeing its dialog window.
    bpy.app.timers.register(lambda: close_window(pointer), first_interval=0.05)


def remember_popup(context):
    if is_asset_window(context.window) and context.region_popup:
        _popups[context.window.as_pointer()] = context.region_popup


def refresh():
    live = {window.as_pointer() for window in bpy.context.window_manager.windows}
    for pointer in list(_windows):
        if pointer not in live:
            _windows.pop(pointer, None)
            _popups.pop(pointer, None)
    for region in list(_popups.values()):
        region.tag_redraw()
        region.tag_refresh_ui()
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            area.tag_redraw()


def open_window(context):
    source = context.window
    for window in context.window_manager.windows:
        if _windows.get(window.as_pointer()) == source.as_pointer():
            return  # One asset browser per originating window.
    previous = {window.as_pointer() for window in context.window_manager.windows}
    bpy.ops.screen.area_dupli('INVOKE_DEFAULT')
    window = next(window for window in context.window_manager.windows if window.as_pointer() not in previous)
    pointer = window.as_pointer()
    _windows[pointer] = source.as_pointer()
    window.scene = context.scene

    def show():
        current = next((item for item in bpy.context.window_manager.windows if item.as_pointer() == pointer), None)
        if current:
            # Avoid rendering a second copy of the character behind the dialog.
            current.screen.areas[0].type = 'IMAGE_EDITOR'
            with bpy.context.temp_override(window=current, area=current.screen.areas[0]):
                bpy.ops.nte_bridge.asset_dialog('INVOKE_DEFAULT')
    bpy.app.timers.register(show, first_interval=0.1)


def unregister():
    for pointer in list(_windows):
        close_window(pointer)
