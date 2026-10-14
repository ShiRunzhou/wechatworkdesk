"""Reuse WeFlow's SenseVoice weights; audio stays in memory."""
import importlib.util
import io
import json
import os
import re
import threading
import wave
from pathlib import Path


def documents_directory():
    if os.name=='nt':
        import ctypes
        buf=ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None,5,None,0,buf)==0:
            return Path(buf.value)
    return Path.home()/'Documents'


def model_directory(config):
    override=config.get('voice_model_directory','').strip()
    if override:return Path(override)
    settings=Path(config['weflow_config'])
    try:weflow=json.loads(settings.read_text('utf8'))
    except (OSError,ValueError):weflow={}
    configured=weflow.get('whisperModelDir','')
    if configured:
        p=Path(configured)
        return p if p.is_absolute() else settings.parent/p
    # Portable installations may bundle weights. Prefer only complete directories.
    exe=config.get('weflow_executable','')
    if exe:
        for rel in ('models/sensevoice','resources/models/sensevoice'):
            p=Path(exe).parent/rel
            if (p/'model.int8.onnx').is_file() and (p/'tokens.txt').is_file():return p
    return documents_directory()/'WeFlow/models/sensevoice'


def status(config):
    p=model_directory(config)
    return {'directory':str(p),'model_found':(p/'model.int8.onnx').is_file() and (p/'tokens.txt').is_file(),
            'runtime_ready':importlib.util.find_spec('sherpa_onnx') is not None,
            'automatic':not bool(config.get('voice_model_directory'))}


class LocalVoice:
    _lock=threading.RLock()
    _recognizer=None
    _identity=None
    def __init__(self, config):self.directory=model_directory(config)
    def transcribe(self, audio):
        if not audio or len(audio)>25*1024**2:raise ValueError('语音为空或过大')
        try:
            import numpy as np
            import sherpa_onnx
        except ImportError:raise ValueError('本机尚未安装本地转写组件，请安装项目的voice依赖') from None
        try:
            with wave.open(io.BytesIO(audio),'rb') as wav:
                channels,width,rate,frames=wav.getnchannels(),wav.getsampwidth(),wav.getframerate(),wav.getnframes()
                if channels not in (1,2) or width not in (1,2,3,4) or not 8000<=rate<=192000 or not 0<frames<=600*rate:raise ValueError('语音格式或时长不受支持')
                raw=wav.readframes(frames)
            if len(raw)!=frames*width*channels:raise ValueError('语音文件不完整')
            if width==1:samples=(np.frombuffer(raw,np.uint8).astype(np.float32)-128)/128
            elif width==3:
                values=np.frombuffer(raw,np.uint8).reshape(-1,3).astype(np.int32)
                values=values[:,0]|(values[:,1]<<8)|(values[:,2]<<16)
                values=(values^0x800000)-0x800000;samples=values.astype(np.float32)/8388608
            else:samples=np.frombuffer(raw,'<i2' if width==2 else '<i4').astype(np.float32)/(32768 if width==2 else 2147483648)
            if channels==2:samples=samples.reshape(-1,2).mean(axis=1)
        except (wave.Error,EOFError):raise ValueError('WeFlow没有提供可识别的PCM WAV语音') from None
        model=self.directory/'model.int8.onnx';tokens=self.directory/'tokens.txt'
        if not model.is_file() or not tokens.is_file():raise ValueError('未找到WeFlow已下载的SenseVoice模型，请核对本机模型目录')
        identity=(str(model.resolve()),model.stat().st_mtime_ns,str(tokens.resolve()),tokens.stat().st_mtime_ns)
        with self._lock:
            if LocalVoice._identity!=identity:
                LocalVoice._recognizer=None;LocalVoice._identity=None
                LocalVoice._recognizer=sherpa_onnx.OfflineRecognizer.from_sense_voice(model=str(model),tokens=str(tokens),num_threads=2,use_itn=True,debug=False,provider='cpu')
                LocalVoice._identity=identity
            recognizer=LocalVoice._recognizer;stream=recognizer.create_stream()
            stream.accept_waveform(rate,samples);recognizer.decode_stream(stream)
            return re.sub(r'<\|[^|]*\|>','',stream.result.text).strip()
