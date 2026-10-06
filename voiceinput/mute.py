"""Mute the default speakers while recording (setting "錄音時暫時靜音電腦音效", added 2026-10-06).

The mute is undone when the dictation has been pasted. Speakers the user had muted themselves are left alone: only
a mute we set is undone. Core Audio (IAudioEndpointVolume) through comtypes, no extra package.
"""
import ctypes
import logging
from ctypes import HRESULT, POINTER, c_float, c_uint, c_void_p
from ctypes.wintypes import BOOL, DWORD

import comtypes
from comtypes import COMMETHOD, GUID, IUnknown

log = logging.getLogger(__name__)

_CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
_eRender, _eMultimedia = 0, 1
_CLSCTX_ALL = 23


class IAudioEndpointVolume(IUnknown):
    _iid_ = GUID("{5CDF2C82-841E-4546-9722-0CF74078229A}")
    _methods_ = [
        COMMETHOD([], HRESULT, "RegisterControlChangeNotify", (["in"], c_void_p)),
        COMMETHOD([], HRESULT, "UnregisterControlChangeNotify", (["in"], c_void_p)),
        COMMETHOD([], HRESULT, "GetChannelCount", (["out"], POINTER(c_uint))),
        COMMETHOD([], HRESULT, "SetMasterVolumeLevel", (["in"], c_float), (["in"], POINTER(GUID))),
        COMMETHOD([], HRESULT, "SetMasterVolumeLevelScalar", (["in"], c_float), (["in"], POINTER(GUID))),
        COMMETHOD([], HRESULT, "GetMasterVolumeLevel", (["out"], POINTER(c_float))),
        COMMETHOD([], HRESULT, "GetMasterVolumeLevelScalar", (["out"], POINTER(c_float))),
        COMMETHOD([], HRESULT, "SetChannelVolumeLevel", (["in"], c_uint), (["in"], c_float),
                  (["in"], POINTER(GUID))),
        COMMETHOD([], HRESULT, "SetChannelVolumeLevelScalar", (["in"], c_uint), (["in"], c_float),
                  (["in"], POINTER(GUID))),
        COMMETHOD([], HRESULT, "GetChannelVolumeLevel", (["in"], c_uint), (["out"], POINTER(c_float))),
        COMMETHOD([], HRESULT, "GetChannelVolumeLevelScalar", (["in"], c_uint), (["out"], POINTER(c_float))),
        COMMETHOD([], HRESULT, "SetMute", (["in"], BOOL), (["in"], POINTER(GUID))),
        COMMETHOD([], HRESULT, "GetMute", (["out"], POINTER(BOOL))),
    ]


class IMMDevice(IUnknown):
    _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Activate", (["in"], POINTER(GUID)), (["in"], DWORD), (["in"], c_void_p),
                  (["out"], POINTER(POINTER(IUnknown)))),
    ]


class IMMDeviceEnumerator(IUnknown):
    _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    _methods_ = [
        COMMETHOD([], HRESULT, "EnumAudioEndpoints", (["in"], DWORD), (["in"], DWORD), (["out"], POINTER(c_void_p))),
        COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint", (["in"], DWORD), (["in"], DWORD),
                  (["out"], POINTER(POINTER(IMMDevice)))),
    ]


def _endpoint() -> IAudioEndpointVolume:
    """The default output device's volume control (looked up each time: the default device can change)."""
    try:
        comtypes.CoInitialize()
    except OSError:
        pass   # already initialized in another mode on this thread: COM is usable anyway
    enum = comtypes.CoCreateInstance(_CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, _CLSCTX_ALL)
    dev = enum.GetDefaultAudioEndpoint(_eRender, _eMultimedia)
    unk = dev.Activate(ctypes.byref(IAudioEndpointVolume._iid_), _CLSCTX_ALL, None)
    return unk.QueryInterface(IAudioEndpointVolume)


class Muter:
    def __init__(self):
        self._ours = False   # the speakers are muted by us, so restore() unmutes them

    def mute(self):
        if self._ours:
            return
        try:
            vol = _endpoint()
            if vol.GetMute():
                return
            vol.SetMute(True, None)
            self._ours = True
        except Exception:
            log.exception("mute failed")

    def restore(self):
        if not self._ours:
            return
        self._ours = False
        try:
            _endpoint().SetMute(False, None)
        except Exception:
            log.exception("unmute failed")
