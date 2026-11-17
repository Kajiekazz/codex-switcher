"""Unicode clipboard writes through Win32; no external helper process."""
import ctypes as c
import os
import time


def copy_text(text):
    if os.name != 'nt':
        return False
    user=c.WinDLL('user32',use_last_error=True)
    kernel=c.WinDLL('kernel32',use_last_error=True)
    handle=c.c_void_p
    user.CreateWindowExW.argtypes=[c.c_uint32,c.c_wchar_p,c.c_wchar_p,c.c_uint32,
        c.c_int,c.c_int,c.c_int,c.c_int,handle,handle,handle,handle]
    user.CreateWindowExW.restype=handle
    user.DestroyWindow.argtypes=[handle]
    user.OpenClipboard.argtypes=[handle]
    user.SetClipboardData.argtypes=[c.c_uint32,handle]
    user.SetClipboardData.restype=handle
    kernel.GlobalAlloc.argtypes=[c.c_uint32,c.c_size_t]
    kernel.GlobalAlloc.restype=handle
    kernel.GlobalLock.argtypes=[handle]
    kernel.GlobalLock.restype=handle
    kernel.GlobalUnlock.argtypes=[handle]
    kernel.GlobalFree.argtypes=[handle]
    kernel.GlobalFree.restype=handle
    owner=user.CreateWindowExW(0,'STATIC','',0,0,0,0,0,handle(-3),None,None,None)
    if not owner:
        return False
    memory=None
    opened=False
    try:
        data=(str(text)+'\0').encode('utf-16-le')
        memory=kernel.GlobalAlloc(2,len(data))
        if not memory:
            return False
        pointer=kernel.GlobalLock(memory)
        if not pointer:
            return False
        try:
            c.memmove(pointer,data,len(data))
        finally:
            kernel.GlobalUnlock(memory)
        for _ in range(5):
            if user.OpenClipboard(owner):
                opened=True
                break
            time.sleep(.02)
        if not opened or not user.EmptyClipboard():
            return False
        if not user.SetClipboardData(13,memory):  # CF_UNICODETEXT
            return False
        memory=None  # Ownership was transferred to Windows.
        return True
    finally:
        if opened:
            user.CloseClipboard()
        if memory:
            kernel.GlobalFree(memory)
        user.DestroyWindow(owner)
