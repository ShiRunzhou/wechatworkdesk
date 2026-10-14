import io
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
from workdesk.local_voice import model_directory,LocalVoice
from workdesk.assistant import VoiceClient


class LocalVoiceTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name)
        self.config=self.root/'config/WeFlow-config.json';self.config.parent.mkdir();self.config.write_text('{}')
    def test_model_follows_each_devices_configuration(self):
        first=self.root/'device-one/model';second=self.root/'device-two/model'
        config={'weflow_config':str(self.config)}
        for directory in [first,second]:
            self.config.write_text(json.dumps({'whisperModelDir':str(directory)}))
            self.assertEqual(model_directory(config),directory)
        config['voice_model_directory']=str(first);self.assertEqual(model_directory(config),first)
    def test_portable_and_actual_documents_fallback(self):
        config={'weflow_config':str(self.config),'weflow_executable':str(self.root/'portable/WeFlow.exe')}
        with patch('workdesk.local_voice.documents_directory',return_value=self.root/'redirected-documents'):
            self.assertEqual(model_directory(config),self.root/'redirected-documents/WeFlow/models/sensevoice')
        model=self.root/'portable/models/sensevoice';model.mkdir(parents=True)
        (model/'model.int8.onnx').write_bytes(b'model');(model/'tokens.txt').write_text('tokens')
        self.assertEqual(model_directory(config),model)
    def test_relative_custom_path_uses_weflow_config_directory(self):
        self.config.write_text(json.dumps({'whisperModelDir':'models/custom'}))
        self.assertEqual(model_directory({'weflow_config':str(self.config)}),self.config.parent/'models/custom')
    def test_local_voice_cache_never_calls_external_api(self):
        client=object.__new__(VoiceClient);client.transcripts={};client.cache=self.root/'cache';client.cache.mkdir();client.stt={'voice_mode':'local'}
        client.local_voice=type('Local',(),{'transcribe':lambda s,b:'本地识别结果'})()
        client.media=lambda _:b'fakewav'
        m={'localType':34,'localId':3,'serverId':'3','createTime':4,'mediaUrl':'local','mediaFileName':'v.wav'}
        client.get=lambda *args,**kw:{'messages':[m]}
        with patch('workdesk.assistant.request.build_opener',side_effect=AssertionError('External request forbidden')):
            client.page('alice',1,5,0,200)
        self.assertEqual(client.transcripts['alice_4_3'],'本地识别结果')
        self.assertEqual([p.suffix for p in client.cache.iterdir()],['.json'])
    def test_invalid_and_incomplete_wav_refused_before_model_load(self):
        if not __import__('importlib').util.find_spec('sherpa_onnx'):self.skipTest('Optional local voice runtime not installed')
        voice=LocalVoice({'weflow_config':str(self.config)})
        with self.assertRaises(ValueError):voice.transcribe(b'not a wave')
        buffer=io.BytesIO()
        with wave.open(buffer,'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(b'\0\0'*160)
        with self.assertRaises(ValueError):voice.transcribe(buffer.getvalue()[:-4])

if __name__=='__main__':unittest.main()
