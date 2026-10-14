"""Read this user's Electron safeStorage token in memory using Windows APIs."""
import base64
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path

def read_token(value, config_path):
    if not value.startswith('safe:'):
        return value
    if os.name != 'nt':
        raise ValueError('加密 WeFlow 令牌只支持原 Windows 用户')
    class Blob(C.Structure):
        _fields_ = [('size', W.DWORD), ('data', C.c_void_p)]
    def unprotect(data):
        buf=C.create_string_buffer(data); src=Blob(len(data),C.cast(buf,C.c_void_p)); out=Blob()
        crypt=C.WinDLL('crypt32',use_last_error=True); kernel=C.WinDLL('kernel32')
        crypt.CryptUnprotectData.argtypes=[C.POINTER(Blob),C.c_void_p,C.c_void_p,C.c_void_p,C.c_void_p,W.DWORD,C.POINTER(Blob)]
        kernel.LocalFree.argtypes=[C.c_void_p]
        if not crypt.CryptUnprotectData(C.byref(src),None,None,None,None,1,C.byref(out)):
            raise ValueError('无法读取本机加密令牌（必须使用原 Windows 用户）')
        try: return C.string_at(out.data,out.size)
        finally: kernel.LocalFree(out.data)
    encrypted=base64.b64decode(value[5:])
    if not encrypted.startswith(b'v10'):
        return unprotect(encrypted).decode('utf8')
    local=json.loads(Path(config_path).parent.joinpath('Local State').read_text('utf8'))
    wrapped=base64.b64decode(local['os_crypt']['encrypted_key'])
    if not wrapped.startswith(b'DPAPI'):
        raise ValueError('不支持的 WeFlow 本机令牌加密格式')
    key=unprotect(wrapped[5:])
    class Auth(C.Structure):
        _fields_=[('size',W.ULONG),('version',W.ULONG),('nonce',C.c_void_p),('nonce_len',W.ULONG),
            ('aad',C.c_void_p),('aad_len',W.ULONG),('tag',C.c_void_p),('tag_len',W.ULONG),
            ('mac',C.c_void_p),('mac_len',W.ULONG),('cbAAD',W.ULONG),('cbData',C.c_ulonglong),('flags',W.ULONG)]
    lib=C.WinDLL('bcrypt'); alg=C.c_void_p(); handle=C.c_void_p()
    for fn in ['BCryptOpenAlgorithmProvider','BCryptSetProperty','BCryptGenerateSymmetricKey','BCryptDecrypt','BCryptDestroyKey','BCryptCloseAlgorithmProvider']:
        getattr(lib,fn).restype=C.c_long
    lib.BCryptOpenAlgorithmProvider.argtypes=[C.POINTER(C.c_void_p),W.LPCWSTR,W.LPCWSTR,W.ULONG]
    lib.BCryptSetProperty.argtypes=[C.c_void_p,W.LPCWSTR,C.c_void_p,W.ULONG,W.ULONG]
    lib.BCryptGenerateSymmetricKey.argtypes=[C.c_void_p,C.POINTER(C.c_void_p),C.c_void_p,W.ULONG,C.c_void_p,W.ULONG,W.ULONG]
    lib.BCryptDecrypt.argtypes=[C.c_void_p,C.c_void_p,W.ULONG,C.c_void_p,C.c_void_p,W.ULONG,C.c_void_p,W.ULONG,C.POINTER(W.ULONG),W.ULONG]
    lib.BCryptDestroyKey.argtypes=[C.c_void_p];lib.BCryptCloseAlgorithmProvider.argtypes=[C.c_void_p,W.ULONG]
    def check(status):
        if status: raise ValueError('WeFlow 令牌解密失败；未输出凭据')
    check(lib.BCryptOpenAlgorithmProvider(C.byref(alg),'AES',None,0))
    try:
        mode=C.create_unicode_buffer('ChainingModeGCM')
        check(lib.BCryptSetProperty(alg,'ChainingMode',mode,C.sizeof(mode),0))
        keybuf=C.create_string_buffer(key)
        check(lib.BCryptGenerateSymmetricKey(alg,C.byref(handle),None,0,keybuf,len(key),0))
        nonce=C.create_string_buffer(encrypted[3:15]); tag=C.create_string_buffer(encrypted[-16:])
        ciphertext=encrypted[15:-16]; inp=C.create_string_buffer(ciphertext); out=C.create_string_buffer(len(ciphertext)); used=W.ULONG()
        auth=Auth();auth.size=C.sizeof(Auth);auth.version=1
        auth.nonce=C.cast(nonce,C.c_void_p);auth.nonce_len=12;auth.tag=C.cast(tag,C.c_void_p);auth.tag_len=16
        check(lib.BCryptDecrypt(handle,inp,len(ciphertext),C.byref(auth),None,0,out,len(out),C.byref(used),0))
        return out.raw[:used.value].decode('utf8')
    finally:
        if handle:lib.BCryptDestroyKey(handle)
        lib.BCryptCloseAlgorithmProvider(alg,0)
